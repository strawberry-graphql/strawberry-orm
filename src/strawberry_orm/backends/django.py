"""Django backend -- built from scratch using Django model introspection."""

from __future__ import annotations

import datetime
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from inspect import iscoroutinefunction
from typing import Any

import strawberry
from django.db import models
from django.db.models import Aggregate, FloatField, Model, QuerySet
from strawberry.extensions import SchemaExtension

from strawberry_orm._async import async_safe_resolver, materialize_result, run_sync
from strawberry_orm.backends.filter_budget import check_filter_budget
from strawberry_orm.backends.filter_pk_shortcut import (
    build_reference_object_filter_clause,
)
from strawberry_orm.fields import call_scope
from strawberry_orm.filters import (
    check_regex,
    is_fk_shortcut_lookup,
    is_reference_lookup,
)
from strawberry_orm.lazy_resolution import reads_relation
from strawberry_orm.leaf_fields import is_default_relay_id
from strawberry_orm.optimizer import OptimizerExtension

from ._base import (
    WRITE_REFUSED,
    AggregateMeta,
    BaseBackend,
    extract_element_type,
    hint_hops,
    invoke_custom_callback,
    key_in_range,
    model_is_type_of,
    past_a_hop,
    requested_aggregates,
)

#: Relation-name sets by model class; see :meth:`DjangoBackend.relation_names`.
_RELATION_NAMES: dict[type, set[str]] = {}

#: Columns PostgreSQL has no ``MIN`` or ``MAX`` of, even cast.
_UNRANKABLE_FIELDS = frozenset(
    {
        "JSONField",
        "BinaryField",
        "HStoreField",
        "SearchVectorField",
        "GeometryField",
        "RasterField",
    }
)


def _relation_kind(field: Any) -> str | None:
    """``"one"`` or ``"many"`` for a relation the optimizer can load, else ``None``.

    Told by Django's relation flags, not the field's class, so a subclass
    (django-mptt's ``TreeForeignKey``, modelcluster's ``ParentalKey``) is the
    relation it extends rather than a column.
    """
    if not getattr(field, "is_relation", False) or field.related_model is None:
        return None
    return "one" if field.many_to_one or field.one_to_one else "many"


def _followed_model(field: Any) -> type | None:
    """The model filters, orders and groups may follow *field* to, or ``None``.

    A generic relation is loaded and read like any to-many relation, but its
    rows are also told apart by content type, which the subqueries correlated
    on its key would not check, so it is not followed.
    """
    if _relation_kind(field) is None or (field.one_to_many and not field.auto_created):
        return None
    return field.related_model


class _PlannedLookup:
    """One relation in a query's load plan, with the relations under it.

    ``to_attr`` is set for an ``on=`` view, which loads ``relation`` into an
    attribute of its own and is never shared. ``token`` is the edge's own
    ``scope=``, which the cache it fills is marked with.
    """

    __slots__ = (
        "to_one",
        "queryset",
        "relation",
        "to_attr",
        "scoped",
        "token",
        "children",
    )

    def __init__(
        self,
        to_one: bool,
        queryset: Any,
        relation: str = "",
        to_attr: str | None = None,
        scoped: bool = False,
        token: Any = None,
    ) -> None:
        self.to_one = to_one
        self.queryset = queryset
        self.relation = relation
        self.to_attr = to_attr
        self.scoped = scoped
        self.token = token
        self.children: dict[str, _PlannedLookup] = {}


_DJANGO_FIELD_MAP: dict[str, type] = {
    "AutoField": int,
    "BigAutoField": int,
    "SmallAutoField": int,
    "IntegerField": int,
    "SmallIntegerField": int,
    "BigIntegerField": int,
    "PositiveIntegerField": int,
    "PositiveSmallIntegerField": int,
    "PositiveBigIntegerField": int,
    "FloatField": float,
    "DecimalField": Decimal,
    "CharField": str,
    "TextField": str,
    "SlugField": str,
    "URLField": str,
    "EmailField": str,
    "FilePathField": str,
    "BooleanField": bool,
    "NullBooleanField": bool,
    "DateField": datetime.date,
    "TimeField": datetime.time,
    "DateTimeField": datetime.datetime,
    "DurationField": str,
    "UUIDField": str,
    "GenericIPAddressField": str,
    "IPAddressField": str,
    "FileField": str,
    "ImageField": str,
    "BinaryField": bytes,
    "JSONField": str,
}


def _primary_key(value: Any) -> Any:
    return getattr(value, "pk", getattr(value, "id", None))


class DjangoBackend(BaseBackend):
    """Backend adapter for Django."""

    def __init__(
        self,
        *,
        max_filter_depth: int = 10,
        max_filter_branches: int = 50,
        enable_regex_filters: bool = False,
        max_in_list_size: int = 500,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._django_async_safe: bool = kwargs.get("django_async_safe", True)
        self._max_filter_depth = max_filter_depth
        self._max_filter_branches = max_filter_branches
        self._enable_regex_filters = enable_regex_filters
        self._max_in_list_size = max_in_list_size

    def wrap_async_safe(self, resolver: Any, *, materialize: bool = True) -> Any:
        if not self._django_async_safe:
            return resolver
        return async_safe_resolver(resolver, materialize=materialize)

    def _introspect_model(
        self, model: type
    ) -> list[tuple[str, type, bool, type | None]]:
        """Return (field_name, python_type, is_relation, related_model) for each
        field on a Django model."""
        meta = model._meta  # type: ignore[attr-defined]
        result: list[tuple[str, type, bool, type | None]] = []

        for field in meta.get_fields():
            if field.is_relation:
                result.append((field.name, Any, True, _followed_model(field)))
                if isinstance(field, models.ForeignKey) and field.attname != field.name:
                    # The column holds the value of the field the key points
                    # at, the primary key unless ``to_field`` names another.
                    fk_type: type = _DJANGO_FIELD_MAP.get(
                        type(field.target_field).__name__, str
                    )
                    if field.null:
                        fk_type = fk_type | None  # type: ignore[assignment]
                    result.append((field.attname, fk_type, False, field.related_model))
                continue

            py_type = _DJANGO_FIELD_MAP.get(type(field).__name__, str)
            result.append((field.name, py_type, False, None))

        return result

    def _get_pk_names(self, model: type) -> set[str]:
        meta = model._meta  # type: ignore[attr-defined]
        if meta.pk is None:
            return set()
        return {meta.pk.name}

    def _generic_key_columns(self, model: type) -> set[str]:
        # contenttypes is not imported: an app without it installed has no
        # GenericForeignKey, and importing it would refuse to load.
        meta = model._meta  # type: ignore[attr-defined]
        columns: set[str] = set()
        for field in meta.private_fields:
            if hasattr(field, "ct_field") and hasattr(field, "fk_field"):
                # The content type picks the model the key is read in.
                columns |= {field.fk_field, meta.get_field(field.ct_field).attname}
        return columns

    # -- Type generation -----------------------------------------------------

    def type(self, model: type, **kwargs: Any) -> Any:
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        name = kwargs.get("name")
        filters = kwargs.get("filters")
        order = kwargs.get("order")
        group = kwargs.get("group")
        aggregate = kwargs.get("aggregate")

        def decorator(cls: type) -> Any:
            if "is_type_of" not in cls.__dict__:
                cls.is_type_of = model_is_type_of(model)  # type: ignore[method-assign]

            meta = model._meta  # type: ignore[attr-defined]

            col_types: dict[str, type] = {}
            rel_fields: dict[str, dict[str, Any]] = {}

            for field_obj in meta.get_fields():
                if field_obj.is_relation:
                    kind = _relation_kind(field_obj)
                    if kind is not None:
                        rel_fields[field_obj.name] = {
                            "kind": kind,
                            "model": field_obj.related_model,
                        }
                    if (
                        isinstance(field_obj, models.ForeignKey)
                        and field_obj.attname != field_obj.name
                    ):
                        col_types[field_obj.attname] = (
                            int | None if field_obj.null else int
                        )
                    continue
                py_type = _DJANGO_FIELD_MAP.get(type(field_obj).__name__, str)
                if getattr(field_obj, "null", False):
                    py_type = py_type | None  # type: ignore[assignment]
                col_types[field_obj.name] = py_type

            type_name = self._process_type_annotations(
                cls,
                model,
                col_types,
                include=include,
                exclude=exclude,
                name=name,
                filters=filters,
                order=order,
                group=group,
                aggregate=aggregate,
            )

            annotations = getattr(cls, "__annotations__", {})
            for field_name, rel_info in rel_fields.items():
                if field_name not in annotations:
                    continue
                if field_name in vars(cls):
                    continue
                kind = rel_info["kind"]
                ann = annotations[field_name]
                if kind == "many":
                    el_type = extract_element_type(ann)
                    f_type = (
                        getattr(el_type, "__orm_filter__", None) if el_type else None
                    )
                    o_type = (
                        getattr(el_type, "__orm_order__", None) if el_type else None
                    )
                    rel_model = rel_info["model"]

                    if f_type or o_type:
                        setattr(
                            cls,
                            field_name,
                            _make_dj_rel_resolver(
                                self,
                                field_name,
                                rel_model,
                                f_type,
                                o_type,
                            ),
                        )
                    else:

                        def _make_resolver(
                            fname: str, return_ann: Any, backend: Any
                        ) -> Any:
                            def resolver(self: Any, info: Any) -> Any:
                                with _read_errors(_instance_db(self)):
                                    return list(
                                        _scoped_related_queryset(
                                            backend, self, fname, info
                                        )
                                    )

                            resolver.__name__ = fname
                            resolver.__annotations__ = {
                                "info": strawberry.types.Info,
                                "return": return_ann,
                            }
                            reads_relation(resolver, fname)
                            if backend._django_async_safe:
                                resolver = async_safe_resolver(
                                    resolver,
                                    in_memory=_relation_in_memory(backend, fname),
                                )
                            return strawberry.field(resolver=resolver)

                        setattr(cls, field_name, _make_resolver(field_name, ann, self))
                else:

                    def _make_fk_resolver(
                        fname: str, return_ann: Any, backend: Any
                    ) -> Any:
                        def resolver(self: Any, info: Any) -> Any:
                            with _read_errors(_instance_db(self)):
                                return _scoped_related_instance(
                                    backend, self, fname, info
                                )

                        resolver.__name__ = fname
                        resolver.__annotations__ = {
                            "info": strawberry.types.Info,
                            "return": return_ann,
                        }
                        reads_relation(resolver, fname)
                        if backend._django_async_safe:
                            resolver = async_safe_resolver(
                                resolver,
                                materialize=False,
                                in_memory=_relation_in_memory(backend, fname),
                            )
                        return strawberry.field(resolver=resolver)

                    setattr(cls, field_name, _make_fk_resolver(field_name, ann, self))

            annotations = getattr(cls, "__annotations__", {})
            self._check_lazy_relation_fields(cls, model, annotations)
            graphql_type = self._finalize_type(cls, model, type_name, name)
            if self._django_async_safe:
                graphql_type = self._post_process_strawberry_fields(graphql_type, model)
            return graphql_type

        return decorator

    def apply_ref_list(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Any | None = None,
    ) -> Any:
        from django.db import router, transaction

        from strawberry_orm.upserts import run_sync as run_steps

        def apply() -> None:
            writer = self._ref_list_writer(instance, field, refs, info, authorize)
            spec = self._upsert_relation_spec(type(instance), field)
            # The parent may have been read from a replica; the writes are not.
            using = router.db_for_write(type(instance), instance=instance)
            with _write_errors(), transaction.atomic(using=using):
                run_steps(writer.apply_refs(instance, spec, refs))

        return run_sync(apply, thread_sensitive=True)

    # -- Upserts -------------------------------------------------------------

    def apply_upsert(
        self, payload: Any, info: Any, *, authorize: Any | None = None
    ) -> Any:
        from django.db import router, transaction

        from strawberry_orm.upserts import run_sync as run_steps

        def apply() -> Any:
            writer = self._upsert_writer(info, authorize)
            writer.validate_root(payload)
            model = type(payload).__orm_model__
            with _write_errors(), transaction.atomic(using=router.db_for_write(model)):
                return run_steps(writer.apply_root(payload))

        return run_sync(apply, thread_sensitive=True)

    def _upsert_ops(self, info: Any) -> Any:
        return _DjangoUpsertOps(info)

    def _upsert_relation_spec(self, model: type, field: str) -> Any:
        from strawberry_orm.mutations import _django_relation_specs

        return _django_relation_specs(model)[field]

    def subclasses_read_as_base(self, model: type) -> list[type]:
        # A proxy shares its base's table, so nothing tells its rows apart.
        found: list[type] = []
        pending = list(model._meta.concrete_model.__subclasses__())  # type: ignore[attr-defined]
        while pending:
            sub = pending.pop(0)
            pending.extend(sub.__subclasses__())
            if not sub._meta.proxy and sub not in found:
                found.append(sub)
        return found

    def _scope_subclass_writes(
        self, rows: Any, repo: Any, model: type, info: Any
    ) -> Any:
        """*rows* of *model*, less the rows of a multi-table child its own repo hides.

        Read through *model*, such a row comes back as a *model* row, yet it
        is written through its own model's repo (see ``UpsertWriter._owner``),
        so counting a match on one that repo hides would answer for a row the
        caller cannot reach. A child of it with a repo of its own answers for
        its own rows.
        """
        from django.db.models import Q

        apart = self.classes_written_apart(repo, self.subclasses_read_as_base(model))
        parent = model._meta.concrete_model  # type: ignore[attr-defined]
        for cls, own in apart.items():
            link = _parent_link(cls, parent)
            shown = ~Q(pk__in=cls._base_manager.values(link)) | Q(
                pk__in=own.scope_query(cls._default_manager.all(), info).values(link)
            )
            for other in apart:
                if other is not cls and issubclass(other, cls):
                    shown |= Q(
                        pk__in=other._base_manager.values(_parent_link(other, parent))
                    )
            rows = rows.filter(shown)
        return rows

    def _find_for_upsert(
        self, repo: Any, model: type, query: dict[str, Any], info: Any
    ) -> Any:
        rows = repo.scope_query(model._default_manager.all(), info).filter(
            **self._upsert_lookup_kwargs(query)
        )
        rows = self._scope_subclass_writes(rows, repo, model, info)
        return self._single_upsert_match(model, list(rows[:2]))

    async def _find_for_upsert_async(
        self, repo: Any, model: type, query: dict[str, Any], info: Any
    ) -> Any:
        return await run_sync(
            self._find_for_upsert, repo, model, query, info, thread_sensitive=True
        )

    # -- Query application ----------------------------------------------------

    def apply_filters(
        self, query: Any, filter_input: Any, model: type, info: Any = None
    ) -> Any:
        check_filter_budget(
            filter_input,
            max_depth=self._max_filter_depth,
            max_branches=self._max_filter_branches,
            max_values=self._max_in_list_size,
        )
        with self.filtering(model, info), _refused_values():
            q_obj, query = _build_django_filter(
                filter_input,
                query=query,
                model=model,
                info=info,
                backend=self,
                max_depth=self._max_filter_depth,
                max_branches=self._max_filter_branches,
                enable_regex=self._enable_regex_filters,
                max_in_list_size=self._max_in_list_size,
            )
        if q_obj is None:
            return query
        if self.filter_crosses_to_many(filter_input, model):
            # Matched in a subquery, so the outer query keeps each row once.
            from django.db.models import Q

            with _refused_values():
                matching = model._base_manager.filter(q_obj)  # type: ignore[attr-defined]
            q_obj = Q(pk__in=matching.values("pk"))
        with _refused_values():
            return query.filter(q_obj)

    def apply_ordering(
        self, query: Any, order_input: Any, model: type, info: Any = None
    ) -> Any:
        from strawberry_orm.grouping import SetOrder, order_terms

        clauses: list[Any] = []
        for term in order_terms(self, model, order_input):
            if isinstance(term, SetOrder):
                clauses.append(self._set_order(term, info))
                continue
            entry_clauses, query = _build_django_ordering(
                term, query=query, model=model, info=info, backend=self
            )
            clauses.extend(entry_clauses)
        if clauses:
            query = query.order_by(*clauses)
        return query

    # -- Grouping / aggregation -----------------------------------------------

    def apply_aggregation(
        self, query: Any, info: Any, aggregate_meta: AggregateMeta
    ) -> Any:
        requested = requested_aggregates(info, "aggregates", aggregate_meta) or {}
        if not requested:
            return aggregate_meta.aggregates_type(count=0)

        agg_kwargs = _django_aggregates(requested)
        if not agg_kwargs:
            return aggregate_meta.build_aggregates(None, requested)

        def _run():
            with _read_errors(query.db):
                result = _own_rows(query).aggregate(**agg_kwargs)
            row = type("Row", (), result)()
            return aggregate_meta.build_aggregates(row, requested)

        return run_sync(_run, thread_sensitive=True)

    def together_reader(self, model: type, column: str, interval: Any) -> Any:
        from django.conf import settings
        from django.utils import timezone

        read = super().together_reader(model, column, interval)
        field = model._meta.get_field(column)  # type: ignore[attr-defined]
        if not isinstance(field, models.DateTimeField):
            return read
        # Read as Django reads the column: with time zones on, a datetime is
        # stored in UTC, and a cut one is the start of its period here.
        zone = timezone.get_current_timezone() if interval is not None else datetime.UTC

        def read_datetime(value: Any) -> Any:
            value = read(value)
            if settings.USE_TZ and value is not None and timezone.is_naive(value):
                return timezone.make_aware(value, zone)
            return value

        return read_datetime

    def relation_hop(self, model: type, relation: str) -> Any:
        from strawberry_orm.grouping import GroupHop

        field = model._meta.get_field(relation)  # type: ignore[attr-defined]
        return GroupHop(
            relation=relation,
            model=model,
            target=field.related_model,
            many=_relation_kind(field) == "many",
        )

    def apply_grouping(
        self,
        query: Any,
        paths: list[Any],
        info: Any,
        aggregate_meta: AggregateMeta,
        *,
        order_input: Any | None = None,
    ) -> list[Any]:
        from strawberry_orm.grouping import build_group, within_max_groups

        requested = (
            requested_aggregates(info, "groups.aggregates", aggregate_meta) or {}
        )
        aggregates = _django_aggregates(requested)
        aliases = [path.alias for path in paths]

        def _run():
            rows = self._grouped_rows(query, paths, info)
            firsts, ordering = _group_ordering(
                _connection_ordering(query, order_input, backend=self, info=info),
                query.model,
            )
            grouped = (
                rows.order_by()
                .values(*aliases)
                .annotate(**aggregates, **firsts)
                .order_by(*ordering, *aliases)
            )
            if not aggregates and not firsts:
                # Django groups by the values only to aggregate; without an
                # aggregate each row would be a group of its own.
                grouped = grouped.distinct()
            return [
                build_group(
                    aggregate_meta,
                    paths,
                    row,
                    requested,
                    max_key_rows=self._max_group_key_rows,
                )
                for row in within_max_groups(
                    _listed(grouped[: self._max_groups + 1]), self._max_groups
                )
            ]

        return run_sync(_run, thread_sensitive=True)

    def batch_group_items(
        self,
        query: Any,
        paths: list[Any],
        info: Any,
        model: type,
        *,
        per_group_limit: int,
        order_input: Any | None = None,
        per_group_offset: int = 0,
    ) -> dict[tuple, list[Any]]:
        from collections import defaultdict

        from django.db.models import F, Window
        from django.db.models.functions import RowNumber

        from strawberry_orm.grouping import key_tuple

        ordering = _connection_ordering(query, order_input, backend=self, info=info)
        tiebreak = _pk_tiebreak(ordering, model)
        if tiebreak is not None:
            ordering = [*ordering, F(tiebreak).asc()]

        def _run():
            qs = (
                self._grouped_rows(query, paths, info)
                .annotate(
                    _rn=Window(
                        expression=RowNumber(),
                        partition_by=[F(path.alias) for path in paths],
                        order_by=ordering,
                    )
                )
                .filter(
                    _rn__gt=per_group_offset,
                    _rn__lte=per_group_offset + per_group_limit,
                )
                # Ordered by the row number so each group comes back in the
                # order the window put it in; without it the rows arrive
                # however the database found them and the order is lost.
                .order_by("_rn")
            )
            items_by_key: dict[tuple, list[Any]] = defaultdict(list)
            for row in _listed(qs):
                values = {path.alias: getattr(row, path.alias) for path in paths}
                items_by_key[key_tuple(paths, values)].append(row)
            return dict(items_by_key)

        return run_sync(_run, thread_sensitive=True)

    def group_memberships(
        self, query: Any, paths: list[Any], pks: list[Any], info: Any
    ) -> Any:
        from collections import defaultdict

        from strawberry_orm.grouping import key_tuple

        def _run():
            rows = (
                self._grouped_rows(query, paths, info)
                .filter(pk__in=pks)
                .order_by()
                .values("pk", *(path.alias for path in paths))
            )
            found: dict[Any, set[tuple]] = defaultdict(set)
            for row in _listed(rows):
                found[row["pk"]].add(key_tuple(paths, row))
            return dict(found)

        return run_sync(_run, thread_sensitive=True)

    def _grouped_rows(self, query: Any, paths: list[Any], info: Any) -> Any:
        """*query*'s rows, each annotated with its value for every path.

        A row appears once per key a to-many path reads off its related rows,
        and once with ``None`` when it has none the caller may see.
        """
        from django.db.models import Exists, F

        rows = _own_rows(query)
        markers: dict[str, Any] = {}
        keep: list[Any] = []
        values = {
            path.alias: self._group_value(path, rows, info, markers, keep)
            for path in paths
        }
        through_many: dict[str, list[Any]] = {}
        for path in paths:
            if path.hops and path.hops[0].many:
                through_many.setdefault(path.hops[0].relation, []).append(path)
                markers.setdefault(
                    f"_m_{path.hops[0].relation}", F(f"{path.hops[0].relation}__pk")
                )
        if markers:
            rows = rows.alias(**markers)
        rows = rows.annotate(**values)
        for condition in keep:
            rows = rows.filter(condition)
        for through in through_many.values():
            rows = rows.exclude(Exists(self._same_key_earlier(through, info)))
        return rows

    def _same_key_earlier(self, paths: list[Any], info: Any) -> Any:
        """The related rows, ahead of the one joined, that give the same key.

        Every path here starts across one to-many relation. A row joined to
        two related rows with the same values would be grouped twice under
        one key, so only the first related row with those values is kept.
        """
        from django.db.models import OuterRef, Q
        from django.db.models.lookups import IsNull

        hop = paths[0].hops[0]
        restrict = self.relation_scope(hop.model, hop.relation, info)
        related = (
            restrict(hop.target._default_manager.all(), info)
            if restrict is not None
            else hop.target._base_manager.all()
        )
        earlier = related.filter(
            **{
                _remote_key(hop): OuterRef("pk"),
                "pk__lt": OuterRef(f"_m_{hop.relation}"),
            }
        )
        for path in paths:
            onward = replace(path, hops=path.hops[1:])
            name = f"_v{path.alias}"
            earlier = earlier.annotate(
                **{name: self._group_value(onward, earlier, info, {}, [])}
            ).filter(
                Q(**{name: OuterRef(path.alias)})
                | (Q(**{f"{name}__isnull": True}) & IsNull(OuterRef(path.alias), True))
            )
        return earlier

    def _group_value(
        self,
        path: Any,
        rows: Any,
        info: Any,
        markers: dict[str, Any],
        keep: list[Any],
    ) -> Any:
        """The expression *path* groups by, read through every hop's scope.

        A related row the caller may not see reads as ``None``, as it does on
        the type. Across a to-many hop the hidden rows are dropped instead,
        leaving one ``None`` row for a parent with no visible ones.
        """
        from functools import reduce
        from operator import and_

        from django.db.models import Case, F, Q, When

        if path.through is not None:
            return self._together(path, info, limit=self._max_group_key_rows + 1)

        if path.custom is not None:
            expr = invoke_custom_callback(
                path.custom, path.owner, query=rows, value=path.value, info=info
            )
            if isinstance(expr, tuple):
                expr = expr[0]
            return F(expr) if isinstance(expr, str) else expr

        prefix = ""
        conditions = []
        for hop in path.hops:
            restrict = self.relation_scope(hop.model, hop.relation, info)
            if restrict is not None:
                visible = restrict(hop.target._default_manager.all(), info)
                if hop.many:
                    _keep_visible_links(hop, visible, markers, keep)
                conditions.append(
                    Q(**{f"{prefix}{hop.relation}__pk__in": visible.values("pk")})
                )
            prefix = f"{prefix}{hop.relation}__"

        expr = F(f"{prefix}{path.column}")
        if conditions:
            expr = Case(When(reduce(and_, conditions), then=expr), default=None)
        if path.interval is not None:
            # Cut outside the CASE: the cut converts what the database hands
            # back (a date, an aware datetime), and inside it would not.
            owner = path.model or rows.model
            expr = _truncate(expr, owner._meta.get_field(path.column), path.interval)
        return expr

    def _together(self, path: Any, info: Any, *, limit: int | None = None) -> Any:
        """The related rows *path* reads of each row, as ``_Together`` JSON text.

        Only the rows the caller may see, as the field reads on the type, and
        at most *limit* of them: a group key is refused past
        ``max_group_key_rows`` rows, and reading one more tells it so.
        """
        from django.db.models import F, OuterRef

        hop = path.through
        restrict = self.relation_scope(hop.model, hop.relation, info)
        related = (
            restrict(hop.target._default_manager.all(), info)
            if restrict is not None
            else hop.target._base_manager.all()
        )
        names = [f"_orm_c{index}" for index in range(len(path.columns))]
        values = {
            name: F(column)
            if interval is None
            else _truncate(F(column), hop.target._meta.get_field(column), interval)
            for name, column, interval in zip(
                names, path.columns, path.intervals, strict=True
            )
        }
        rows_together = (
            related.filter(**{_remote_key(hop): OuterRef("pk")})
            .annotate(**values)
            .values(*names)
            .distinct()
            .order_by(*names)
        )
        if limit is not None:
            rows_together = rows_together[:limit]
        return _Together(rows_together, names)

    def _set_order(self, by_set: Any, info: Any) -> Any:
        """The clause a ``SetOrder`` orders by: the text its set groups under."""
        together = self._together(by_set.path, info)
        return together.desc() if by_set.descending else together.asc()

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type, *, limited: bool = True) -> Any:
        qs = model.objects.all()
        if limited and self._default_query_limit is not None:
            qs = qs[: self._default_query_limit]
        return qs

    def is_query_object(self, value: Any) -> bool:
        return isinstance(value, QuerySet)

    def is_model_instance(self, value: Any) -> bool:
        return isinstance(value, Model)

    def relation_names(self, model: type) -> set[str]:
        # Called once per field per row while resolving, so the set is cached
        # per model class. Django's field list is fixed once apps are loaded.
        names = _RELATION_NAMES.get(model)
        if names is None:
            names = {
                f.name
                for f in model._meta.get_fields()  # type: ignore[attr-defined]
                if getattr(f, "is_relation", False)
            }
            _RELATION_NAMES[model] = names
        return names

    def _relation_target_model(self, model: type, relation: str) -> type | None:
        try:
            field = model._meta.get_field(relation)  # type: ignore[attr-defined]
        except Exception:
            return None
        return getattr(field, "related_model", None)

    def instance_pk(self, instance: Any) -> Any:
        return getattr(instance, "pk", None)

    def query_model(self, query: Any) -> type | None:
        return getattr(query, "model", None)

    def filter_scoped_pks(self, model: type, pks: list[Any], info: Any) -> set[Any]:
        def _run() -> set[Any]:
            queryset = self.apply_type_scope(
                model._default_manager.filter(pk__in=pks), model, info
            )
            return set(_listed(queryset.values_list("pk", flat=True)))

        return run_sync(_run, thread_sensitive=True)

    def split_parent_predicate(
        self, query: Any, parent_pk: Any
    ) -> tuple[str, Any, Any] | None:
        from django.db.models.sql.where import AND, WhereNode

        inner = query.query
        if inner.combinator or inner.distinct_fields or inner.extra:
            return None
        if inner.low_mark or inner.high_mark is not None:
            return None

        remainder = query._chain()
        where = remainder.query.where
        if where.connector != AND or where.negated:
            return None

        # The key is read back off each result row, so the predicate has to sit
        # on the row's own table. A joined column (``tags__posts__author``)
        # would group rows by a value that is not the one the join matched.
        base_alias = getattr(remainder.query, "base_table", None)

        hits = []
        for child in where.children:
            if isinstance(child, WhereNode):
                # A nested group is fine as long as the parent key is not
                # inside it; an OR arm mentioning the parent is not rewritable.
                if _references_parent(child, parent_pk):
                    return None
                continue
            if _is_parent_predicate(child, parent_pk, base_alias):
                hits.append(child)

        if len(hits) != 1:
            return None

        hit = hits[0]
        attname = hit.lhs.target.attname
        where.children = [child for child in where.children if child is not hit]
        return attname, attname, remainder

    def query_signature(self, query: Any) -> str | None:
        # ``str(query.query)`` inlines parameters without quoting or type
        # information, so ``title=1`` and ``title="1"`` render identically even
        # though a strictly typed database treats them differently. Sign the
        # parameterised SQL plus typed parameters instead.
        try:
            sql, params = query.query.get_compiler(using=query.db).as_sql()
        except Exception:  # pragma: no cover - defensive
            return None
        typed = tuple((type(param).__name__, repr(param)) for param in params)
        # The same SQL read from two databases (a tenant's, a replica) reads
        # different rows, so the alias is part of what the query is.
        return repr((query.db, sql, typed))

    def apply_key_filter(
        self, query: Any, attr_name: str, key_handle: Any, keys: list[Any]
    ) -> Any:
        return query.filter(**{f"{attr_name}__in": list(keys)})

    @contextmanager
    def query_probe(self, info: Any) -> Any:
        """Count statements issued on the default connection while running."""
        from django.db import connection

        from strawberry_orm.lazy_resolution import QueryProbe

        probe = QueryProbe()

        def _wrapper(execute: Any, sql: Any, params: Any, many: Any, ctx: Any) -> Any:
            probe.count += 1
            return execute(sql, params, many, ctx)

        with connection.execute_wrapper(_wrapper):
            yield probe

    def count_query(self, query: Any, info: Any) -> Any:
        return run_sync(_counted, query, thread_sensitive=True)

    def apply_page(self, query: Any, offset: int, limit: int) -> Any | None:
        from strawberry_orm.lazy_resolution import mark_query_scoped, query_is_scoped

        # Rows already in memory cost nothing to slice; a cut would re-read them.
        if query._result_cache is not None:
            return None
        # Slicing composes with an existing slice. The clone forgets that
        # scope_rows was applied, and a sliced queryset can no longer be
        # filtered, so the optimizer must not try to apply it again.
        page = _ordered_to_the_row(query)[offset : offset + limit]
        if query_is_scoped(query):
            mark_query_scoped(page)
        return page

    def materialize_query(self, query: Any, info: Any) -> Any:
        from strawberry_orm._async import in_async_context

        if not in_async_context():
            return _listed(query)
        return run_sync(_listed, query, thread_sensitive=True)

    _supports_on_prefetch = True
    _supports_relation_batching = True
    _supports_windowed_pages = True

    def _make_on_resolver(
        self, model: type, field_name: str, relation: str, return_ann: Any
    ) -> Any:
        return _make_on_resolver(self, field_name, relation, return_ann)

    def _post_process_strawberry_fields(
        self, graphql_type: type, model: type | None = None
    ) -> type:
        if not self._django_async_safe:
            return graphql_type

        from strawberry.types.field import UNRESOLVED
        from strawberry.types.fields.resolver import StrawberryResolver

        for field in graphql_type.__strawberry_definition__.fields:
            if getattr(field, "_orm_connection", False):
                continue

            resolver = field.base_resolver
            if resolver is not None:
                wrapped = resolver.wrapped_func
                if wrapped is not None and not iscoroutinefunction(wrapped):
                    in_memory = None
                    if model is not None and is_default_relay_id(graphql_type, wrapped):
                        in_memory = _relay_id_in_memory(model, graphql_type)
                        field._orm_relay_id = True  # type: ignore[attr-defined]
                    resolver.wrapped_func = async_safe_resolver(
                        wrapped, in_memory=in_memory
                    )
                continue

            python_name = field.python_name
            if not python_name:
                continue

            captured_field = field
            captured_name = python_name

            def _make_resolve_basic(
                bound_field: Any = captured_field,
                bound_name: str = captured_name,
            ) -> Any:
                def _resolve_basic(root: Any) -> Any:
                    value = bound_field.default_resolver(root, bound_name)
                    return materialize_result(self, value, None, sync=False)

                return async_safe_resolver(
                    _resolve_basic,
                    materialize=False,
                    in_memory=model and _column_in_memory(model, bound_name),
                )

            attname = model and _column_attname(model, python_name, stored_as_read=True)
            if attname and not field.extensions:
                # Left without a resolver, so Strawberry keeps its fast path
                # for plain attributes; only an unloaded column goes the long way.
                field.__class__ = _column_field_class(type(field))
                field._orm_attname = attname  # type: ignore[attr-defined]
                field._orm_plain_reads = {}  # type: ignore[attr-defined]
                field._orm_unloaded_read = _make_resolve_basic()  # type: ignore[attr-defined]
                continue

            field_type = field.type
            field.base_resolver = StrawberryResolver(
                _make_resolve_basic(),
                type_override=field_type if field_type is not UNRESOLVED else None,
            )
            field._orm_basic_resolver = True  # type: ignore[attr-defined]

        return graphql_type

    # -- Optimizer -----------------------------------------------------------

    def _make_relation_query_resolver(
        self, model: type, field_name: str, relation: str
    ) -> Any:
        backend = self

        def resolver(self: Any, info: Any) -> Any:
            from strawberry_orm.batching import take_page

            page = take_page(self, field_name, info)
            if page is not None:
                return page
            restrict = backend.relation_scope(model, field_name, info, on=relation)
            queryset = getattr(self, relation).all()
            return queryset if restrict is None else restrict(queryset, info)

        resolver.__name__ = field_name
        return resolver

    def group_counts(self, query: Any, key_field: str, info: Any) -> Any:
        from django.db.models import Count

        def _run() -> dict[Any, int]:
            rows = query.values(key_field).annotate(_n=Count("pk"))
            return {row[key_field]: row["_n"] for row in _listed(rows)}

        return run_sync(_run, thread_sensitive=True)

    def _relation_connection_spec(
        self, model: type, field_name: str, relation: str
    ) -> Any:
        from strawberry_orm.backends._base import RelationConnectionSpec

        # The relation was checked before this is reached.
        rel = model._meta.get_field(relation)
        # Only a reverse FK carries the parent key on the related row; a
        # many-to-many keeps it in the join table, where a window cannot
        # partition by it.
        if not isinstance(rel, models.ManyToOneRel) or rel.one_to_one:
            return None
        fk = rel.field
        return RelationConnectionSpec(
            model=model,
            field_name=field_name,
            relation=relation,
            related_model=rel.related_model,
            # Rows are matched to parents by primary key. A key with
            # ``to_field`` holds another column of the parent, so the parent's
            # primary key is read through it.
            key_field=fk.attname if fk.target_field.primary_key else f"{fk.name}__pk",
        )

    def relation_base_query(self, spec: Any, keys: list[Any], info: Any) -> Any:
        queryset = spec.related_model._default_manager.filter(
            **{f"{spec.key_field}__in": keys}
        )
        restrict = self.relation_scope(
            spec.model, spec.field_name, info, on=spec.relation
        )
        return queryset if restrict is None else restrict(queryset, info)

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return OptimizerExtension.configure(backend=self, store=self._store)

    def _relation_lookups(
        self, store: Any, model: type, info: Any
    ) -> tuple[list[str], list[Any]]:
        """Return the lookups the current selection needs for *model*.

        Shared by query optimization and by loading relations onto instances
        the caller already holds, so both apply the same row scoping.
        """
        import re

        from django.db.models import Prefetch

        from strawberry_orm.optimizer.selections import (
            field_nodes_from_info,
            fragments_from_info,
            merged_field_nodes,
        )

        def compute() -> tuple[list[str], list[Any]]:
            select_related: list[str] = []
            prefetch_related: list[Any] = []
            fragments = fragments_from_info(info)

            def _to_snake(name: str) -> str:
                return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()

            def _edge_scope(type_name: str | None, field_name: str) -> Any:
                hints = (
                    store.get(type_name, field_name) if type_name and store else None
                )
                return hints.scope if hints else None

            def _get_nested_queryset(
                type_name: str | None,
                field_name: str,
                related_model: type,
            ) -> Any:
                """Build a custom queryset if the field has a scope callable or
                the related model's type defines scope_rows."""
                get_qs = self._type_querysets.get(related_model)
                load_fn = _edge_scope(type_name, field_name)

                if get_qs is None and load_fn is None:
                    return None

                qs = related_model.objects.all()
                if get_qs is not None:
                    qs = get_qs(qs, info)
                if load_fn is not None:
                    qs = call_scope(load_fn, qs, info)
                return qs

            # A relation reached both by the selection and by a ``using=`` path
            # must be loaded once, with one queryset. A plain lookup next to a
            # scoped one fills the cache with whichever rows Django loads first.
            plan: dict[str, _PlannedLookup] = {}

            def _planned(
                level: dict[str, _PlannedLookup],
                key: str,
                to_one: bool,
                type_name: str | None,
                related_model: type,
            ) -> _PlannedLookup:
                lookup = level.get(key)
                if lookup is None:
                    lookup = level[key] = _PlannedLookup(
                        to_one=to_one,
                        queryset=_get_nested_queryset(type_name, key, related_model),
                        token=_edge_scope(type_name, key),
                    )
                return lookup

            def _plan_hints(
                level: dict[str, _PlannedLookup],
                current_model: type,
                type_name: str | None,
                field_name: str,
            ) -> None:
                """Plan the relation paths declared on ``using=`` for *field_name*.

                Declared against the field name, so they apply to computed
                fields that are not ORM fields themselves.
                """
                hints = (
                    store.get(type_name, field_name) if type_name and store else None
                )
                if not hints or hints.disable_optimization or not hints.using:
                    return
                for path in hints.using:
                    hop_level, hop_model, hop_type = level, current_model, type_name
                    for hop in hint_hops(path):
                        try:
                            rel_field = hop_model._meta.get_field(hop)  # type: ignore[attr-defined]
                        except Exception:
                            break
                        kind = _relation_kind(rel_field)
                        if kind is None:
                            break
                        related_model = rel_field.related_model
                        lookup = _planned(
                            hop_level, hop, kind == "one", hop_type, related_model
                        )
                        hop_type = self._child_type_name(
                            hop_type, hop, related_model, info
                        )
                        hop_level, hop_model = lookup.children, related_model

            def _plan_selection(
                level: dict[str, _PlannedLookup],
                selection_sets: list[Any],
                current_model: type,
                type_name: str | None,
            ) -> None:
                meta = current_model._meta  # type: ignore[attr-defined]
                for name, nodes in merged_field_nodes(
                    selection_sets, fragments
                ).items():
                    field_name = _to_snake(name)

                    # A on= field is served by a relation under another name,
                    # so ask the model about the relation, not the field.
                    on = None
                    if type_name and store:
                        on_hints = store.get(type_name, field_name)
                        on = getattr(on_hints, "on", None) if on_hints else None
                    relation_name = on or field_name

                    # A field that answers for itself will ignore whatever the
                    # prefetch loads, so do not pay for it.
                    if on is None and self.resolves_itself(type_name, field_name):
                        continue
                    _plan_hints(level, current_model, type_name, field_name)

                    try:
                        field_obj = meta.get_field(relation_name)
                    except Exception:
                        continue

                    kind = _relation_kind(field_obj)
                    if kind is None:
                        continue
                    attname = getattr(field_obj, "attname", None)
                    if (
                        attname
                        and field_name == attname
                        and field_name != field_obj.name
                    ):
                        continue
                    related_model = field_obj.related_model
                    below = [node.selection_set for node in nodes if node.selection_set]

                    if kind == "many" and on is not None:
                        # to_attr keeps the two views apart: without it the
                        # second Prefetch of the same relation collides.
                        custom_qs = _get_nested_queryset(
                            type_name, field_name, related_model
                        )
                        view = level.setdefault(
                            f"@{field_name}",
                            _PlannedLookup(
                                to_one=False,
                                queryset=(
                                    custom_qs
                                    if custom_qs is not None
                                    else related_model._default_manager.all()
                                ),
                                relation=relation_name,
                                to_attr=field_name,
                                scoped=custom_qs is not None,
                                token=_edge_scope(type_name, field_name),
                            ),
                        )
                        if below:
                            _plan_selection(
                                view.children,
                                below,
                                related_model,
                                self._child_type_name(
                                    type_name, name, related_model, info
                                ),
                            )
                        continue

                    lookup = _planned(
                        level, field_name, kind == "one", type_name, related_model
                    )
                    if below:
                        child_type = self._child_type_name(
                            type_name, name, related_model, info
                        )
                        _plan_selection(
                            lookup.children,
                            below,
                            related_model,
                            child_type,
                        )

            def _emit(
                level: dict[str, _PlannedLookup], prefix: str, in_prefetch: bool
            ) -> None:
                """Parents go first: Django resolves ``a__b`` through ``a``."""
                for key, lookup in level.items():
                    if lookup.to_attr is not None:
                        relation = lookup.relation
                        path = f"{prefix}__{relation}" if prefix else relation
                        queryset = _without_select_related(
                            lookup.queryset, _scoped_paths(lookup.children)
                        )
                        prefetch = (
                            _scoped_prefetch(
                                path, queryset, lookup.token, to_attr=lookup.to_attr
                            )
                            if lookup.scoped
                            else Prefetch(
                                path, queryset=queryset, to_attr=lookup.to_attr
                            )
                        )
                        prefetch_related.append(prefetch)
                        # Django prefetches through the list a to_attr holds.
                        view_path = (
                            f"{prefix}__{lookup.to_attr}" if prefix else lookup.to_attr
                        )
                        _emit(lookup.children, view_path, True)
                        continue
                    path = f"{prefix}__{key}" if prefix else key
                    if lookup.queryset is not None:
                        queryset = _without_select_related(
                            lookup.queryset, _scoped_paths(lookup.children)
                        )
                        prefetch_related.append(
                            _scoped_prefetch(path, queryset, lookup.token)
                        )
                        _emit(lookup.children, path, True)
                    elif lookup.to_one and not in_prefetch:
                        select_related.append(path)
                        _emit(lookup.children, path, False)
                    else:
                        prefetch_related.append(path)
                        _emit(lookup.children, path, True)

            root_type_name = self._selection_type_name(model, info)
            _plan_selection(
                plan,
                [node.selection_set for node in field_nodes_from_info(info)],
                model,
                root_type_name,
            )
            _emit(plan, "", False)

            return select_related, prefetch_related

        return compute()

    def apply_optimizer_hints(self, store: Any, query: Any, info: Any) -> Any:
        def optimize() -> Any:
            try:
                model = query.model
            except AttributeError:
                return query
            return self._read_with_relations(
                store, self.apply_type_scope(query, model, info), info
            )

        return run_sync(optimize, thread_sensitive=True)

    def _read_with_relations(self, store: Any, query: Any, info: Any) -> list[Any]:
        """Read *query*, already scoped, with the relations the selection needs."""
        select_related, prefetch_related = self._relation_lookups(
            store, query.model, info
        )
        # One relation can be reached twice - two aliases, or the same
        # field under sibling fragments of an interface - and each pass
        # builds its own queryset. Django compares those by identity, so
        # duplicates have to be collapsed before they reach it.
        if select_related:
            query = query.select_related(*_dedupe_lookups(select_related))
        # select_related wins over a later Prefetch of the same relation
        # and would keep the unscoped row. Strip it after the optimizer's
        # own select_related so a scoped prefetch actually runs.
        query = _strip_conflicting_select_related(query, prefetch_related)
        if prefetch_related:
            query = query.prefetch_related(*_dedupe_lookups(prefetch_related))

        rows = _listed(query)
        _mark_scoped_prefetches(rows, _dedupe_lookups(prefetch_related))
        return rows

    def load_relations(self, store: Any, instances: list[Any], info: Any) -> list[Any]:
        """Eager-load the selected relations onto instances already in memory.

        ``prefetch_related_objects`` fills the relation caches in place, so
        scalar values the caller is holding - which may be fresher than the
        database, straight out of a mutation - are never overwritten.

        Returns the rows it actually loaded onto, which is empty when the
        selection named no relations.
        """

        def load() -> list[Any]:
            from django.db.models import prefetch_related_objects

            by_model: dict[type, list[Any]] = {}
            for instance in instances:
                by_model.setdefault(type(instance), []).append(instance)

            loaded: list[Any] = []
            for model, rows in by_model.items():
                select_related, prefetch_related = self._relation_lookups(
                    store, model, info
                )
                lookups = _dedupe_lookups([*select_related, *prefetch_related])
                if lookups:
                    # select_related / prefetch_related already on the instance
                    # makes Django skip this prefetch and keep the unscoped
                    # cache. Drop those caches so the scoped prefetch runs.
                    _clear_scoped_relation_caches(rows, lookups)
                    with _read_errors(_instance_db(rows[0])):
                        prefetch_related_objects(rows, *lookups)
                    _mark_scoped_prefetches(rows, lookups)
                    loaded.extend(rows)
            return loaded

        return run_sync(load, thread_sensitive=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _pk_tiebreak(ordering: Any, model: type) -> str | None:
    """The primary key to end *ordering* with, or ``None`` if it names it.

    Each page is cut by its own query, and a database breaks ties in a
    non-unique order however it likes, differently from one page to the next,
    so a row could land on two pages and another on none.
    """
    pk = model._meta.pk  # type: ignore[attr-defined]
    names = {"pk", pk.name, pk.attname}
    for term in ordering:
        if isinstance(term, str):
            name = term.lstrip("-")
        else:
            name = getattr(getattr(term, "expression", term), "name", None)
        if name in names:
            return None
    return pk.attname


def _ordered_to_the_row(queryset: Any) -> Any:
    """*queryset* in an order no two rows share: its own, then the primary key.

    A sliced queryset can no longer be reordered, and reordering one with an
    ``.extra(order_by=...)`` would drop that order, so those are left as they are.
    """
    query = queryset.query
    if query.is_sliced or query.extra_order_by:
        return queryset.all()
    ordering = query.order_by or (
        query.get_meta().ordering if query.default_ordering else ()
    )
    tiebreak = _pk_tiebreak(ordering, queryset.model)
    if tiebreak is None:
        return queryset.all()
    return queryset.order_by(*ordering, tiebreak)


def _make_on_resolver(
    backend: Any, field_name: str, relation: str, return_ann: Any
) -> Any:
    """Serve a field whose rows come from a differently named relation.

    The optimizer loads those rows under ``field_name`` on ``to_attr``, so
    they are read from there when present. Without the optimizer there is no
    such attribute, and the relation is queried directly - scoped here, because
    that is the only place left to apply it.
    """

    def resolver(self: Any, info: Any) -> Any:
        from strawberry_orm.lazy_resolution import relation_cache_is_scoped

        prefetched = self.__dict__.get(field_name)
        restrict = backend.relation_scope(type(self), field_name, info, on=relation)
        if prefetched is not None and (
            restrict is None
            or relation_cache_is_scoped(
                self,
                field_name,
                scope=backend._field_scope(type(self), field_name, info),
                held=_held,
            )
        ):
            return prefetched
        queryset = getattr(self, relation).all()
        return queryset if restrict is None else restrict(queryset, info)

    resolver.__name__ = field_name
    resolver.__annotations__ = {
        "info": strawberry.types.Info,
        "return": return_ann,
    }
    if backend._django_async_safe:
        resolver = async_safe_resolver(resolver)
    return resolver


#: On a row a scoped ``Prefetch`` read: that prefetch's tag.
_LOADED_BY = "_strawberry_orm_loaded_by"


def _scoped_prefetch(path: str, queryset: Any, token: Any = None, **kwargs: Any) -> Any:
    """A ``Prefetch`` whose cache is safe to trust at resolve time.

    *token* is the ``scope=`` of the field it loads for, if any. Every row
    its query reads is tagged, so the cache can be told from one Django
    filled some other way.
    """
    from django.db.models import Prefetch

    prefetch = Prefetch(path, queryset=queryset, **kwargs)
    tag = object()
    prefetch.queryset = _tagging(queryset, tag)
    prefetch._strawberry_orm_scoped = True  # type: ignore[attr-defined]
    prefetch._strawberry_orm_token = token  # type: ignore[attr-defined]
    prefetch._strawberry_orm_tag = tag  # type: ignore[attr-defined]
    return prefetch


def _tagging(queryset: Any, tag: object) -> Any:
    """*queryset*, leaving *tag* on every row it reads."""
    base = queryset._iterable_class

    class Tagging(base):  # type: ignore[misc, valid-type]
        def __iter__(self) -> Any:
            for row in super().__iter__():
                row.__dict__[_LOADED_BY] = tag
                yield row

    tagged = queryset._chain()
    tagged._iterable_class = Tagging
    return tagged


def _scoped_paths(level: dict[str, _PlannedLookup], prefix: str = "") -> list[str]:
    """Paths under *level*, relative to it, that a scoped ``Prefetch`` loads.

    A ``select_related`` of one of them in the query that reads *level*'s
    rows would fill the cache first, and Django would skip the prefetch.
    """
    paths: list[str] = []
    for key, lookup in level.items():
        if lookup.to_attr is not None:
            continue
        path = f"{prefix}__{key}" if prefix else key
        if lookup.queryset is not None:
            paths.append(path)
        else:
            paths.extend(_scoped_paths(lookup.children, path))
    return paths


def _without_select_related(queryset: Any, paths: list[str]) -> Any:
    """*queryset* without the ``select_related`` of any of *paths*."""
    current = queryset.query.select_related
    if not current or not paths:
        return queryset
    if current is True:
        return queryset.select_related(None)

    def prune(node: dict[str, Any], parts: list[str]) -> dict[str, Any]:
        head, *rest = parts
        if head not in node:
            return node
        if not rest:
            return {key: value for key, value in node.items() if key != head}
        return {**node, head: prune(node[head], rest)}

    for path in paths:
        current = prune(current, path.split("__"))
    clone = queryset._clone()
    clone.query.select_related = current or False
    return clone


def _scoped_prefetch_attr(lookup: Any) -> str | None:
    """Attribute a scoped ``Prefetch`` writes, or ``None`` for other lookups."""
    if not getattr(lookup, "_strawberry_orm_scoped", False):
        return None
    if getattr(lookup, "to_attr", None):
        return lookup.to_attr
    through = getattr(lookup, "prefetch_through", "") or ""
    head = through.split("__")[0]
    return head or None


def _strip_conflicting_select_related(queryset: Any, lookups: list[Any]) -> Any:
    """Drop ``select_related`` entries a scoped prefetch has to replace.

    Django keeps the ``select_related`` object and skips the prefetch, which
    would leave the unscoped row in the cache.
    """
    names = {name for lookup in lookups if (name := _scoped_prefetch_attr(lookup))}
    current = queryset.query.select_related
    if not current or not names:
        return queryset
    if current is True:
        return queryset.select_related(None)

    def prune(node: dict[str, Any]) -> dict[str, Any]:
        return {key: prune(value) for key, value in node.items() if key not in names}

    pruned = prune(current)
    clone = queryset._clone()
    clone.query.select_related = pruned or False
    return clone


def _clear_django_relation_cache(instance: Any, field_name: str) -> None:
    try:
        field = instance._meta.get_field(field_name)
    except Exception:
        field = None
    if (
        field is not None
        and getattr(field, "is_cached", None)
        and field.is_cached(instance)
    ):
        field.delete_cached_value(instance)
    cache = getattr(instance, "_prefetched_objects_cache", None)
    if isinstance(cache, dict):
        cache.pop(field_name, None)
    # on= stores its rows under to_attr, which is not a model field.
    if field is None:
        instance.__dict__.pop(field_name, None)


def _clear_scoped_relation_caches(instances: list[Any], lookups: list[Any]) -> None:
    names = [name for lookup in lookups if (name := _scoped_prefetch_attr(lookup))]
    if not names:
        return
    for instance in instances:
        for name in names:
            _clear_django_relation_cache(instance, name)


def _django_related_children(instances: list[Any], name: str) -> list[Any]:
    children: list[Any] = []
    for instance in instances:
        value = None
        state = getattr(instance, "_state", None)
        fields_cache = getattr(state, "fields_cache", None)
        if isinstance(fields_cache, dict) and name in fields_cache:
            value = fields_cache[name]
        if value is None:
            prefetched = getattr(instance, "_prefetched_objects_cache", None)
            if isinstance(prefetched, dict) and name in prefetched:
                value = prefetched[name]
        if value is None and name in getattr(instance, "__dict__", {}):
            value = instance.__dict__[name]
        if value is None:
            continue
        result_cache = getattr(value, "_result_cache", None)
        if isinstance(result_cache, list):
            children.extend(result_cache)
        elif isinstance(value, list):
            children.extend(value)
        else:
            children.append(value)
    return children


def _held(instance: Any, name: str) -> Any:
    """What relation *name* holds on *instance*: its prefetched queryset, row or list.

    A prefetch or a ``select_related`` replaces it, so a mark made with this
    stops holding once another load replaces what it vouched for.
    """
    prefetched = getattr(instance, "_prefetched_objects_cache", None)
    if isinstance(prefetched, dict) and name in prefetched:
        return prefetched[name]
    fields_cache = instance._state.fields_cache
    if name in fields_cache:
        return fields_cache[name]
    return instance.__dict__.get(name)


def _mark_scoped_prefetches(instances: list[Any], lookups: list[Any]) -> None:
    """Mark caches filled by scoped prefetches so resolvers can trust them.

    Django skips a prefetch where the relation is already cached - by a
    ``select_related``, a manager's or the scope's own - so a cache is marked
    only when every row in it was read by that prefetch. Django also caches
    the row a relation was reached from as the far side of a to-one, and that
    row was read under its own type's scope, not this relation's: it is kept
    if the prefetch's queryset reads it, and read as absent if not.
    """
    from strawberry_orm.lazy_resolution import mark_relations_scoped

    for lookup in lookups:
        tag = getattr(lookup, "_strawberry_orm_tag", None)
        if tag is None:
            continue
        through = getattr(lookup, "prefetch_through", "") or ""
        path = [part for part in through.split("__") if part]
        if not path:
            continue
        pairs = [(None, instance) for instance in instances]
        for segment in path[:-1]:
            pairs = [
                (node, child)
                for _, node in pairs
                for child in _django_related_children([node], segment)
            ]
        attr = getattr(lookup, "to_attr", None) or path[-1]
        filled = []
        returned = []
        for parent, node in pairs:
            if parent is not None and node._state.fields_cache.get(attr) is parent:
                returned.append((parent, node))
            elif all(
                getattr(row, _LOADED_BY, None) is tag
                for row in _django_related_children([node], attr)
            ):
                filled.append(node)
        if returned:
            seen = _read_by(lookup, [parent for parent, _node in returned])
            for parent, node in returned:
                if parent.pk not in seen:
                    node._state.fields_cache[attr] = None
                filled.append(node)
        mark_relations_scoped(
            filled,
            [attr],
            scope=getattr(lookup, "_strawberry_orm_token", None),
            held=_held,
        )


def _read_by(lookup: Any, rows: list[Any]) -> set[Any]:
    """The primary keys of *rows* that *lookup*'s scoped queryset reads."""
    queryset = lookup.queryset.using(_instance_db(rows[0])).prefetch_related(None)
    found = queryset.filter(pk__in={row.pk for row in rows}).values_list(
        "pk", flat=True
    )
    return set(_listed(found))


def _scoped_related_queryset(
    backend: Any, instance: Any, field_name: str, info: Any
) -> Any:
    """Return the related rows for *field_name*, with row scoping applied.

    The optimizer scopes relations when it builds the prefetch, but a resolver
    that returns materialized rows never gives it a query to work on. Reading
    the relation off the instance would then skip the related type's
    ``scope_rows`` entirely and hand back rows the caller may not read, so the
    scope is applied here instead.

    A cache is returned only when this library filled it after applying
    ``scope_rows``. A ``prefetch_related`` in the resolver is re-queried.
    """
    from strawberry_orm.lazy_resolution import (
        _django_relation_prefetched,
        relation_cache_is_scoped,
    )

    manager = getattr(instance, field_name)
    restrict = backend.relation_scope(type(instance), field_name, info)
    prefetched = _django_relation_prefetched(instance, field_name)
    if prefetched and (
        restrict is None
        or relation_cache_is_scoped(
            instance,
            field_name,
            scope=backend._field_scope(type(instance), field_name, info),
            held=_held,
        )
    ):
        return manager.all()
    if prefetched and restrict is not None:
        cache = getattr(instance, "_prefetched_objects_cache", None)
        if isinstance(cache, dict):
            cache.pop(field_name, None)

    queryset = manager.all()
    return queryset if restrict is None else restrict(queryset, info)


def _scoped_related_instance(
    backend: Any, instance: Any, field_name: str, info: Any
) -> Any:
    """Return the related row for a to-one *field_name*, with scoping applied.

    A scoped-out row reads as absent, matching what the optimizer produces
    when its scoped load finds nothing on the other end.
    """
    from strawberry_orm.lazy_resolution import (
        _django_relation_prefetched,
        relation_cache_is_scoped,
    )

    restrict = backend.relation_scope(type(instance), field_name, info)
    if restrict is None:
        return getattr(instance, field_name)

    # With a scope in play a removed row has to read as absent. The scoped
    # eager load leaves the cache empty, and Django's forward descriptor
    # raises rather than returning None for a non-nullable column. A cache
    # this library did not fill is not that load: re-check the primary key.
    if _django_relation_prefetched(instance, field_name):
        if relation_cache_is_scoped(
            instance,
            field_name,
            scope=backend._field_scope(type(instance), field_name, info),
            held=_held,
        ):
            return getattr(instance, field_name, None)
        related = getattr(instance, field_name, None)
        if related is None:
            return None
        pk = getattr(related, "pk", None)
        if pk is None:
            # No key to re-check. Hiding the row is the safe result: a
            # scope_rows hook cannot see an unsaved instance.
            return None
        queryset = type(related)._default_manager.filter(pk=pk)
        return restrict(queryset, info).first()

    related = getattr(instance, field_name, None)
    if related is None:
        return None
    queryset = type(related)._default_manager.filter(pk=related.pk)
    return restrict(queryset, info).first()


def _dedupe_lookups(lookups: list[Any]) -> list[Any]:
    """Collapse repeated lookups for one path, keeping the scoped one.

    The same relation can be reached twice - two aliases of one field, or a
    field both selected and named in ``using=`` - and Django rejects a repeated
    path when either occurrence carries a queryset. Which duplicate survives
    matters: only the ``Prefetch`` carries the row scope, so a bare path must
    never be allowed to shadow it.
    """
    position: dict[str, int] = {}
    unique: list[Any] = []
    for lookup in lookups:
        path = (
            lookup if isinstance(lookup, str) else getattr(lookup, "prefetch_to", None)
        )
        if path is None:
            unique.append(lookup)
            continue
        if path not in position:
            position[path] = len(unique)
            unique.append(lookup)
            continue
        kept = unique[position[path]]
        if (
            getattr(kept, "queryset", None) is None
            and getattr(lookup, "queryset", None) is not None
        ):
            unique[position[path]] = lookup
    return unique


def _is_parent_predicate(
    child: Any, parent_pk: Any, base_alias: str | None = None
) -> bool:
    """True when *child* is ``<fk column> = <parent pk>``.

    The column must be a relation, otherwise a boolean column compared to
    ``True`` would match a parent whose primary key is ``1``. When *base_alias*
    is given the column must also belong to the queried table, so a predicate
    reached through a join is not mistaken for the row's own key.
    """
    if getattr(child, "lookup_name", None) != "exact":
        return False
    lhs = getattr(child, "lhs", None)
    target = getattr(lhs, "target", None)
    if target is None or not getattr(target, "is_relation", False):
        return False
    if base_alias is not None and getattr(lhs, "alias", None) != base_alias:
        return False
    rhs = child.rhs
    value = getattr(rhs, "pk", rhs)
    return bool(value == parent_pk)


def _references_parent(node: Any, parent_pk: Any) -> bool:
    from django.db.models.sql.where import WhereNode

    for child in node.children:
        if isinstance(child, WhereNode):
            if _references_parent(child, parent_pk):
                return True
        elif _is_parent_predicate(child, parent_pk):
            return True
    return False


def _make_dj_rel_resolver(
    backend: Any,
    fname: str,
    rel_model: type,
    filter_type: Any,
    order_type: Any,
) -> Any:
    """Create a Strawberry field for a Django many-relation with filter/order."""
    info_type = strawberry.types.Info

    def _resolve(
        self: Any,
        info: Any,
        filter: Any = None,
        order: Any = None,
    ) -> list[Any]:
        if filter is None and order is None:
            with _read_errors(_instance_db(self)):
                return list(_scoped_related_queryset(backend, self, fname, info))

        # Filtering or ordering re-runs the query, so a prefetched result
        # cannot carry the scope through - it has to be reapplied here, and
        # the relations below loaded onto the new rows, ``using=`` paths
        # included, or a field reading one would read it unscoped.
        restrict = backend.relation_scope(type(self), fname, info)
        qs = getattr(self, fname).all()
        if restrict is not None:
            qs = restrict(qs, info)
        if filter is not None:
            qs = backend.apply_filters(qs, filter, rel_model, info=info)
        if order is not None:
            qs = backend.apply_ordering(qs, order, rel_model, info=info)
        return backend._read_with_relations(backend._store, qs, info)

    if filter_type and order_type:

        def resolver(
            self: Any,
            info: Any,
            filter: Any = None,
            order: Any = None,
        ) -> list[Any]:
            return _resolve(self, info, filter=filter, order=order)

        resolver.__annotations__ = {
            "info": info_type,
            "filter": filter_type | None,
            "order": list[order_type] | None,
        }
    elif filter_type:

        def resolver(self: Any, info: Any, filter: Any = None) -> list[Any]:
            return _resolve(self, info, filter=filter, order=None)

        resolver.__annotations__ = {
            "info": info_type,
            "filter": filter_type | None,
        }
    else:

        def resolver(self: Any, info: Any, order: Any = None) -> list[Any]:
            return _resolve(self, info, filter=None, order=order)

        resolver.__annotations__ = {
            "info": info_type,
            "order": list[order_type] | None,
        }

    reads_relation(resolver, fname)
    if getattr(backend, "_django_async_safe", False):
        resolver = async_safe_resolver(
            resolver, in_memory=_relation_in_memory(backend, fname)
        )
    return strawberry.field(resolver=resolver)


def _resolver_instance_and_info(args: tuple, kwargs: dict) -> tuple[Any, Any]:
    instance = args[0] if args else kwargs.get("self")
    info = kwargs.get("info", args[1] if len(args) > 1 else None)
    return instance, info


def _relation_in_memory(backend: Any, field_name: str) -> Any:
    """Whether a relation resolver would only read a cache this library scoped."""
    from strawberry_orm.lazy_resolution import (
        _django_relation_prefetched,
        relation_cache_is_scoped,
    )

    def check(*args: Any, **kwargs: Any) -> bool:
        if kwargs.get("filter") is not None or kwargs.get("order") is not None:
            return False
        instance, info = _resolver_instance_and_info(args, kwargs)
        if instance is None or not _django_relation_prefetched(instance, field_name):
            return False
        return (
            relation_cache_is_scoped(
                instance,
                field_name,
                scope=backend._field_scope(type(instance), field_name, info),
                held=_held,
            )
            or backend.relation_scope(type(instance), field_name, info) is None
        )

    return check


def _relay_id_in_memory(model: type, graphql_type: Any) -> Any:
    """Whether the column Relay's default ``resolve_id`` reads is loaded.

    The column is only known once the type's annotations resolve, which a
    forward reference can put off until the schema is built.
    """
    checks: list[Any] = []

    def check(root: Any, *args: Any, **kwargs: Any) -> bool:
        if not checks:
            checks.append(_column_in_memory(model, graphql_type.resolve_id_attr()))
        return checks[0] is not None and checks[0](root)

    return check


def _column_attname(
    model: type, name: str, *, stored_as_read: bool = False
) -> str | None:
    """Where a plain column's value sits on the row, or ``None`` if *name* is not one.

    ``stored_as_read`` also requires the stored value to be what reading the
    attribute returns; a file field, for one, wraps it on the way out.
    """
    from django.db.models.query_utils import DeferredAttribute

    try:
        field = model._meta.get_field(name)  # type: ignore[attr-defined]
    except Exception:
        return None
    if not getattr(field, "concrete", False) or field.is_relation:
        return None
    if stored_as_read and field.descriptor_class is not DeferredAttribute:
        return None
    return field.attname


def _column_descriptor(cls: type, attname: str) -> Any:
    """What reading *attname* off a *cls* row goes through.

    A proxy or subclass can put a property over a column, and the row's class
    need not be the model the type was declared on.
    """
    from inspect import getattr_static

    return getattr_static(cls, attname, None)


def _column_in_memory(model: type, name: str) -> Any:
    """Whether a plain column is loaded on the row, so reading it runs no query."""
    from django.db.models.query_utils import DeferredAttribute

    attname = _column_attname(model, name)
    if attname is None:
        return None
    by_class: dict[type, bool] = {}

    def check(root: Any, *args: Any, **kwargs: Any) -> bool:
        cls = type(root)
        through_django = by_class.get(cls)
        if through_django is None:
            # Django's descriptor reads a loaded column without a query,
            # whatever a subclass of it does to the value on the way out.
            through_django = by_class[cls] = isinstance(
                _column_descriptor(cls, attname), DeferredAttribute
            )
        return through_django and attname in root.__dict__

    return check


_COLUMN_FIELD_CLASSES: dict[type, type] = {}


def _column_field_class(base: type) -> type:
    """*base*, reading a loaded column straight off the row.

    A deferred column would query on read, which async code may not do on the
    event loop, so that read goes through the async-safe resolver instead.
    """
    from django.db.models.query_utils import DeferredAttribute

    cls = _COLUMN_FIELD_CLASSES.get(base)
    if cls is None:

        def get_result(
            self: Any, source: Any, info: Any, args: list[Any], kwargs: Any
        ) -> Any:
            row_class = type(source)
            plain = self._orm_plain_reads.get(row_class)
            if plain is None:
                plain = self._orm_plain_reads[row_class] = (
                    type(_column_descriptor(row_class, self._orm_attname))
                    is DeferredAttribute
                )
            if plain:
                try:
                    return source.__dict__[self._orm_attname]
                except KeyError:
                    pass
            return self._orm_unloaded_read(source)

        cls = type(base.__name__, (base,), {"get_result": get_result})
        _COLUMN_FIELD_CLASSES[base] = cls
    return cls


class _DjangoUpsertOps:
    """The calls :class:`~strawberry_orm.upserts.UpsertWriter` makes.

    Every call is synchronous; under async the whole write already runs in one
    ``sync_to_async`` call.
    """

    def __init__(self, info: Any) -> None:
        self._info = info
        #: The tables written on each database; ``None`` once a row was
        #: deleted, which can leave its key behind in any table.
        self._written: dict[str, set[str] | None] = {}

    # -- Repo-backed reads and writes ----------------------------------------

    def find(self, repo: Any, model: type, query: dict[str, Any]) -> Any:
        return repo.find_for_upsert(model, query, self._info)

    def get(self, repo: Any, model: type, pk: Any) -> Any:
        return repo._get(model, pk, self._info)

    def holders(self, row: Any, models: list[type]) -> list[type]:
        """Those of *models*, multi-table children of *row*'s model, holding *row*."""
        parent = type(row)._meta.concrete_model
        return [
            model
            for model in models
            if model._base_manager.using(row._state.db)
            .filter(**{_parent_link(model, parent): row.pk})
            .exists()
        ]

    def create(self, repo: Any, model: type, data: dict[str, Any]) -> Any:
        instance = repo._create(model, data, self._info)
        self._wrote(instance)
        return instance

    def save(self, repo: Any, instance: Any) -> None:
        repo._save(instance, self._info)
        self._wrote(instance)

    def delete(self, repo: Any, instance: Any) -> None:
        self._written[_written_db(instance)] = None
        repo._delete(instance, self._info)

    def check_constraints(self) -> None:
        """Check the foreign keys the database would check as it commits.

        Django creates them deferred on SQLite and PostgreSQL, where a key
        naming no row is refused only by the outermost commit.
        """
        from django.db import connections

        for using, tables in self._written.items():
            connection = connections[using]
            if connection.features.can_defer_constraint_checks:
                connection.check_constraints(None if tables is None else sorted(tables))

    def _wrote(self, instance: Any) -> None:
        tables = self._written.setdefault(_written_db(instance), set())
        if tables is not None:
            meta = instance._meta
            tables.update(
                model._meta.db_table for model in (meta.model, *meta.get_parent_list())
            )

    # -- Relations -----------------------------------------------------------

    def load_attr(self, instance: Any, name: str) -> Any:
        return getattr(instance, name)

    def init_collection(self, instance: Any, name: str) -> None:
        return None

    def link(self, parent: Any, spec: Any, child: Any) -> None:
        if spec.relation_mode == "reverse_fk" and self._is_child(parent, spec, child):
            return
        getattr(parent, spec.name).add(child)

    def unlink(self, parent: Any, spec: Any, child: Any) -> None:
        getattr(parent, spec.name).remove(child)

    def is_member(self, parent: Any, spec: Any, child: Any) -> bool:
        if spec.relation_mode == "reverse_fk":
            return self._is_child(parent, spec, child)
        return getattr(parent, spec.name).filter(pk=child.pk).exists()

    def previous_parent(self, parent: Any, spec: Any, child: Any) -> Any:
        """The other row whose reverse foreign key relation holds *child*, if any."""
        if spec.relation_mode != "reverse_fk" or self._is_child(parent, spec, child):
            return None
        return getattr(child, spec.remote_attr)

    def back_reference(
        self, parent_model: type, spec: Any
    ) -> tuple[str | None, str | None]:
        """The attribute and foreign key column a nested child points back with."""
        return spec.remote_attr, self._foreign_key(spec).attname

    def parent_scope(self, parent: Any, spec: Any) -> dict[str, Any]:
        if spec.relation_mode != "reverse_fk":
            return {}
        foreign_key = self._foreign_key(spec)
        return {foreign_key.attname: getattr(parent, foreign_key.target_field.attname)}

    def parent_assignment(self, parent: Any, spec: Any) -> dict[str, Any]:
        if spec.relation_mode != "reverse_fk":
            return {}
        return {spec.remote_attr: parent}

    # -- Keys ----------------------------------------------------------------

    def pk_value(self, instance: Any) -> Any:
        return instance.pk

    def coerce_pk(self, model: type, value: Any) -> Any:
        from django.core.exceptions import ValidationError

        from strawberry_orm.upserts import UpsertValidationError

        if not isinstance(value, str):
            return value
        try:
            return key_in_range(model._meta.pk.to_python(value))  # type: ignore[attr-defined]
        except (ValidationError, ValueError):
            raise UpsertValidationError(f"{value!r} is not a valid id") from None

    # -- Internals -----------------------------------------------------------

    @staticmethod
    def _foreign_key(spec: Any) -> Any:
        return spec.related_model._meta.get_field(spec.remote_attr)

    def _is_child(self, parent: Any, spec: Any, child: Any) -> bool:
        return all(
            getattr(child, child_key) == value
            for child_key, value in self.parent_scope(parent, spec).items()
        )


# ---------------------------------------------------------------------------
# Filter translation
# ---------------------------------------------------------------------------

_LOOKUP_TO_DJANGO: dict[str, str] = {
    "exact": "exact",
    "neq": "exact",  # handled with exclude
    "gt": "gt",
    "gte": "gte",
    "lt": "lt",
    "lte": "lte",
    "contains": "contains",
    "i_contains": "icontains",
    "starts_with": "startswith",
    "i_starts_with": "istartswith",
    "ends_with": "endswith",
    "i_ends_with": "iendswith",
    "regex": "regex",
    "i_regex": "iregex",
}


def _django_reference_key(model: type, rel_name: str) -> tuple[str, str] | None:
    """The parent's foreign key column and the related key it holds.

    Only a forward foreign key to the related primary key: the key is then on
    the parent's own row, and comparing it needs no join.
    """
    from django.db.models.fields.related import ForeignKey, OneToOneField

    try:
        field = model._meta.get_field(rel_name)  # type: ignore[attr-defined]
    except Exception:
        return None
    if (
        isinstance(field, (ForeignKey, OneToOneField))
        and field.target_field.primary_key
    ):
        return field.attname, field.target_field.name
    return None


def _parent_link(model: type, parent: type) -> str:
    """The lookup from a multi-table *model* to the key of *parent*, an ancestor."""
    return "__".join(
        path.join_field.name
        for path in model._meta.get_path_to_parent(parent)  # type: ignore[attr-defined]
    )


def _django_to_many_link(model: type | None, rel_name: str) -> str | None:
    """How the far side of a to-many *rel_name* names its way back to *model*."""
    if model is None:
        return None
    try:
        field = model._meta.get_field(rel_name)  # type: ignore[attr-defined]
    except Exception:
        return None
    if _relation_kind(field) != "many":
        return None
    return field.remote_field.name


def _django_related_model(model: type, rel_name: str) -> type | None:
    from django.db.models.fields.related import ForeignKey, OneToOneField

    try:
        field = model._meta.get_field(rel_name)  # type: ignore[attr-defined]
    except Exception:
        return None
    if isinstance(field, (ForeignKey, OneToOneField)):
        return field.remote_field.model
    return None


def _django_traversal_model(model: type | None, rel_name: str) -> type | None:
    """Model reached by *rel_name*, including reverse and m2m relations.

    ``_django_related_model`` deliberately covers only forward keys, which the
    foreign-key shortcut needs. Traversal has to keep following reverse
    relations too, or the far side is left unscoped.
    """
    if model is None:
        return None
    try:
        field = model._meta.get_field(rel_name)  # type: ignore[attr-defined]
    except Exception:
        return None
    return getattr(field, "related_model", None)


def _django_relation_scope(
    backend: Any, model: type | None, relation: str, info: Any
) -> Any | None:
    """The rows of *relation*'s far side its scoping allows, or ``None``.

    Covers both the related type's ``scope_rows`` and any ``scope=`` on this
    edge, so a filter cannot reach rows the read path hides.
    """
    if backend is None or model is None:
        return None
    get_qs = backend.relation_scope(model, relation, info)
    if get_qs is None:
        return None
    related = backend._relation_target_model(model, relation)
    return get_qs(related._default_manager.all(), info)


def _django_custom_filter_q(
    handler: Any, filter_input: Any, model: type, value: Any, info: Any, prefix: str
) -> Any:
    """A ``@filter_field`` as a condition on rows of *model*.

    The handler narrows a query of its own over *model*, and the rows it
    keeps are matched by key, so the condition can be negated, made one
    alternative of several, or tested on a related row.
    """
    from django.db.models import Q

    narrowed = invoke_custom_callback(
        handler,
        filter_input,
        query=model._base_manager.all(),  # type: ignore[attr-defined]
        value=value,
        info=info,
    )
    return Q(**{f"{prefix}pk__in": narrowed.values("pk")})


def _build_django_filter(
    filter_input: Any,
    *,
    query: Any = None,
    model: type | None = None,
    info: Any = None,
    backend: Any = None,
    max_depth: int = 10,
    max_branches: int = 50,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
    _depth: int = 0,
    _prefix: str = "",
    _on_query: bool = True,
) -> tuple[Any, Any]:
    """Return ``(Q_clause | None, query)``.

    A ``@filter_field`` narrows *query* itself only while the filter so far
    is a plain AND at the top (``_on_query``). Anywhere else - under
    ``not:``, ``any:``, ``oneOf:`` or ``object:`` - it becomes a clause.
    """
    from django.db.models import Q

    if _depth > max_depth:
        raise ValueError(f"Filter nesting exceeds maximum depth of {max_depth}")

    if filter_input is None or filter_input is strawberry.UNSET:
        return None, query

    fields = filter_input.__class__.__dataclass_fields__
    custom_filters = getattr(type(filter_input), "_custom_filters", {})
    custom_filter_keys = frozenset(custom_filters.keys())
    recurse_kw = dict(
        query=query,
        model=model,
        info=info,
        backend=backend,
        max_depth=max_depth,
        max_branches=max_branches,
        enable_regex=enable_regex,
        max_in_list_size=max_in_list_size,
        _depth=_depth + 1,
        _prefix=_prefix,
        _on_query=False,
    )

    for key in fields:
        val = getattr(filter_input, key)
        if val is strawberry.UNSET or val is None:
            continue

        if key == "is_null":
            if not _prefix:
                from strawberry_orm.backends._base import (
                    _FILTER_RELATION_PRESENCE_ERROR,
                )

                raise ValueError(_FILTER_RELATION_PRESENCE_ERROR)
            return Q(**{f"{_prefix}isnull": val}), query
        elif key == "field":
            clause = _build_django_field_clause(
                val,
                prefix=_prefix,
                enable_regex=enable_regex,
                max_in_list_size=max_in_list_size,
            )
            return clause, query
        elif key == "object":
            obj_fields = val.__class__.__dataclass_fields__
            for rel_name in obj_fields:
                nested_filter = getattr(val, rel_name)
                if nested_filter is strawberry.UNSET or nested_filter is None:
                    continue
                rel_model = _django_traversal_model(model, rel_name)
                link = _django_to_many_link(model, rel_name)
                # Traversal joins straight to the related table, so the related
                # type's row scoping has to be re-applied or it is bypassed.
                scoped = _django_relation_scope(backend, model, rel_name, info)
                scope_q = (
                    None
                    if scoped is None
                    else Q(**{f"{_prefix}{rel_name}__in": scoped})
                )
                is_null_val = getattr(nested_filter, "is_null", strawberry.UNSET)
                if is_null_val is not strawberry.UNSET and is_null_val is not None:
                    if scope_q is not None:
                        # "Has no related row" has to mean "none the caller can see".
                        return (~scope_q if is_null_val else scope_q), query
                    if link is not None:
                        return Q(**{f"{_prefix}{rel_name}__isnull": is_null_val}), query
                reference = (
                    _django_reference_key(model, rel_name)
                    if model is not None
                    else None
                )
                if reference is not None:
                    fk_attname, key_name = reference
                    fk_clause = build_reference_object_filter_clause(
                        nested_filter,
                        key=key_name,
                        build_field_clause=_build_django_reference_field_clause,
                        custom_filter_keys=custom_filter_keys,
                        max_branches=max_branches,
                        max_depth=max_depth,
                        depth=_depth + 1,
                        fk_prefix=f"{_prefix}{fk_attname}",
                        enable_regex=enable_regex,
                        max_in_list_size=max_in_list_size,
                    )
                    if fk_clause is not None:
                        if scope_q is not None:
                            fk_clause = scope_q & fk_clause
                        return fk_clause, query
                with past_a_hop():
                    nested_clause, query = _build_django_filter(
                        nested_filter,
                        query=query,
                        model=rel_model,
                        info=info,
                        backend=backend,
                        max_depth=max_depth,
                        max_branches=max_branches,
                        enable_regex=enable_regex,
                        max_in_list_size=max_in_list_size,
                        _depth=_depth + 1,
                        _prefix="" if link is not None else f"{_prefix}{rel_name}__",
                        _on_query=False,
                    )
                if nested_clause is None or link is None:
                    if nested_clause is not None and scope_q is not None:
                        nested_clause = scope_q & nested_clause
                    return nested_clause, query
                # One correlated condition per parent, so a negation negates
                # "a readable related row matches" rather than each part of it.
                from django.db.models import Exists, OuterRef

                conditions = [nested_clause]
                if scoped is not None:
                    conditions.insert(0, Q(pk__in=scoped))
                related = rel_model._base_manager.filter(  # type: ignore[union-attr]
                    *conditions, **{f"{link}__pk": OuterRef(f"{_prefix}pk")}
                )
                return Exists(related), query
        elif key == "all":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            combined = Q()
            has_clause = False
            for f in val:
                sub_clause, query = _build_django_filter(
                    f, **{**recurse_kw, "query": query, "_on_query": _on_query}
                )
                if sub_clause is not None:
                    combined &= sub_clause
                    has_clause = True
            return (combined if has_clause else None), query
        elif key == "any":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            combined = Q()
            has_clause = False
            for _i, f in enumerate(val):
                sub_clause, query = _build_django_filter(
                    f, **{**recurse_kw, "query": query}
                )
                if sub_clause is not None:
                    combined = sub_clause if not has_clause else combined | sub_clause
                    has_clause = True
            return (combined if has_clause else None), query
        elif key == "not_":
            inner, query = _build_django_filter(val, **{**recurse_kw, "query": query})
            return (~inner if inner is not None else None), query
        elif key == "one_of":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            combined = Q()
            has_clause = False
            for _i, f in enumerate(val):
                sub_clause, query = _build_django_filter(
                    f, **{**recurse_kw, "query": query}
                )
                if sub_clause is not None:
                    combined = sub_clause if not has_clause else combined | sub_clause
                    has_clause = True
            return (combined if has_clause else None), query
        elif key in custom_filters:
            if not _on_query:
                clause = _django_custom_filter_q(
                    custom_filters[key], filter_input, model, val, info, _prefix
                )
                return clause, query
            query = invoke_custom_callback(
                custom_filters[key],
                filter_input,
                query=query,
                value=val,
                info=info,
            )
            return None, query

    return None, query


def _build_django_reference_lookup(
    col_name: str,
    lookup: Any,
    *,
    max_in_list_size: int = 500,
) -> Any:
    from django.db.models import Q

    if not is_reference_lookup(lookup):
        raise TypeError("Expected ReferenceLookup for FK / reference filtering.")

    q = Q()
    fields = lookup.__class__.__dataclass_fields__

    for op_name in fields:
        val = getattr(lookup, op_name)
        if val is strawberry.UNSET or val is None:
            continue

        if op_name == "is_null":
            q &= Q(**{f"{col_name}__isnull": val})
        elif op_name in ("in_list", "not_in_list"):
            if len(val) > max_in_list_size:
                raise ValueError(
                    f"in_list/not_in_list has {len(val)} items; "
                    f"maximum is {max_in_list_size}"
                )
            if op_name == "in_list":
                q &= Q(**{f"{col_name}__in": val})
            else:
                q &= ~Q(**{f"{col_name}__in": val})
        elif op_name == "neq":
            q &= ~Q(**{f"{col_name}__exact": val})
        elif op_name == "exact":
            q &= Q(**{f"{col_name}__exact": val})

    return q


def _build_django_reference_field_clause(
    field_input: Any,
    *,
    fk_prefix: str,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> Any:
    from django.db.models import Q

    q = Q()
    fields = field_input.__class__.__dataclass_fields__

    for col_name in fields:
        lookup = getattr(field_input, col_name)
        if lookup is strawberry.UNSET or lookup is None:
            continue
        if is_reference_lookup(lookup):
            q &= _build_django_reference_lookup(
                fk_prefix,
                lookup,
                max_in_list_size=max_in_list_size,
            )
        elif is_fk_shortcut_lookup(lookup):
            q &= _build_django_lookup(
                fk_prefix,
                lookup,
                enable_regex=enable_regex,
                max_in_list_size=max_in_list_size,
            )
        else:
            raise TypeError(
                f"Expected ReferenceLookup or FK-mappable IntComparisonLookup "
                f"for reference field '{col_name}'."
            )

    return q


def _build_django_field_clause(
    field_input: Any,
    *,
    prefix: str = "",
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> Any:
    from django.db.models import Q

    q = Q()
    fields = field_input.__class__.__dataclass_fields__

    for col_name in fields:
        lookup = getattr(field_input, col_name)
        if lookup is strawberry.UNSET or lookup is None:
            continue

        col_q = _build_django_lookup(
            f"{prefix}{col_name}",
            lookup,
            enable_regex=enable_regex,
            max_in_list_size=max_in_list_size,
        )
        q &= col_q

    return q


def _build_django_lookup(
    col_name: str,
    lookup: Any,
    *,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> Any:
    if is_reference_lookup(lookup):
        return _build_django_reference_lookup(
            col_name,
            lookup,
            max_in_list_size=max_in_list_size,
        )

    from django.db.models import Q

    q = Q()
    fields = lookup.__class__.__dataclass_fields__

    for op_name in fields:
        val = getattr(lookup, op_name)
        if val is strawberry.UNSET or val is None:
            continue

        if op_name == "is_null":
            q &= Q(**{f"{col_name}__isnull": val})
        elif op_name in ("in_list", "not_in_list"):
            if len(val) > max_in_list_size:
                raise ValueError(
                    f"in_list/not_in_list has {len(val)} items; "
                    f"maximum is {max_in_list_size}"
                )
            if op_name == "in_list":
                q &= Q(**{f"{col_name}__in": val})
            else:
                q &= ~Q(**{f"{col_name}__in": val})
        elif op_name == "range":
            q &= Q(**{f"{col_name}__range": (val.start, val.end)})
        elif op_name == "neq":
            q &= ~Q(**{f"{col_name}__exact": val})
        elif op_name in ("regex", "i_regex"):
            if not enable_regex:
                raise ValueError(
                    "Regex filters are disabled. Pass enable_regex_filters=True "
                    "to enable."
                )
            check_regex(val)
            django_lookup = _LOOKUP_TO_DJANGO[op_name]
            q &= Q(**{f"{col_name}__{django_lookup}": val})
        elif op_name in _LOOKUP_TO_DJANGO:
            django_lookup = _LOOKUP_TO_DJANGO[op_name]
            q &= Q(**{f"{col_name}__{django_lookup}": val})

    return q


# ---------------------------------------------------------------------------
# Ordering translation
# ---------------------------------------------------------------------------


def _build_django_ordering(
    order_input: Any,
    _prefix: str = "",
    *,
    query: Any = None,
    info: Any = None,
    model: type | None = None,
    backend: Any = None,
) -> tuple[list[Any], Any]:
    """Return ``(order_clauses, query)``."""
    clauses: list[Any] = []
    fields = order_input.__class__.__dataclass_fields__
    custom_orders = getattr(type(order_input), "_custom_orders", {})

    for key in fields:
        val = getattr(order_input, key)
        if val is strawberry.UNSET or val is None:
            continue

        if key == "field":
            clauses.extend(_build_django_order_field(val, _prefix))
        elif key == "object":
            obj_fields = val.__class__.__dataclass_fields__
            for rel_name in obj_fields:
                nested = getattr(val, rel_name)
                if nested is strawberry.UNSET or nested is None:
                    continue
                if backend is not None and model is not None:
                    backend.reject_scoped_order_traversal(
                        model, rel_name, type(order_input)
                    )
                sub_clauses, query = _build_django_ordering(
                    nested,
                    _prefix=f"{_prefix}{rel_name}__",
                    query=query,
                    info=info,
                    model=_django_traversal_model(model, rel_name),
                    backend=backend,
                )
                clauses.extend(sub_clauses)
        elif key in custom_orders:
            query = invoke_custom_callback(
                custom_orders[key],
                order_input,
                query=query,
                value=val,
                info=info,
            )

    return clauses, query


def _build_django_order_field(field_input: Any, prefix: str = "") -> list[Any]:
    from django.db.models import F

    clauses: list[Any] = []
    fields = field_input.__class__.__dataclass_fields__

    for col_name in fields:
        direction = getattr(field_input, col_name)
        if direction is strawberry.UNSET or direction is None:
            continue

        dir_value = direction.value if hasattr(direction, "value") else str(direction)

        nulls_first = True if "NULLS_FIRST" in dir_value else None
        nulls_last = True if "NULLS_LAST" in dir_value else None

        if dir_value.startswith("DESC"):
            expr = F(f"{prefix}{col_name}").desc(
                nulls_first=nulls_first, nulls_last=nulls_last
            )
        else:
            expr = F(f"{prefix}{col_name}").asc(
                nulls_first=nulls_first, nulls_last=nulls_last
            )
        clauses.append(expr)

    return clauses


# ---------------------------------------------------------------------------
# Grouping helpers
# ---------------------------------------------------------------------------


def _own_rows(query: Any) -> Any:
    """*query*'s rows, once each, read through a query with no joins of its own.

    A filter across a to-many relation joins it, repeating a row once for
    every related row that matched. Counted, summed or grouped as it stands,
    each repeat would count again.
    """
    inner = query if query.query.is_sliced else query.order_by()
    return query.model._base_manager.filter(pk__in=inner.values("pk"))


def _django_aggregates(requested: dict[str, Any]) -> dict[str, Any]:
    from django.db.models import Avg, Count, Max, Min, Sum

    aggregates: dict[str, Any] = {}
    if requested.get("count"):
        aggregates["_count"] = Count("*")
    for func_name, aggregate in {
        "sum": Sum,
        "avg": Avg,
        "median": _Median,
        "min": Min,
        "max": Max,
    }.items():
        for fname in requested.get(func_name, []):
            aggregates[f"_{func_name}_{fname}"] = aggregate(fname)
    return aggregates


class _Median(Aggregate):
    """The median of a column, which PostgreSQL alone can work out."""

    function = "PERCENTILE_CONT"
    name = "Median"
    template = "%(function)s(0.5) WITHIN GROUP (ORDER BY %(expressions)s)"
    output_field = FloatField()

    def as_sql(self, compiler: Any, connection: Any, **extra: Any) -> Any:
        if connection.vendor != "postgresql":
            raise ValueError(
                f"median is supported on PostgreSQL, not {connection.vendor}."
            )
        return super().as_sql(compiler, connection, **extra)


class _Together(models.Subquery):
    """The rows of a correlated subquery as one JSON array, ``[]`` for none.

    The subquery's columns, *names*, are put in an array per row. It hands
    rows over distinct and sorted, so a set of rows reads as the same text
    wherever it is read. PostgreSQL sorts again inside the aggregate, which
    alone it promises to keep; SQLite keeps the order of an ordered subquery.
    """

    def __init__(self, queryset: Any, names: list[str]) -> None:
        super().__init__(queryset, output_field=models.TextField())
        self.names = names

    def as_sql(self, compiler: Any, connection: Any, *args: Any, **extra: Any) -> Any:
        rows, params = super().as_sql(compiler, connection, *args, **extra)
        columns = ", ".join(
            f"_orm_t.{connection.ops.quote_name(name)}" for name in self.names
        )
        if connection.vendor == "sqlite":
            together = f"json_group_array(json_array({columns}))"
        elif connection.vendor == "postgresql":
            together = f"json_agg(json_build_array({columns}) ORDER BY {columns})::text"
        else:
            raise ValueError(
                f"Grouping or ordering by objects: is supported on PostgreSQL "
                f"and SQLite, not {connection.vendor}."
            )
        return f"COALESCE((SELECT {together} FROM {rows} _orm_t), '[]')", params


def _truncate(expr: Any, field: Any, interval: Any) -> Any:
    """*expr* cut down to the start of its *interval*.

    A datetime is cut in the current time zone, so a day or month begins
    where the caller's does rather than where UTC's does.
    """
    from django.db.models import DateField, DateTimeField
    from django.db.models.functions import Trunc

    output = DateTimeField() if isinstance(field, DateTimeField) else DateField()
    return Trunc(expr, interval.value.lower(), output_field=output)


def _remote_key(hop: Any) -> str:
    """The lookup from *hop*'s target to the primary key of the row it left.

    Through the related row's key, not the link's own column: a foreign key
    with ``to_field`` holds another column of that row.
    """
    from django.db.models import ForeignObjectRel

    field = hop.model._meta.get_field(hop.relation)
    if isinstance(field, ForeignObjectRel):
        return f"{field.field.name}__pk"
    return f"{field.related_query_name()}__pk"


def _keep_visible_links(
    hop: Any, visible: Any, markers: dict[str, Any], keep: list[Any]
) -> None:
    """Drop the rows a to-many join adds for related rows the caller cannot see.

    A row keeps one joined row per visible related row. A row with none keeps
    exactly one, whichever related row comes first, which then groups as
    ``None``; dropping them all would drop the row.
    """
    from django.db.models import Exists, F, OuterRef, Q, Subquery

    marker = f"_m_{hop.relation}"
    if marker in markers:
        return
    markers[marker] = F(f"{hop.relation}__pk")
    own = {_remote_key(hop): OuterRef("pk")}
    first = hop.target._base_manager.filter(**own).order_by("pk").values("pk")[:1]
    keep.append(
        Q(**{f"{marker}__in": visible.values("pk")})
        | Q(**{f"{marker}__isnull": True})
        | (~Exists(visible.filter(**own)) & Q(**{marker: Subquery(first)}))
    )


def _single_valued(model: type, name: str) -> bool:
    """True when *name* reaches one value per row of *model*.

    Ordering through a to-many relation would join it, repeating rows, and a
    name the model does not have, such as an annotation, cannot be read
    outside the query that made it.
    """
    from django.core.exceptions import FieldDoesNotExist

    parts = name.split("__")
    current = model
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "pk" and last:
            return True
        try:
            field = current._meta.get_field(part)  # type: ignore[attr-defined]
        except FieldDoesNotExist:
            return False
        if not field.is_relation:
            return last
        if _relation_kind(field) != "one" or field.related_model is None:
            return False
        current = field.related_model
    return True


def _rankable(model: type, expr: Any) -> Any:
    """*expr* as a value PostgreSQL has a ``MIN`` and ``MAX`` of, or ``None``.

    PostgreSQL has neither for a UUID nor a boolean. A UUID's text sorts as
    the UUID does, and a boolean's integer as the boolean does, false first.
    The set an ``objects:`` reads is text already.
    """
    from django.db.models.functions import Cast

    if isinstance(expr, _Together):
        return expr
    field = _ordered_field(model, expr.name)
    kinds = {field.get_internal_type(), *(cls.__name__ for cls in type(field).__mro__)}
    if "UUIDField" in kinds:
        return Cast(expr, models.TextField())
    if "BooleanField" in kinds:
        return Cast(expr, models.IntegerField())
    if kinds & _UNRANKABLE_FIELDS:
        return None
    return expr


def _ordered_field(model: type, name: str) -> Any:
    """The column *name* orders *model* by, through to-one relations."""
    field: Any = None
    current: Any = model
    for part in name.split("__"):
        field = current._meta.pk if part == "pk" else current._meta.get_field(part)
        if field.is_relation:
            current = field.related_model
    while field.is_relation:
        field = field.target_field
    return field


def _order_clauses(
    order_input: Any, model: type, *, backend: Any, prefix: str = ""
) -> list[Any]:
    """The column clauses of one order input, through ``object:`` too."""
    clauses: list[Any] = []
    for key in order_input.__class__.__dataclass_fields__:
        value = getattr(order_input, key)
        if value is strawberry.UNSET or value is None:
            continue
        if key == "field":
            clauses.extend(_build_django_order_field(value, prefix))
        elif key == "object":
            for relation in value.__class__.__dataclass_fields__:
                nested = getattr(value, relation)
                if nested is not strawberry.UNSET and nested is not None:
                    backend.reject_scoped_order_traversal(
                        model, relation, type(order_input)
                    )
                    clauses.extend(
                        _order_clauses(
                            nested,
                            backend._relation_target_model(model, relation),
                            backend=backend,
                            prefix=f"{prefix}{relation}__",
                        )
                    )
    return clauses


def _connection_ordering(
    query: Any, order_input: Any, *, backend: Any = None, info: Any = None
) -> list[Any]:
    """The order the connection reads its rows in, as ``OrderBy`` clauses.

    The client's ``order`` when given, else the query's own, else the model's.
    Only clauses that read one value per row are kept, the set of an
    ``objects:`` among them; the rest cannot rank groups, and a
    ``@order_field`` only knows how to order the query itself.
    """
    from django.db.models import F
    from django.db.models.expressions import OrderBy

    from strawberry_orm.grouping import SetOrder, order_terms

    model = query.model
    clauses: list[Any] = []
    if order_input:
        for term in order_terms(backend, model, order_input):
            if isinstance(term, SetOrder):
                clauses.append(backend._set_order(term, info))
            else:
                clauses.extend(_order_clauses(term, model, backend=backend))
    else:
        inner = query.query
        terms = inner.order_by or (
            model._meta.ordering if inner.default_ordering else ()
        )
        for term in terms:
            if isinstance(term, str):
                if term == "?":
                    continue
                name = term.lstrip("-+")
                clauses.append(F(name).desc() if term[0] == "-" else F(name).asc())
            elif isinstance(term, OrderBy):
                clauses.append(term)
            elif isinstance(term, F):
                clauses.append(term.asc())
    return [
        clause
        for clause in clauses
        if isinstance(clause.expression, _Together)
        or (
            isinstance(clause.expression, F)
            and _single_valued(model, clause.expression.name)
        )
    ]


def _group_ordering(
    clauses: list[Any], model: type
) -> tuple[dict[str, Any], list[Any]]:
    """Order groups as their first rows come in the connection's order.

    Read in that order, a group starts at its least value of each clause, or
    its greatest for a descending one, so groups are ranked by those. A
    clause on a column the database cannot take the least of is left out.
    """
    from django.db.models import F, Max, Min
    from django.db.models.expressions import OrderBy

    firsts: dict[str, Any] = {}
    ordering: list[Any] = []
    for clause in clauses:
        ranked = _rankable(model, clause.expression)
        if ranked is None:
            continue
        name = f"_o{len(firsts)}"
        firsts[name] = (Max if clause.descending else Min)(ranked)
        ordering.append(
            OrderBy(
                F(name),
                descending=clause.descending,
                nulls_first=clause.nulls_first,
                nulls_last=clause.nulls_last,
            )
        )
    return firsts, ordering


#: What a client is told when a read this library runs fails.
_READ_REFUSED = "Invalid filter expression"


@contextmanager
def _read_errors(using: str | None = None) -> Any:
    """Raise what a client may see in place of the database failing a read.

    Django's error names tables and columns, or the database host, so it is
    kept only as the cause, for the server log. Only an error from running the
    statement is the request's doing: one that leaves *using* with no open
    connection - refused, or dropped - is the connection failing.
    """
    from django.db import (
        DEFAULT_DB_ALIAS,
        Error,
        InterfaceError,
        OperationalError,
        connections,
    )

    try:
        with _refused_values(TypeError, AttributeError, OverflowError):
            yield
    except Error as exc:
        raw = connections[using or DEFAULT_DB_ALIAS].connection
        if isinstance(exc, InterfaceError) or (
            isinstance(exc, OperationalError)
            and (raw is None or getattr(raw, "closed", False))
        ):
            raise ConnectionError("Database connection failed") from exc
        raise ValueError(_READ_REFUSED) from exc


@contextmanager
def _refused_values(*errors: type[BaseException]) -> Any:
    """Raise what a client may see in place of Django refusing a filter value.

    Django checks a value against its column as a filter is added, saying what
    the column holds with its template unfilled (``“%(value)s” is not a valid
    UUID``); and some only as the statement compiles, failing with a Python
    error of its own, in *errors*.
    """
    from django.core.exceptions import ValidationError

    try:
        yield
    except (ValidationError, *errors) as exc:
        raise ValueError(_READ_REFUSED) from exc


def _instance_db(instance: Any) -> str | None:
    return getattr(getattr(instance, "_state", None), "db", None)


def _written_db(instance: Any) -> str:
    from django.db import router

    return instance._state.db or router.db_for_write(type(instance), instance=instance)


def _listed(query: Any) -> list[Any]:
    with _read_errors(query.db):
        return list(query)


def _counted(query: Any) -> int:
    with _read_errors(query.db):
        return query.count()


@contextmanager
def _write_errors() -> Any:
    """Raise what a client may see in place of the database refusing a write.

    Django's error names the table and column, and on PostgreSQL the row's
    values, so it is kept only as the cause, for the server log. The driver
    raises ``OverflowError`` for an integer its column type cannot hold.
    """
    from django.db import Error

    try:
        yield
    except (Error, OverflowError) as exc:
        raise ValueError(WRITE_REFUSED) from exc
