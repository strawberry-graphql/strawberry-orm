"""Tortoise ORM backend -- built from scratch (no existing strawberry integration)."""

from __future__ import annotations

import datetime
import importlib
import inspect
import re
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import strawberry
from strawberry.extensions import SchemaExtension
from tortoise.models import Model
from tortoise.queryset import QuerySet

from strawberry_orm.backends.filter_pk_shortcut import (
    build_reference_object_filter_clause,
)
from strawberry_orm.fields import call_scope
from strawberry_orm.filters import is_fk_shortcut_lookup, is_reference_lookup
from strawberry_orm.optimizer import OptimizerExtension

from ._base import (
    AggregateMeta,
    BaseBackend,
    extract_element_type,
    hint_hops,
    input_to_dict,
    invoke_custom_callback,
    requested_aggregates,
)

#: Where the window puts each row's number within its group. Dropped again
#: before the row is turned back into a model, which knows no such column.
_ROW_NUMBER_ALIAS = "_orm_rn"

#: Where the grouped count puts each group's total.
_COUNT_ALIAS = "_orm_count"

_TORTOISE_FIELD_MAP: dict[str, type] = {
    "IntField": int,
    "SmallIntField": int,
    "BigIntField": int,
    "FloatField": float,
    "DecimalField": Decimal,
    "CharField": str,
    "TextField": str,
    "BooleanField": bool,
    "DateField": datetime.date,
    "DatetimeField": datetime.datetime,
    "TimeField": datetime.time,
    "UUIDField": str,
    "JSONField": str,
}

_MANY_REL_TYPES = frozenset(
    {
        "BackwardFKRelation",
        "ManyToManyFieldInstance",
        "BackwardOneToOneRelation",
    }
)

_FORWARD_REL_TYPES = frozenset({"ForeignKeyFieldInstance", "OneToOneFieldInstance"})

_REL_FIELD_TYPES = _MANY_REL_TYPES | _FORWARD_REL_TYPES

#: Relations a row can have many of. A reverse one-to-one is in
#: ``_MANY_REL_TYPES`` for loading, but never repeats a row when joined.
_TO_MANY_REL_TYPES = frozenset({"BackwardFKRelation", "ManyToManyFieldInstance"})

_Ordering = tuple[str, bool, bool | None, bool | None]

# Tortoise querysets use ``__slots__`` without ``__weakref__``, so python-side
# orderings cannot be attached to the query or tracked with a WeakKeyDictionary.
# The entry therefore keeps the query alive (which pins its id) and is verified
# by identity on read: a recycled id must never inherit another query's order.
_QUERY_ORDERINGS: OrderedDict[int, tuple[Any, list[_Ordering]]] = OrderedDict()
_MAX_REMEMBERED_ORDERINGS = 1024


def _primary_key(value: Any) -> Any:
    return getattr(value, "id", getattr(value, "pk", None))


def _remember_query_ordering(
    query: Any,
    orderings: list[_Ordering],
) -> None:
    _QUERY_ORDERINGS[id(query)] = (query, orderings)
    _QUERY_ORDERINGS.move_to_end(id(query))
    while len(_QUERY_ORDERINGS) > _MAX_REMEMBERED_ORDERINGS:
        _QUERY_ORDERINGS.popitem(last=False)


def _query_orderings(
    query: Any,
) -> list[_Ordering] | None:
    entry = _QUERY_ORDERINGS.get(id(query))
    if entry is None or entry[0] is not query:
        return None
    return entry[1]


class _PlannedPrefetch:
    """One relation in a query's load plan, with the relations under it."""

    __slots__ = ("related_model", "forward", "many_to_many", "scope", "children")

    def __init__(self, related_model: type, field_cls: str, scope: Any) -> None:
        self.related_model = related_model
        self.forward = field_cls in _FORWARD_REL_TYPES
        self.many_to_many = field_cls == "ManyToManyFieldInstance"
        self.scope = scope
        self.children: dict[str, _PlannedPrefetch] = {}


def _prefetch_lookups(plan: dict[str, _PlannedPrefetch]) -> list[Any]:
    """Tortoise lookups for *plan*, nesting each level inside its parent's queryset.

    Nested paths such as ``posts__tags`` are spelled as ``Prefetch`` objects:
    Tortoise does not always apply the string form on reverse relations.
    Forward relations stay unscoped here, and a many-to-many relation's
    children are loaded afterwards, because Tortoise drops ``Prefetch``
    objects nested under one; see ``_finish_prefetches``.
    """
    from tortoise.query_utils import Prefetch

    lookups: list[Any] = []
    for name, load in plan.items():
        children = [] if load.many_to_many else _prefetch_lookups(load.children)
        scoped = load.scope is not None and not load.forward
        if not scoped and not children:
            lookups.append(name)
            continue
        queryset = load.related_model.all()
        if scoped:
            queryset = load.scope(queryset)
        if children:
            queryset = queryset.prefetch_related(*children)
        lookups.append(Prefetch(name, queryset))
    return lookups


async def _prefetch_onto(model: type, rows: list[Any], lookups: list[Any]) -> None:
    """Prefetch *lookups* onto rows already in memory.

    ``fetch_for_list`` only takes strings, so this runs the queryset's own
    prefetch step, which takes ``Prefetch`` objects too.
    """
    queryset = model.all().prefetch_related(*lookups)  # type: ignore[attr-defined]
    db = model._choose_db()  # type: ignore[attr-defined]
    executor = db.executor_class(
        model=model,
        db=db,
        prefetch_map=queryset._prefetch_map,
        prefetch_queries=queryset._prefetch_queries,
    )
    await executor._execute_prefetch_queries(rows)


def _cached_related(rows: list[Any], name: str) -> list[Any]:
    """The rows cached under relation *name* on *rows*, each once."""
    related: dict[int, Any] = {}
    for row in rows:
        value = row.__dict__.get(f"_{name}")
        if value is None:
            continue
        if isinstance(value, Model):
            related.setdefault(id(value), value)
        elif getattr(value, "_fetched", False):
            for item in value.related_objects:
                related.setdefault(id(item), item)
    return list(related.values())


async def _hide_scoped_out(rows: list[Any], name: str, load: _PlannedPrefetch) -> None:
    """Drop cached forward rows *load*'s scope rejects, leaving the FK alone."""
    cache_attr = f"_{name}"
    pk_attr = load.related_model._meta.pk_attr  # type: ignore[attr-defined]
    cached = [row.__dict__.get(cache_attr) for row in rows]
    pks = {getattr(obj, pk_attr) for obj in cached if obj is not None}
    if not pks:
        return
    visible = set(
        await load.scope(
            load.related_model.filter(**{f"{pk_attr}__in": list(pks)})  # type: ignore[attr-defined]
        ).values_list(pk_attr, flat=True)
    )
    for row, obj in zip(rows, cached, strict=True):
        if obj is not None and getattr(obj, pk_attr) not in visible:
            row.__dict__[cache_attr] = None


def _counting_call(original: Any, probe: Any) -> Any:
    """Wrap an async client method so each call bumps *probe*."""

    async def _counted(*args: Any, **kwargs: Any) -> Any:
        probe.count += 1
        return await original(*args, **kwargs)

    return _counted


class TortoiseBackend(BaseBackend):
    """Backend adapter for Tortoise ORM (async)."""

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
        self._type_cache: dict[tuple[int, str], type] = {}
        self._max_filter_depth = max_filter_depth
        self._max_filter_branches = max_filter_branches
        self._enable_regex_filters = enable_regex_filters
        self._max_in_list_size = max_in_list_size

    def _introspect_model(
        self, model: type
    ) -> list[tuple[str, type, bool, type | None]]:
        """Return (field_name, python_type, is_relation, related_model) for each
        field on a Tortoise model."""
        meta = model._meta  # type: ignore[attr-defined]
        result: list[tuple[str, type, bool, type | None]] = []
        seen: set[str] = set()

        module = importlib.import_module(model.__module__)

        def _resolve_related_model(
            field_obj: Any, annotation: str | None = None
        ) -> type | None:
            related_model = getattr(field_obj, "related_model", None)
            if related_model is not None:
                return related_model

            model_name = getattr(field_obj, "model_name", None)
            if isinstance(model_name, str):
                target_name = model_name.split(".")[-1]
                return getattr(module, target_name, None)

            if annotation:
                match = re.search(r"\[\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)", annotation)
                if match:
                    return getattr(module, match.group(1), None)

            return None  # pragma: no cover

        for name, field_obj in meta.fields_map.items():
            field_class_name = type(field_obj).__name__

            if field_class_name in _MANY_REL_TYPES:
                related_model = _resolve_related_model(field_obj)
                result.append((name, Any, True, related_model))
                seen.add(name)
                continue

            if field_class_name == "ForeignKeyFieldInstance":
                related_model = _resolve_related_model(field_obj)
                result.append((name, Any, True, related_model))
                seen.add(name)
                fk_type: type = int
                if related_model is not None:
                    pk_attr = related_model._meta.pk_attr  # type: ignore[attr-defined]
                    pk_field = related_model._meta.fields_map.get(pk_attr)  # type: ignore[attr-defined]
                    if pk_field is not None and type(pk_field).__name__ == "CharField":
                        fk_type = str
                if getattr(field_obj, "null", False):
                    fk_type = fk_type | None  # type: ignore[assignment]
                result.append((f"{name}_id", fk_type, False, related_model))
                seen.add(f"{name}_id")
                continue

            py_type = _TORTOISE_FIELD_MAP.get(field_class_name, str)
            result.append((name, py_type, False, None))
            seen.add(name)

        # Reverse relations are often only visible through type annotations
        # before Tortoise has fully initialized model metadata.
        for name, annotation in getattr(model, "__annotations__", {}).items():
            if name in seen or not isinstance(annotation, str):
                continue
            if "Relation[" not in annotation:
                continue  # pragma: no cover
            related_model = _resolve_related_model(None, annotation)
            result.append((name, Any, True, related_model))  # pragma: no cover
            seen.add(name)

        return result

    def _get_pk_names(self, model: type) -> set[str]:
        meta = model._meta  # type: ignore[attr-defined]
        return {meta.pk_attr}

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

                @classmethod
                def is_type_of(inner_cls: type, obj: object, info: Any) -> bool:
                    return isinstance(obj, model)

                cls.is_type_of = is_type_of  # type: ignore[method-assign]

            fields_meta = self._introspect_model(model)
            col_types: dict[str, type] = {}
            rel_fields: dict[str, dict[str, Any]] = {}

            for fname, ftype, is_relation, rel_model in fields_meta:
                if is_relation:
                    rel_fields[fname] = {"model": rel_model}
                else:
                    col_types[fname] = ftype

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
            for field_name in list(annotations):
                if field_name not in rel_fields:
                    continue
                if field_name in vars(cls):
                    continue  # pragma: no cover
                ann = annotations[field_name]
                rel_model = rel_fields[field_name]["model"]
                el_type = extract_element_type(ann)
                if el_type is None:
                    # A to-one relation. No arguments to generate, but it
                    # still needs a resolver so the scope is applied when the
                    # parent arrives already materialized.
                    setattr(
                        cls,
                        field_name,
                        _make_tortoise_to_one_resolver(
                            self, field_name, rel_model, ann
                        ),
                    )
                    continue

                f_type = getattr(el_type, "__orm_filter__", None)
                o_type = getattr(el_type, "__orm_order__", None)

                if f_type or o_type:
                    setattr(
                        cls,
                        field_name,
                        _make_tortoise_rel_resolver(
                            self,
                            field_name,
                            rel_model,
                            f_type,
                            o_type,
                        ),
                    )
                else:

                    def _make_resolver(
                        fname: str,
                        related_model: type,
                        return_ann: Any,
                        backend: Any,
                    ) -> Any:
                        async def resolver(self: Any, info: Any) -> Any:
                            from strawberry_orm.lazy_resolution import (
                                _tortoise_relation_prefetched,
                                relation_cache_is_scoped,
                            )

                            rel_value = getattr(self, fname)
                            restrict = backend.relation_scope(type(self), fname, info)
                            # A cache is returned only when this library filled
                            # it after applying scope_rows. A prefetch in the
                            # resolver is re-queried.
                            if _tortoise_relation_prefetched(self, fname) and (
                                restrict is None
                                or relation_cache_is_scoped(self, fname)
                            ):
                                return list(rel_value)
                            qs = (
                                rel_value.all()
                                if hasattr(rel_value, "all")
                                else related_model.filter(
                                    pk__in=[item.id for item in list(rel_value)]
                                )
                            )
                            qs = backend._apply_nested_queryset(
                                qs,
                                type(self),
                                fname,
                                related_model,
                                info,
                            )
                            return _apply_python_ordering(
                                list(await qs),
                                _query_orderings(qs),
                            )

                        resolver.__name__ = fname
                        resolver.__annotations__ = {
                            "info": strawberry.types.Info,
                            "return": return_ann,
                        }
                        return strawberry.field(resolver=resolver)

                    setattr(
                        cls,
                        field_name,
                        _make_resolver(field_name, rel_model, ann, self),
                    )

            self._check_lazy_relation_fields(cls, model, annotations)
            return self._finalize_type(cls, model, type_name, name)

        return decorator

    # -- Query application ----------------------------------------------------

    def apply_filters(
        self, query: Any, filter_input: Any, model: type, info: Any = None
    ) -> Any:
        q_obj, query = _build_tortoise_filter(
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
        if q_obj is not None:
            if self.filter_crosses_to_many(filter_input, model):
                # Matched in a subquery, so the outer query keeps each row once.
                from tortoise.expressions import Q, Subquery

                pk = model._meta.pk_attr  # type: ignore[attr-defined]
                matching = model.filter(q_obj).values_list(pk, flat=True)  # type: ignore[attr-defined]
                q_obj = Q(**{f"{pk}__in": Subquery(matching)})
            query = query.filter(q_obj)
        return query

    def apply_ordering(
        self, query: Any, order_input: Any, model: type, info: Any = None
    ) -> Any:
        order_list = order_input if isinstance(order_input, list) else [order_input]
        clauses: list[str] = []
        python_orderings: list[tuple[str, bool, bool | None, bool | None]] = []
        for entry in order_list:
            entry_orderings, query = _build_tortoise_ordering(
                entry, query=query, model=model, info=info, backend=self
            )
            for (
                col_name,
                descending,
                nulls_first,
                nulls_last,
            ) in entry_orderings:
                python_orderings.append((col_name, descending, nulls_first, nulls_last))
                if nulls_first or nulls_last:
                    continue
                clauses.append(f"-{col_name}" if descending else col_name)
        if clauses:
            query = query.order_by(*clauses)
        if python_orderings:
            _remember_query_ordering(query, python_orderings)
        return query

    # -- Related list refs ---------------------------------------------------

    async def apply_ref_list(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Any | None = None,
    ) -> None:
        from tortoise.transactions import in_transaction

        from strawberry_orm.upserts import has_upsert

        writer = self._ref_list_writer(instance, field, refs, info, authorize)
        if not any(has_upsert(ref) for ref in refs):
            await self._apply_ref_items(instance, field, refs, info, authorize, writer)
            return
        async with in_transaction(type(instance)._meta.default_connection):
            await self._apply_ref_items(instance, field, refs, info, authorize, writer)

    async def _apply_ref_items(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        authorize: Any | None,
        writer: Any,
    ) -> None:
        from strawberry_orm.repo import _check_auth
        from strawberry_orm.upserts import has_upsert, run_async

        manager = getattr(instance, field)
        rel_model = manager.remote_model
        repo = self.get_repo(rel_model) if not authorize else None

        to_add: list[Any] = []
        to_unlink_ids: list[Any] = []
        to_delete_ids: list[Any] = []

        for ref in refs:
            ref_create = getattr(ref, "create", strawberry.UNSET)
            ref_update = getattr(ref, "update", strawberry.UNSET)
            ref_unlink = getattr(ref, "unlink", strawberry.UNSET)
            ref_delete = getattr(ref, "delete", strawberry.UNSET)

            if has_upsert(ref):
                spec = self._upsert_relation_spec(type(instance), field)
                await run_async(writer.apply_ref_upsert(instance, spec, ref, link=True))
            elif ref_create is not strawberry.UNSET and ref_create is not None:
                if authorize and not authorize("create", rel_model, None, info):
                    continue
                data = input_to_dict(ref_create)
                if repo is not None:
                    data = repo.on_before_create(data, info)
                _check_auth(repo, "can_create", data, info)
                if repo is not None:
                    obj = await repo._create_async(rel_model, data, info)
                else:
                    obj = await rel_model.create(**data)
                if repo is not None:
                    repo.on_after_create(obj, info)
                to_add.append(obj)
            elif ref_update is not strawberry.UNSET and ref_update is not None:
                data = input_to_dict(ref_update)
                pk = data.pop("id")
                if authorize and not authorize("update", rel_model, pk, info):
                    continue
                if repo is not None:
                    obj = await repo._get_async(rel_model, pk, info)
                else:
                    obj = await rel_model.filter(pk=pk).first()
                if obj is not None:
                    if repo is not None:
                        data = repo.on_before_update(obj, data, info)
                    _check_auth(repo, "can_update", obj, data, info)
                    _check_auth(repo, "can_link", instance, field, obj, info)
                    if data:
                        if repo is not None:
                            for k, v in data.items():
                                setattr(obj, k, v)
                            await repo._save_async(obj, info)
                            repo.on_after_update(obj, info)
                        else:
                            await rel_model.filter(pk=pk).update(**data)
                            obj = await rel_model.get(pk=pk)
                    to_add.append(obj)
            elif ref_unlink is not strawberry.UNSET and ref_unlink is not None:
                if authorize and not authorize(
                    "unlink", rel_model, ref_unlink.id, info
                ):
                    continue
                if repo is not None:
                    obj = await repo._get_async(rel_model, ref_unlink.id, info)
                else:
                    obj = await rel_model.filter(pk=ref_unlink.id).first()
                if obj is not None:
                    _check_auth(repo, "can_unlink", instance, field, obj, info)
                    to_unlink_ids.append(ref_unlink.id)
            elif ref_delete is not strawberry.UNSET and ref_delete is not None:
                if authorize and not authorize(
                    "delete", rel_model, ref_delete.id, info
                ):
                    continue
                if repo is not None:
                    obj = await repo._get_async(rel_model, ref_delete.id, info)
                else:
                    obj = await rel_model.filter(pk=ref_delete.id).first()
                if obj is not None:
                    _check_auth(repo, "can_delete", obj, info)
                    if repo is not None:
                        repo.on_before_delete(obj, info)
                    to_delete_ids.append(ref_delete.id)

        if to_add:
            await manager.add(*to_add)
        if to_unlink_ids:
            to_remove = await rel_model.filter(pk__in=to_unlink_ids)
            await manager.remove(*to_remove)
        if to_delete_ids:
            to_remove = await rel_model.filter(pk__in=to_delete_ids)
            await manager.remove(*to_remove)
            await rel_model.filter(pk__in=to_delete_ids).delete()

    # -- Upserts -------------------------------------------------------------

    async def apply_upsert(
        self, payload: Any, info: Any, *, authorize: Any | None = None
    ) -> Any:
        from tortoise.transactions import in_transaction

        from strawberry_orm.upserts import run_async

        writer = self._upsert_writer(info, authorize)
        writer.validate_root(payload)
        model = type(payload).__orm_model__
        async with in_transaction(model._meta.default_connection):
            return await run_async(writer.apply_root(payload))

    def _upsert_ops(self, info: Any) -> Any:
        return _TortoiseUpsertOps(info)

    def _upsert_relation_spec(self, model: type, field: str) -> Any:
        from strawberry_orm.mutations import _tortoise_relation_specs

        return _tortoise_relation_specs(model)[field]

    def _find_for_upsert(
        self, repo: Any, model: type, query: dict[str, Any], info: Any
    ) -> Any:
        return self._find_for_upsert_async(repo, model, query, info)

    async def _find_for_upsert_async(
        self, repo: Any, model: type, query: dict[str, Any], info: Any
    ) -> Any:
        rows = repo.scope_query(model.all(), info).filter(
            **self._upsert_lookup_kwargs(query)
        )
        return self._single_upsert_match(model, list(await rows.limit(2)))

    # -- Grouping / aggregation -----------------------------------------------

    async def apply_aggregation(
        self, query: Any, info: Any, aggregate_meta: AggregateMeta
    ) -> Any:
        from tortoise.functions import Avg, Count, Max, Min, Sum

        requested = requested_aggregates(info, "aggregates") or {}
        if not requested:
            return aggregate_meta.aggregates_type(count=0)

        agg_kwargs: dict[str, Any] = {}
        if requested.get("count"):
            agg_kwargs["_count"] = Count("id")
        _TORT_AGG = {"sum": Sum, "avg": Avg, "min": Min, "max": Max}
        for func_name, AggCls in _TORT_AGG.items():
            for fname in requested.get(func_name, []):
                agg_kwargs[f"_{func_name}_{fname}"] = AggCls(fname)

        if not agg_kwargs:
            return aggregate_meta.aggregates_type(count=0)

        # Tortoise folds ordering columns into the GROUP BY of an aggregate
        # query, which silently turns a total into a per-row count. Ordering
        # means nothing to a single aggregate row, so drop it.
        result = (
            await query.order_by().annotate(**agg_kwargs).values(*agg_kwargs.keys())
        )
        if result:
            row = type("Row", (), result[0])()
        else:
            row = type("Row", (), {k: 0 for k in agg_kwargs})()  # pragma: no cover
        return aggregate_meta.build_aggregates(row, requested)

    def relation_hop(self, model: type, relation: str) -> Any:
        from strawberry_orm.grouping import GroupHop

        field = model._meta.fields_map[relation]  # type: ignore[attr-defined]
        return GroupHop(
            relation=relation,
            model=model,
            target=field.related_model,
            many=type(field).__name__ in _TO_MANY_REL_TYPES,
        )

    async def apply_grouping(
        self,
        query: Any,
        paths: list[Any],
        info: Any,
        aggregate_meta: AggregateMeta,
        *,
        order_input: Any | None = None,
    ) -> list[Any]:
        from pypika_tortoise import functions

        from strawberry_orm.grouping import build_group

        model = aggregate_meta.model
        requested = requested_aggregates(info, "groups.aggregates") or {}
        rows = _GroupedRows(self, query, model, paths, info)
        aggregates = rows.aggregates(requested)
        ordering = rows.ordering(query, order_input)
        stmt = rows.stmt.select(
            *rows.selected(),
            *(term.as_(name) for name, (term, _) in aggregates.items()),
        ).groupby(*rows.values.values())
        # Read in the connection's order, a group starts at its least value of
        # each clause, or its greatest for a descending one.
        for term, descending, nulls in ordering:
            first = (functions.Max if descending else functions.Min)(term)
            stmt = _order_by(stmt, first, descending, nulls)
        for term in rows.values.values():
            stmt = stmt.orderby(term)

        groups = []
        for found in await rows.execute(stmt):
            values = rows.read(found)
            for name, (_term, read) in aggregates.items():
                values[name] = read(found[name])
            groups.append(build_group(aggregate_meta, paths, values, requested))
        return groups

    async def batch_group_items(
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
        """Every group's page of rows in one windowed query."""
        from pypika_tortoise import Order
        from pypika_tortoise.analytics import RowNumber

        from strawberry_orm.grouping import key_tuple

        rows = _GroupedRows(self, query, model, paths, info)
        window = RowNumber().over(*rows.values.values())
        for term, descending, nulls in [
            *rows.ordering(query, order_input),
            # Rows that tie on the order would be numbered however the
            # database likes, differently from one page to the next.
            (rows.pk, False, None),
        ]:
            if nulls is not None:
                window = window.orderby(_nulls_rank(term, nulls))
            window = window.orderby(term, order=Order.desc if descending else Order.asc)
        # The model's columns one by one: pypika drops a table's other
        # columns, the group values among them, from a select of its star.
        columns = model._meta.fields_db_projection.values()  # type: ignore[attr-defined]
        ranked = rows.stmt.select(
            *(rows.base.field(column) for column in columns),
            *rows.selected(),
            window.as_(_ROW_NUMBER_ALIAS),
        ).as_("_orm_windowed")
        number = ranked.field(_ROW_NUMBER_ALIAS)
        page = (
            rows.query_class.from_(ranked)
            .select(ranked.star)
            .where(
                (number > per_group_offset)
                & (number <= per_group_offset + per_group_limit)
            )
            # Ordered by the row number so each group comes back in the order
            # the window put it in; without it the rows arrive however the
            # database found them and the requested order is lost.
            .orderby(number)
        )

        items_by_key: dict[tuple, list[Any]] = defaultdict(list)
        for found in await rows.execute(page):
            found.pop(_ROW_NUMBER_ALIAS)
            values = rows.read(found)
            for alias in rows.values:
                found.pop(alias)
            items_by_key[key_tuple(paths, values)].append(model._init_from_db(**found))
        return dict(items_by_key)

    async def group_memberships(
        self, query: Any, paths: list[Any], pks: list[Any], info: Any
    ) -> dict[Any, set[tuple]]:
        from strawberry_orm.grouping import key_tuple

        model = query.model
        rows = _GroupedRows(self, query, model, paths, info)
        stmt = rows.stmt.select(rows.pk.as_("_orm_pk"), *rows.selected()).where(
            rows.pk.isin(pks)
        )
        read_pk = model._meta.pk.to_python_value
        found: dict[Any, set[tuple]] = defaultdict(set)
        for row in await rows.execute(stmt):
            found[read_pk(row["_orm_pk"])].add(key_tuple(paths, rows.read(row)))
        return dict(found)

    async def group_counts(
        self, query: Any, key_field: str, info: Any
    ) -> dict[Any, int]:
        """Each group's real total, which the windowed page cannot report."""
        from tortoise.functions import Count

        # Inherited ordering would drag non-grouped columns into the statement.
        counted = (
            query.order_by()
            .group_by(key_field)
            .annotate(**{_COUNT_ALIAS: Count(query.model._meta.pk_attr)})
            .values(key_field, _COUNT_ALIAS)
        )
        return {row[key_field]: row[_COUNT_ALIAS] for row in await counted}

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type) -> Any:
        qs = model.all()
        if self._default_query_limit is not None:
            qs = qs.limit(self._default_query_limit)
        return qs

    def is_query_object(self, value: Any) -> bool:
        return isinstance(value, QuerySet)

    def is_model_instance(self, value: Any) -> bool:
        return isinstance(value, Model)

    def _relation_target_model(self, model: type, relation: str) -> type | None:
        field = model._meta.fields_map.get(relation)  # type: ignore[attr-defined]
        return getattr(field, "related_model", None) if field is not None else None

    def relation_names(self, model: type) -> set[str]:
        # ``related_model`` is only populated once Tortoise is initialised, so
        # fall back to the field class for schemas built before init.
        fields_map = model._meta.fields_map  # type: ignore[attr-defined]
        return {
            name
            for name, field in fields_map.items()
            if getattr(field, "related_model", None) is not None
            or type(field).__name__ in _REL_FIELD_TYPES
        }

    @contextmanager
    def query_probe(self, info: Any) -> Any:
        """Count statements by wrapping the client's execute methods."""
        from strawberry_orm.lazy_resolution import QueryProbe

        probe = QueryProbe()
        try:
            # Importing ``connections`` needs an active Tortoise context.
            from tortoise import connections

            client = connections.get("default")
        except Exception:
            yield probe
            return

        originals = {}
        for name in ("execute_query", "execute_query_dict"):
            original = getattr(client, name, None)
            if original is None:
                continue  # pragma: no cover
            originals[name] = original
            setattr(client, name, _counting_call(original, probe))
        try:
            yield probe
        finally:
            for name, original in originals.items():
                setattr(client, name, original)

    async def count_query(self, query: Any, info: Any) -> int:
        if not query._distinct:
            return await query.count()
        # Tortoise's count drops DISTINCT and would count a join's repeats,
        # so count the rows of the statement itself. Built on a clone: the
        # caller may still cut its page from *query*.
        counted = query._clone()
        counted._choose_db_if_not_chosen()
        counted._make_query()
        inner, values = counted.query.get_parameterized_sql()
        rows = await counted._db.execute_query_dict(
            f"SELECT COUNT(*) AS n FROM ({inner}) _orm_counted", values
        )
        return rows[0]["n"]

    def apply_page(self, query: Any, offset: int, limit: int) -> Any | None:
        prior_offset, prior_limit = query._offset or 0, query._limit
        if prior_limit is not None:
            limit = max(0, min(limit, prior_limit - offset))
        page = _ordered_to_the_row(query)
        return page.offset(prior_offset + offset).limit(limit)

    async def materialize_query(self, query: Any, info: Any) -> list[Any]:
        return _apply_python_ordering(
            list(await query),
            _query_orderings(query),
        )

    # -- Optimizer -----------------------------------------------------------

    _supports_windowed_pages = True

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return OptimizerExtension.configure(backend=self, store=self._store)

    def instance_pk(self, instance: Any) -> Any:
        return getattr(instance, "pk", None)

    def query_model(self, query: Any) -> type | None:
        return getattr(query, "model", None)

    async def filter_scoped_pks(
        self, model: type, pks: list[Any], info: Any
    ) -> set[Any]:
        queryset = self.apply_type_scope(model.filter(pk__in=pks), model, info)
        return {self.instance_pk(row) for row in await queryset}

    def _relation_connection_spec(
        self, model: type, field_name: str, relation: str
    ) -> Any:
        from tortoise.fields.relational import BackwardFKRelation

        from strawberry_orm.backends._base import RelationConnectionSpec

        field = model._meta.fields_map.get(relation)  # type: ignore[attr-defined]
        key_field = getattr(field, "relation_field", None)
        # Only a reverse foreign key keeps the parent's key on the related row.
        # A many-to-many hides it in the through table, leaving the window
        # nothing to partition by.
        if not isinstance(field, BackwardFKRelation) or key_field is None:
            return None
        return RelationConnectionSpec(
            model=model,
            field_name=field_name,
            relation=relation,
            related_model=self._relation_target_model(model, relation),
            key_field=key_field,
        )

    def relation_base_query(self, spec: Any, pks: list[Any], info: Any) -> Any:
        qs = spec.related_model.filter(**{f"{spec.key_field}__in": pks})
        restrict = self.relation_scope(
            spec.model, spec.field_name, info, on=spec.relation
        )
        return qs if restrict is None else restrict(qs, info)

    def _make_relation_query_resolver(
        self, model: type, field_name: str, relation: str
    ) -> Any:
        backend = self
        spec = self._relation_connection_spec(model, field_name, relation)

        def resolver(self: Any, info: Any) -> Any:
            from strawberry_orm.batching import take_page

            page = take_page(self, field_name, info)
            if page is not None:
                return page
            qs = spec.related_model.filter(
                **{spec.key_field: backend.instance_pk(self)}
            )
            restrict = backend.relation_scope(model, field_name, info, on=relation)
            return qs if restrict is None else restrict(qs, info)

        resolver.__name__ = field_name
        return resolver

    def _apply_nested_queryset(
        self,
        qs: Any,
        parent_model: type,
        field_name: str,
        related_model: type,
        info: Any,
    ) -> Any:
        """Re-apply child scoping for nested relation resolvers."""
        get_qs = self._type_querysets.get(related_model)
        if get_qs is not None:
            qs = get_qs(qs, info)

        type_name = self._resolved_type_name(parent_model, info)
        if type_name:
            hints = self._store.get(type_name, field_name)
            if hints and hints.scope is not None:
                qs = call_scope(hints.scope, qs, info)

        return qs

    def _relation_prefetches(
        self, store: Any, model: type, info: Any
    ) -> dict[str, _PlannedPrefetch]:
        """Plan the relations the selection and its ``using=`` paths need.

        Shared by query optimization and by loading relations onto instances
        the caller already holds, so both apply the same row scoping.
        """
        from strawberry_orm.optimizer.selections import (
            field_nodes_from_info,
            fragments_from_info,
            iter_field_nodes,
        )

        fragments = fragments_from_info(info)

        # A relation reached both by the selection and by a ``using=`` path is
        # loaded once, with one scope, so neither can leave the other's rows in
        # the cache.
        plan: dict[str, _PlannedPrefetch] = {}

        def _to_snake(name: str) -> str:
            return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()

        def _get_custom_qs(
            type_name: str | None,
            field_name: str,
            related_model: type,
        ) -> Any:
            """Return a load callable or None."""
            nested_get_qs = self._type_querysets.get(related_model)
            load_fn = None
            if type_name and store:
                hints = store.get(type_name, field_name)
                if hints and hints.scope is not None:
                    load_fn = hints.scope
            if nested_get_qs is None and load_fn is None:
                return None

            def combined(qs: Any) -> Any:
                if nested_get_qs is not None:
                    qs = nested_get_qs(qs, info)
                if load_fn is not None:
                    qs = call_scope(load_fn, qs, info)
                return qs

            return combined

        def _planned(
            level: dict[str, _PlannedPrefetch],
            current_model: type,
            rel_name: str,
            type_name: str | None,
        ) -> _PlannedPrefetch | None:
            field_obj = current_model._meta.fields_map.get(rel_name)  # type: ignore[attr-defined]
            field_cls = type(field_obj).__name__
            if field_obj is None or (
                field_cls not in _FORWARD_REL_TYPES and field_cls not in _MANY_REL_TYPES
            ):
                return None
            load = level.get(rel_name)
            if load is None:
                related_model = field_obj.related_model
                load = level[rel_name] = _PlannedPrefetch(
                    related_model=related_model,
                    field_cls=field_cls,
                    scope=_get_custom_qs(type_name, rel_name, related_model),
                )
            return load

        def _plan_hints(
            level: dict[str, _PlannedPrefetch],
            current_model: type,
            type_name: str | None,
            field_name: str,
        ) -> None:
            """Plan the relation paths declared on ``using=`` for *field_name*.

            Declared against the field name, so they also apply to computed
            fields that are not ORM fields themselves.
            """
            hints = store.get(type_name, field_name) if type_name and store else None
            if not hints or hints.disable_optimization or not hints.using:
                return
            for path in hints.using:
                hop_level, hop_model, hop_type = level, current_model, type_name
                for hop in hint_hops(path):
                    load = _planned(hop_level, hop_model, hop, hop_type)
                    if load is None:
                        break
                    hop_type = self._child_type_name(
                        hop_type, hop, load.related_model, info
                    )
                    hop_level, hop_model = load.children, load.related_model

        def _plan_selection(
            level: dict[str, _PlannedPrefetch],
            selection_set: Any,
            current_model: type,
            type_name: str | None,
        ) -> None:
            for node in iter_field_nodes(selection_set, fragments):
                field_name = _to_snake(node.name.value)

                # A field that answers for itself will ignore whatever the
                # prefetch loads, so do not pay for it.
                if self.resolves_itself(type_name, field_name):
                    continue
                _plan_hints(level, current_model, type_name, field_name)

                load = _planned(level, current_model, field_name, type_name)
                if load is None or not node.selection_set:
                    continue
                child_type = self._child_type_name(
                    type_name, node.name.value, load.related_model, info
                )
                _plan_selection(
                    load.children, node.selection_set, load.related_model, child_type
                )

        root_type_name = self._selection_type_name(model, info)
        for field_node in field_nodes_from_info(info):
            if field_node.selection_set is not None:
                _plan_selection(plan, field_node.selection_set, model, root_type_name)
        return plan

    async def apply_optimizer_hints(
        self,
        store: Any,
        query: Any,
        info: Any,
    ) -> Any:
        orderings = _query_orderings(query)

        try:
            model = query.model
        except AttributeError:
            return _apply_python_ordering(list(await query), orderings)

        query = self.apply_type_scope(query, model, info)

        plan = self._relation_prefetches(store, model, info)

        if plan:
            query = query.prefetch_related(*_prefetch_lookups(plan))
        if orderings:
            _remember_query_ordering(query, orderings)

        results = list(await query)

        if plan:
            await self._finish_prefetches(results, plan)

        return results

    async def load_relations(
        self, store: Any, instances: list[Any], info: Any
    ) -> list[Any]:
        """Eager-load the selected relations onto instances already in memory.

        Prefetching fills the relation caches in place, so scalar values the
        caller is holding - which may be fresher than the database, straight
        out of a mutation - are never overwritten.

        Returns the rows it actually loaded onto, which is empty when the
        selection named no relations.
        """
        by_model: dict[type, list[Any]] = {}
        for instance in instances:
            by_model.setdefault(type(instance), []).append(instance)

        loaded: list[Any] = []
        for model, rows in by_model.items():
            plan = self._relation_prefetches(store, model, info)
            if not plan:
                continue
            await _prefetch_onto(model, rows, _prefetch_lookups(plan))
            await self._finish_prefetches(rows, plan)
            loaded.extend(rows)

        return loaded

    async def _finish_prefetches(
        self, rows: list[Any], plan: dict[str, _PlannedPrefetch]
    ) -> None:
        """Hide scoped-out forward rows, then mark every scoped cache.

        A forward relation is prefetched unscoped: Tortoise clears the parent's
        foreign key column when a filtered prefetch finds nothing, which a later
        save would write back. The rows the scope rejects are dropped from the
        cache here instead, leaving the column alone.
        """
        from strawberry_orm.lazy_resolution import mark_relations_scoped

        for name, load in plan.items():
            if load.scope is not None:
                if load.forward:
                    await _hide_scoped_out(rows, name, load)
                mark_relations_scoped(rows, [name])
            if not load.children:
                continue
            related = _cached_related(rows, name)
            if load.many_to_many and related:
                await _prefetch_onto(
                    load.related_model, related, _prefetch_lookups(load.children)
                )
            await self._finish_prefetches(related, load.children)


class _TortoiseUpsertOps:
    """The calls :class:`~strawberry_orm.upserts.UpsertWriter` makes.

    A call that needs I/O returns an awaitable for the async driver to await.
    """

    def __init__(self, info: Any) -> None:
        self._info = info

    # -- Repo-backed reads and writes ----------------------------------------

    def find(self, repo: Any, model: type, query: dict[str, Any]) -> Any:
        return repo.find_for_upsert_async(model, query, self._info)

    def get(self, repo: Any, model: type, pk: Any) -> Any:
        return repo._get_async(model, pk, self._info)

    def create(self, repo: Any, model: type, data: dict[str, Any]) -> Any:
        return repo._create_async(model, data, self._info)

    def save(self, repo: Any, instance: Any) -> Any:
        return repo._save_async(instance, self._info)

    def delete(self, repo: Any, instance: Any) -> Any:
        return repo._delete_async(instance, self._info)

    # -- Relations -----------------------------------------------------------

    def load_attr(self, instance: Any, name: str) -> Any:
        return getattr(instance, name)

    def init_collection(self, instance: Any, name: str) -> None:
        return None

    def link(self, parent: Any, spec: Any, child: Any) -> Any:
        if spec.relation_mode == "reverse_fk":
            if self._is_child(parent, spec, child):
                return None
            setattr(child, spec.remote_attr, parent)
            return child.save()
        return getattr(parent, spec.name).add(child)

    def unlink(self, parent: Any, spec: Any, child: Any) -> Any:
        if spec.relation_mode == "reverse_fk":
            setattr(child, spec.remote_attr, None)
            return child.save()
        return getattr(parent, spec.name).remove(child)

    def is_member(self, parent: Any, spec: Any, child: Any) -> Any:
        if spec.relation_mode == "reverse_fk":
            return self._is_child(parent, spec, child)
        return getattr(parent, spec.name).filter(pk=child.pk).exists()

    def back_reference(
        self, parent_model: type, spec: Any
    ) -> tuple[str | None, str | None]:
        """The attribute and foreign key column a nested child points back with."""
        return spec.remote_attr, self._foreign_key(spec).source_field

    def parent_scope(self, parent: Any, spec: Any) -> dict[str, Any]:
        if spec.relation_mode != "reverse_fk":
            return {}
        foreign_key = self._foreign_key(spec)
        target = foreign_key.to_field_instance.model_field_name
        return {foreign_key.source_field: getattr(parent, target)}

    def parent_assignment(self, parent: Any, spec: Any) -> dict[str, Any]:
        if spec.relation_mode != "reverse_fk":
            return {}
        return {spec.remote_attr: parent}

    # -- Keys ----------------------------------------------------------------

    def pk_value(self, instance: Any) -> Any:
        return instance.pk

    def coerce_pk(self, model: type, value: Any) -> Any:
        from strawberry_orm.upserts import UpsertValidationError

        if not isinstance(value, str):
            return value
        try:
            return model._meta.pk.to_python_value(value)  # type: ignore[attr-defined]
        except (TypeError, ValueError):
            raise UpsertValidationError(
                f"{value!r} is not a valid {model.__name__} id"
            ) from None

    # -- Internals -----------------------------------------------------------

    @staticmethod
    def _foreign_key(spec: Any) -> Any:
        return spec.related_model._meta.fields_map[spec.remote_attr]

    def _is_child(self, parent: Any, spec: Any, child: Any) -> bool:
        return all(
            getattr(child, child_key) == value
            for child_key, value in self.parent_scope(parent, spec).items()
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_tortoise_to_one_resolver(
    backend: Any,
    fname: str,
    rel_model: type,
    return_ann: Any,
) -> Any:
    """Create a field for a to-one relation, scoped at resolve time.

    A scoped-out row reads as absent, matching what the optimizer produces
    when its scoped load finds nothing on the other end.
    """

    async def resolver(self: Any, info: Any) -> Any:
        from strawberry_orm.lazy_resolution import (
            _tortoise_relation_prefetched,
            relation_cache_is_scoped,
        )

        restrict = backend.relation_scope(type(self), fname, info)
        if restrict is None or (
            _tortoise_relation_prefetched(self, fname)
            and relation_cache_is_scoped(self, fname)
        ):
            related = getattr(self, fname, None)
            return await related if inspect.isawaitable(related) else related

        related_id = getattr(self, f"{fname}_id", None)
        if related_id is None:
            return None
        return await restrict(rel_model.filter(pk=related_id), info).first()

    resolver.__name__ = fname
    resolver.__annotations__ = {
        "info": strawberry.types.Info,
        "return": return_ann,
    }
    return strawberry.field(resolver=resolver)


def _make_tortoise_rel_resolver(
    backend: Any,
    fname: str,
    rel_model: type,
    filter_type: Any,
    order_type: Any,
) -> Any:
    """Create a Strawberry field for a Tortoise relation with filter/order."""

    info_type = strawberry.types.Info

    def _build_qs(self: Any, info: Any) -> Any:
        rel_value = getattr(self, fname)
        qs = (
            rel_value.all()
            if hasattr(rel_value, "all")
            else rel_model.filter(pk__in=[item.id for item in list(rel_value)])
        )
        return backend._apply_nested_queryset(qs, type(self), fname, rel_model, info)

    if filter_type and order_type:

        async def resolver(
            self: Any, info: Any, filter: Any = None, order: Any = None
        ) -> Any:
            qs = _build_qs(self, info)
            if filter is not None:
                qs = backend.apply_filters(qs, filter, rel_model, info=info)
            if order is not None:
                qs = backend.apply_ordering(qs, order, rel_model, info=info)
            return _apply_python_ordering(
                list(await qs),
                _query_orderings(qs),
            )

        resolver.__annotations__ = {
            "info": info_type,
            "filter": filter_type | None,
            "order": list[order_type] | None,
        }
    elif filter_type:

        async def resolver(self: Any, info: Any, filter: Any = None) -> Any:
            qs = _build_qs(self, info)
            if filter is not None:
                qs = backend.apply_filters(qs, filter, rel_model, info=info)
            return _apply_python_ordering(
                list(await qs),
                _query_orderings(qs),
            )

        resolver.__annotations__ = {
            "info": info_type,
            "filter": filter_type | None,
        }
    else:

        async def resolver(self: Any, info: Any, order: Any = None) -> Any:
            qs = _build_qs(self, info)
            if order is not None:
                qs = backend.apply_ordering(qs, order, rel_model, info=info)
            return _apply_python_ordering(
                list(await qs),
                _query_orderings(qs),
            )

        resolver.__annotations__ = {
            "info": info_type,
            "order": list[order_type] | None,
        }

    return strawberry.field(resolver=resolver)


# ---------------------------------------------------------------------------
# Filter translation
# ---------------------------------------------------------------------------

_LOOKUP_TO_TORTOISE: dict[str, str] = {
    "exact": "",
    "neq": "__not",
    "gt": "__gt",
    "gte": "__gte",
    "lt": "__lt",
    "lte": "__lte",
    "contains": "__contains",
    "i_contains": "__icontains",
    "starts_with": "__startswith",
    "i_starts_with": "__istartswith",
    "ends_with": "__endswith",
    "i_ends_with": "__iendswith",
}


def _tortoise_relation_scope_q(
    backend: Any, model: type | None, relation: str, info: Any, prefix: str
) -> Any | None:
    """``Q`` restricting *relation* to the rows its scoping allows.

    Covers both the related type's ``scope_rows`` and any ``scope=`` on this
    edge, so a filter cannot reach rows the read path hides.
    """
    if backend is None or model is None:
        return None
    get_qs = backend.relation_scope(model, relation, info)
    if get_qs is None:
        return None
    from tortoise.expressions import Q, Subquery

    related = backend._relation_target_model(model, relation)
    pk_attr = related._meta.pk_attr
    scoped = get_qs(related.all(), info)
    return Q(**{f"{prefix}{relation}__{pk_attr}__in": Subquery(scoped.values(pk_attr))})


def _build_tortoise_filter(
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
) -> tuple[Any, Any]:
    """Return ``(Q_clause | None, query)``."""

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
            from tortoise.expressions import Q

            rel_name = _prefix.removesuffix("__")
            return Q(**{f"{rel_name}_id__isnull": val}), query
        elif key == "field":
            clause = _build_tortoise_field_clause(
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
                # Traversal joins straight to the related table, so the related
                # type's row scoping has to be re-applied or it is bypassed.
                scope_q = _tortoise_relation_scope_q(
                    backend, model, rel_name, info, _prefix
                )
                if scope_q is not None:
                    is_null_val = getattr(nested_filter, "is_null", strawberry.UNSET)
                    if is_null_val is not strawberry.UNSET and is_null_val is not None:
                        # "Has no related row" has to mean "none the caller can see".
                        return (~scope_q if is_null_val else scope_q), query
                fk_col = f"{rel_name}_id"
                fk_prefix = f"{_prefix}{fk_col}"
                fk_clause = build_reference_object_filter_clause(
                    nested_filter,
                    build_field_clause=_build_tortoise_reference_field_clause,
                    custom_filter_keys=custom_filter_keys,
                    max_branches=max_branches,
                    fk_prefix=fk_prefix,
                    enable_regex=enable_regex,
                    max_in_list_size=max_in_list_size,
                )
                if fk_clause is not None:
                    if scope_q is not None:
                        fk_clause = fk_clause & scope_q
                    return fk_clause, query
                nested_clause, query = _build_tortoise_filter(
                    nested_filter,
                    query=query,
                    model=(
                        backend._relation_target_model(model, rel_name)
                        if backend is not None and model is not None
                        else None
                    ),
                    info=info,
                    backend=backend,
                    max_depth=max_depth,
                    max_branches=max_branches,
                    enable_regex=enable_regex,
                    max_in_list_size=max_in_list_size,
                    _depth=_depth + 1,
                    _prefix=f"{_prefix}{rel_name}__",
                )
                if nested_clause is not None and scope_q is not None:
                    nested_clause = nested_clause & scope_q
                return nested_clause, query
        elif key == "all":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            clauses = []
            for f in val:
                sub_clause, query = _build_tortoise_filter(
                    f, **{**recurse_kw, "query": query}
                )
                if sub_clause is not None:
                    clauses.append(sub_clause)
            if not clauses:
                return None, query
            result = clauses[0]
            for s in clauses[1:]:
                result = result & s
            return result, query
        elif key == "any":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            clauses = []
            for f in val:
                sub_clause, query = _build_tortoise_filter(
                    f, **{**recurse_kw, "query": query}
                )
                if sub_clause is not None:
                    clauses.append(sub_clause)
            if not clauses:
                return None, query
            result = clauses[0]
            for s in clauses[1:]:
                result = result | s
            return result, query
        elif key == "not_":
            inner, query = _build_tortoise_filter(val, **{**recurse_kw, "query": query})
            return (~inner if inner is not None else None), query
        elif key == "one_of":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            clauses = []
            for f in val:
                sub_clause, query = _build_tortoise_filter(
                    f, **{**recurse_kw, "query": query}
                )
                if sub_clause is not None:
                    clauses.append(sub_clause)
            if not clauses:
                return None, query
            result = clauses[0]
            for s in clauses[1:]:
                result = result | s
            return result, query
        elif key in custom_filters:
            query = invoke_custom_callback(
                custom_filters[key],
                filter_input,
                query=query,
                value=val,
                info=info,
            )
            return None, query

    return None, query


def _coerce_reference_value(val: Any) -> Any:
    if isinstance(val, list):
        return [_coerce_reference_value(item) for item in val]
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return val
    text = str(val)
    try:
        return int(text)
    except ValueError:
        return text


def _build_tortoise_reference_lookup(
    col_name: str,
    lookup: Any,
    *,
    max_in_list_size: int = 500,
) -> Any:
    from tortoise.queryset import Q

    if not is_reference_lookup(lookup):
        raise TypeError("Expected ReferenceLookup for FK / reference filtering.")

    q = Q()
    fields = lookup.__class__.__dataclass_fields__

    for op_name in fields:
        val = getattr(lookup, op_name)
        if val is strawberry.UNSET or val is None:
            continue

        val = _coerce_reference_value(val)

        if op_name == "is_null":
            q = q & Q(**{f"{col_name}__isnull": val})
        elif op_name in ("in_list", "not_in_list"):
            if len(val) > max_in_list_size:
                raise ValueError(
                    f"in_list/not_in_list has {len(val)} items; "
                    f"maximum is {max_in_list_size}"
                )
            if op_name == "in_list":
                q = q & Q(**{f"{col_name}__in": val})
            else:
                q = q & ~Q(**{f"{col_name}__in": val})
        elif op_name == "neq":
            q = q & ~Q(**{f"{col_name}": val})
        elif op_name == "exact":
            q = q & Q(**{f"{col_name}": val})

    return q


def _build_tortoise_reference_field_clause(
    field_input: Any,
    *,
    fk_prefix: str,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> Any:
    from tortoise.queryset import Q

    q = Q()
    fields = field_input.__class__.__dataclass_fields__

    for col_name in fields:
        lookup = getattr(field_input, col_name)
        if lookup is strawberry.UNSET or lookup is None:
            continue
        if is_reference_lookup(lookup):
            q = q & _build_tortoise_reference_lookup(
                fk_prefix,
                lookup,
                max_in_list_size=max_in_list_size,
            )
        elif is_fk_shortcut_lookup(lookup):
            q = q & _build_tortoise_lookup(
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


def _build_tortoise_field_clause(
    field_input: Any,
    *,
    prefix: str = "",
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> Any:
    """Translate a *Field input into column-level Tortoise Q conditions."""
    from tortoise.queryset import Q

    q = Q()
    fields = field_input.__class__.__dataclass_fields__

    for col_name in fields:
        lookup = getattr(field_input, col_name)
        if lookup is strawberry.UNSET or lookup is None:
            continue
        col_q = _build_tortoise_lookup(
            f"{prefix}{col_name}",
            lookup,
            enable_regex=enable_regex,
            max_in_list_size=max_in_list_size,
        )
        q = q & col_q

    return q


def _build_tortoise_lookup(
    col_name: str,
    lookup: Any,
    *,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> Any:
    """Translate a single lookup object into Tortoise Q conditions."""
    if is_reference_lookup(lookup):
        return _build_tortoise_reference_lookup(
            col_name,
            lookup,
            max_in_list_size=max_in_list_size,
        )

    from tortoise.queryset import Q

    q = Q()
    fields = lookup.__class__.__dataclass_fields__

    for op_name in fields:
        val = getattr(lookup, op_name)
        if val is strawberry.UNSET or val is None:
            continue

        if op_name == "is_null":
            q = q & Q(**{f"{col_name}__isnull": val})
        elif op_name in ("in_list", "not_in_list"):
            if len(val) > max_in_list_size:
                raise ValueError(
                    f"in_list/not_in_list has {len(val)} items; "
                    f"maximum is {max_in_list_size}"
                )
            if op_name == "in_list":
                q = q & Q(**{f"{col_name}__in": val})
            else:
                q = q & ~Q(**{f"{col_name}__in": val})
        elif op_name == "range":
            q = q & Q(**{f"{col_name}__range": (val.start, val.end)})
        elif op_name == "neq":
            q = q & ~Q(**{f"{col_name}": val})
        elif op_name in ("regex", "i_regex"):
            if not enable_regex:
                raise ValueError(
                    "Regex filters are disabled. Pass enable_regex_filters=True "
                    "to enable."
                )
            suffix = {"regex": "__regex", "i_regex": "__iregex"}.get(op_name, "")
            q = q & Q(**{f"{col_name}{suffix}": val})
        elif op_name in _LOOKUP_TO_TORTOISE:
            suffix = _LOOKUP_TO_TORTOISE[op_name]
            q = q & Q(**{f"{col_name}{suffix}": val})

    return q


# ---------------------------------------------------------------------------
# Ordering translation
# ---------------------------------------------------------------------------


def _apply_python_ordering(
    items: list[Any],
    orderings: list[tuple[str, bool, bool | None, bool | None]] | None,
) -> list[Any]:
    if not orderings:
        return items

    ordered = list(items)
    for col_name, descending, nulls_first, nulls_last in reversed(orderings):
        null_items = [item for item in ordered if getattr(item, col_name, None) is None]
        value_items = [
            item for item in ordered if getattr(item, col_name, None) is not None
        ]
        value_items.sort(key=lambda item: getattr(item, col_name), reverse=descending)

        if nulls_first:
            ordered = null_items + value_items
        elif nulls_last or descending:
            ordered = value_items + null_items
        else:
            ordered = null_items + value_items

    return ordered


def _build_tortoise_ordering(
    order_input: Any,
    _prefix: str = "",
    *,
    query: Any = None,
    info: Any = None,
    model: type | None = None,
    backend: Any = None,
) -> tuple[list[tuple[str, bool, bool | None, bool | None]], Any]:
    """Return ``(ordering_tuples, query)``."""
    clauses: list[tuple[str, bool, bool | None, bool | None]] = []
    fields = order_input.__class__.__dataclass_fields__
    custom_orders = getattr(type(order_input), "_custom_orders", {})

    for key in fields:
        val = getattr(order_input, key)
        if val is strawberry.UNSET or val is None:
            continue

        if key == "field":
            clauses.extend(_build_tortoise_order_field(val, _prefix))
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
                sub_clauses, query = _build_tortoise_ordering(
                    nested,
                    _prefix=f"{_prefix}{rel_name}__",
                    query=query,
                    info=info,
                    model=(
                        backend._relation_target_model(model, rel_name)
                        if backend is not None and model is not None
                        else None
                    ),
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


def _build_tortoise_order_field(
    field_input: Any, prefix: str = ""
) -> list[tuple[str, bool, bool | None, bool | None]]:
    clauses: list[tuple[str, bool, bool | None, bool | None]] = []
    fields = field_input.__class__.__dataclass_fields__

    for col_name in fields:
        direction = getattr(field_input, col_name)
        if direction is strawberry.UNSET or direction is None:
            continue
        dir_value = direction.value if hasattr(direction, "value") else str(direction)
        clauses.append(
            (
                f"{prefix}{col_name}",
                dir_value.startswith("DESC"),
                "NULLS_FIRST" in dir_value,
                "NULLS_LAST" in dir_value,
            )
        )

    return clauses


# ---------------------------------------------------------------------------
# Grouping helpers
# ---------------------------------------------------------------------------


def _pypika(queryset: Any) -> Any:
    """The pypika query *queryset* runs, to be nested in a larger one.

    Nested, it keeps its values as parameters, numbered with the rest of the
    statement's when that is rendered.
    """
    queryset._choose_db_if_not_chosen()
    queryset._make_query()
    return queryset.query


class _GroupedRows:
    """*query*'s rows, once per key, joined to what every group-by path reads.

    Built as one pypika statement over the model's table: Tortoise cannot
    join a scoped subquery, nor number rows in a window. Each hop is an outer
    join to the related type's scoped rows, so a hidden related row reads as
    ``None``, as it does on the type. Across a to-many hop the join is to the
    visible rows alone, leaving one ``None`` row for a parent with none.
    """

    def __init__(
        self, backend: Any, query: Any, model: type, paths: list[Any], info: Any
    ) -> None:
        from pypika_tortoise import Table

        query._choose_db_if_not_chosen()
        self.db = query._db
        self.query_class = self.db.query_class
        self.backend = backend
        self.model = model
        self.info = info
        meta = model._meta  # type: ignore[attr-defined]
        self.base = Table(meta.db_table)
        self.pk = self.base.field(meta.db_pk_column)
        # Ordering means nothing to a set of keys, unless it picks the rows a
        # limit keeps.
        if query._limit is None and not query._offset:
            query = query.order_by()
        own = _pypika(query.values_list(meta.pk_attr, flat=True))
        self.stmt = self.query_class.from_(self.base).where(self.pk.isin(own))
        self._joined: dict[tuple[str, ...], Any] = {}
        self.values: dict[str, Any] = {}
        self._readers: dict[str, Any] = {}
        for path in paths:
            self._add(path)
        if any(hop.many for path in paths for hop in path.hops):
            # A row joined to two related rows with the same values would be
            # grouped twice under one key, so its keys are taken once each.
            pairs = (
                self.stmt.select(self.pk.as_("_orm_pk"), *self.selected())
                .distinct()
                .as_("_orm_pairs")
            )
            self.stmt = (
                self.query_class.from_(self.base)
                .join(pairs)
                .on(pairs.field("_orm_pk") == self.pk)
            )
            self.values = {alias: pairs.field(alias) for alias in self.values}

    def selected(self) -> list[Any]:
        return [term.as_(alias) for alias, term in self.values.items()]

    def read(self, row: dict[str, Any]) -> dict[str, Any]:
        """Each path's value in *row*, as the field reads it."""
        return {alias: read(row[alias]) for alias, read in self._readers.items()}

    async def execute(self, stmt: Any) -> list[dict[str, Any]]:
        sql, values = stmt.get_parameterized_sql()
        return await self.db.execute_query_dict(sql, values)

    def aggregates(self, requested: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        """The requested aggregates over the model's columns, with their readers."""
        from pypika_tortoise import functions

        meta = self.model._meta  # type: ignore[attr-defined]
        found: dict[str, tuple[Any, Any]] = {}
        if requested.get("count"):
            found["_count"] = (functions.Count("*"), _same)
        for name, aggregate in (
            ("sum", functions.Sum),
            ("avg", functions.Avg),
            ("min", functions.Min),
            ("max", functions.Max),
        ):
            for field_name in requested.get(name, []):
                column = self.base.field(meta.fields_db_projection[field_name])
                # A raw row holds what the driver hands back, which for a
                # date or a boolean on SQLite is not what the field reads as.
                read = (
                    _same
                    if name == "avg"
                    else meta.fields_map[field_name].to_python_value
                )
                found[f"_{name}_{field_name}"] = (aggregate(column), read)
        return found

    def ordering(self, query: Any, order_input: Any) -> list[tuple[Any, bool, Any]]:
        """``(term, descending, nulls)`` for the order the connection reads in.

        The client's ``order`` when given, else the query's own. An order
        through a to-many relation is left out: its join would repeat rows.
        """
        meta = self.model._meta  # type: ignore[attr-defined]
        found: list[tuple[Any, bool, Any]] = []
        if order_input:
            entries = order_input if isinstance(order_input, list) else [order_input]
            for entry in entries:
                self._order_entry(entry, self.model, self.base, (), found)
            return found
        from pypika_tortoise import Order

        orderings = list(query._orderings)
        # Tortoise falls back to the model's ordering, but not for annotated queries.
        if not orderings and not query._annotations:
            orderings = list(meta.ordering)
        for name, order in orderings:
            column = meta.fields_db_projection.get(name)
            if column is not None:
                found.append((self.base.field(column), order == Order.desc, None))
        return found

    def _order_entry(
        self,
        entry: Any,
        model: type,
        table: Any,
        chain: tuple[str, ...],
        found: list[tuple[Any, bool, Any]],
    ) -> None:
        from pypika_tortoise import JoinType, Table

        meta = model._meta  # type: ignore[attr-defined]
        for key in entry.__class__.__dataclass_fields__:
            value = getattr(entry, key)
            if value is strawberry.UNSET or value is None:
                continue
            if key == "field":
                for name, descending, first, last in _build_tortoise_order_field(value):
                    nulls = "first" if first else "last" if last else None
                    column = table.field(meta.fields_db_projection[name])
                    found.append((column, descending, nulls))
            elif key == "object":
                for relation in value.__class__.__dataclass_fields__:
                    nested = getattr(value, relation)
                    if nested is strawberry.UNSET or nested is None:
                        continue
                    field = meta.fields_map[relation]
                    if type(field).__name__ not in _FORWARD_REL_TYPES:
                        continue
                    target = field.related_model
                    hop = (*chain, relation)
                    joined = self._joined.get(("_orm_order", *hop))
                    if joined is None:
                        joined = Table(target._meta.db_table).as_(
                            f"_orm_o{len(self._joined)}"
                        )
                        to_name = field.to_field_instance.model_field_name
                        self.stmt = self.stmt.join(joined, how=JoinType.left).on(
                            joined.field(target._meta.fields_db_projection[to_name])
                            == table.field(
                                meta.fields_db_projection[field.source_field]
                            )
                        )
                        self._joined[("_orm_order", *hop)] = joined
                    self._order_entry(nested, target, joined, hop, found)

    def _add(self, path: Any) -> None:
        entity, owner, chain = self.base, self.model, ()
        for hop in path.hops:
            chain = (*chain, hop.relation)
            if chain not in self._joined:
                self._joined[chain] = self._join(entity, hop)
            entity, owner = self._joined[chain], hop.target
        if path.custom is not None:
            term, read = self._custom(path), _same
        else:
            field = owner._meta.fields_map[path.column]
            term = entity.field(owner._meta.fields_db_projection[path.column])
            read = field.to_python_value
            if path.interval is not None:
                dialect = self.db.capabilities.dialect
                term = _truncate(term, field, path.interval, dialect)
                read = _same
        self.values[path.alias] = term
        self._readers[path.alias] = read

    def _join(self, entity: Any, hop: Any) -> Any:
        """Outer-join the rows of *hop*'s target the caller may see."""
        from pypika_tortoise import JoinType, Table

        alias = f"_orm_r{len(self._joined)}"
        restrict = self.backend.relation_scope(hop.model, hop.relation, self.info)
        rows = hop.target.all()
        if restrict is not None:
            rows = restrict(rows, self.info)
        field = hop.model._meta.fields_map[hop.relation]
        parent, target = hop.model._meta, hop.target._meta
        kind = type(field).__name__
        if kind == "ManyToManyFieldInstance":
            # The through table goes inside the scoped rows: joined outside, a
            # link to a hidden row would still add a row of its own.
            through = Table(field.through)
            visible = _pypika(rows).as_(f"{alias}_rows")
            joined = (
                self.query_class.from_(through)
                .join(visible)
                .on(
                    through.field(field.forward_key)
                    == visible.field(target.db_pk_column)
                )
                .select(
                    visible.star, through.field(field.backward_key).as_("_orm_parent")
                )
                .as_(alias)
            )
            on = joined.field("_orm_parent") == entity.field(parent.db_pk_column)
        elif kind in _FORWARD_REL_TYPES:
            joined = _pypika(rows).as_(alias)
            to_name = field.to_field_instance.model_field_name
            on = joined.field(target.fields_db_projection[to_name]) == entity.field(
                parent.fields_db_projection[field.source_field]
            )
        else:
            joined = _pypika(rows).as_(alias)
            to_name = field.to_field_instance.model_field_name
            on = joined.field(
                target.fields_db_projection[field.relation_field]
            ) == entity.field(parent.fields_db_projection[to_name])
        self.stmt = self.stmt.join(joined, how=JoinType.left).on(on)
        return joined

    def _custom(self, path: Any) -> Any:
        """The term a ``@group_field`` method groups by.

        The method may return a Tortoise expression (``F``, a function, a
        ``Case``), a field name, or a pypika term.
        """
        from pypika_tortoise import JoinType
        from tortoise.expressions import Expression, F, ResolveContext

        expr = invoke_custom_callback(
            path.custom,
            path.owner,
            query=self.model.all(),
            value=path.value,
            info=self.info,
        )
        if isinstance(expr, tuple):
            expr = expr[0]
        if isinstance(expr, str):
            expr = F(expr)
        if not isinstance(expr, Expression):
            return expr
        resolved = expr.resolve(
            ResolveContext(
                model=self.model, table=self.base, annotations={}, custom_filters={}
            )
        )
        for table, criterion in resolved.joins:
            self.stmt = self.stmt.join(table, how=JoinType.left).on(criterion)
        return resolved.term


def _same(value: Any) -> Any:
    return value


def _nulls_rank(term: Any, nulls: str) -> Any:
    """A term sorting *term*'s nulls before (``"first"``) or after the rest."""
    from pypika_tortoise import Case

    first = nulls == "first"
    return Case().when(term.isnull(), 0 if first else 1).else_(1 if first else 0)


def _order_by(stmt: Any, term: Any, descending: bool, nulls: str | None) -> Any:
    from pypika_tortoise import Order

    if nulls is not None:
        stmt = stmt.orderby(_nulls_rank(term, nulls))
    return stmt.orderby(term, order=Order.desc if descending else Order.asc)


def _truncate(term: Any, field: Any, interval: Any, dialect: str) -> Any:
    """*term* cut down to the start of its *interval*.

    PostgreSQL cuts a ``timestamptz`` in the session's time zone. SQLite keeps
    datetimes as text, so the start of the period is rendered as ISO text.
    The constants are written into the statement rather than bound: a bound
    value would make the grouped expression differ from the selected one.
    """
    from pypika_tortoise.functions import Cast, Function
    from pypika_tortoise.terms import LiteralValue

    from strawberry_orm.grouping import SQLITE_PERIOD_START
    from strawberry_orm.types import DateGroupByInterval

    is_datetime = type(field).__name__ == "DatetimeField"
    if dialect == "postgres":
        kind = LiteralValue(f"'{interval.value.lower()}'")
        truncated = Function("DATE_TRUNC", kind, term)
        return truncated if is_datetime else Cast(truncated, "DATE")
    if dialect == "sqlite":
        fmt = "'%Y-%m-%dT%H:%M:%S'" if is_datetime else "'%Y-%m-%d'"
        if interval is DateGroupByInterval.QUARTER:
            month = (
                Cast(Function("STRFTIME", LiteralValue("'%m'"), term), "INTEGER") - 1
            )
            months_in = month - month / 3 * 3
            modifiers: list[Any] = [
                LiteralValue("'start of month'"),
                Function("PRINTF", LiteralValue("'-%d months'"), months_in),
            ]
        else:
            modifiers = [
                LiteralValue(f"'{modifier}'")
                for modifier in SQLITE_PERIOD_START[interval]
            ]
        return Function("STRFTIME", LiteralValue(fmt), term, *modifiers)
    raise ValueError(
        f"Grouping by a date interval is supported on PostgreSQL and SQLite, "
        f"not {dialect}."
    )


def _ordered_to_the_row(query: Any) -> Any:
    """*query* in an order no two rows share: its own, then the primary key.

    Each page is cut by its own query, and a database breaks ties in a
    non-unique order however it likes, differently from one page to the next,
    so a row could land on two pages and another on none.
    """
    from pypika_tortoise import Order

    meta = query.model._meta
    orderings = list(query._orderings)
    # Tortoise falls back to the model's ordering, but not for annotated queries.
    if not orderings and not query._annotations:
        orderings = list(meta.ordering)
    if any(field == meta.pk_attr for field, _ in orderings):
        return query
    ordered = query._clone()
    ordered._orderings = [*orderings, (meta.pk_attr, Order.asc)]
    return ordered
