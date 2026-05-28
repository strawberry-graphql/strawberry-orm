"""Django backend -- built from scratch using Django model introspection."""

from __future__ import annotations

import datetime
from decimal import Decimal
from inspect import iscoroutinefunction
from typing import Any

import strawberry
from strawberry.extensions import SchemaExtension

from strawberry_orm._async import async_safe_resolver, materialize_result, run_sync
from strawberry_orm.backends.filter_pk_shortcut import (
    build_reference_object_filter_clause,
)
from strawberry_orm.filters import is_fk_shortcut_lookup, is_reference_lookup
from strawberry_orm.optimizer import OptimizerExtension

from ._base import (
    AggregateMeta,
    BaseBackend,
    extract_element_type,
    input_to_dict,
    invoke_custom_callback,
    requested_aggregates,
)

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
            field_class_name = type(field).__name__

            if field_class_name in (
                "ManyToManyField",
                "ManyToManyRel",
                "ManyToOneRel",
                "OneToOneRel",
                "ForeignObject",
            ):
                related_model = (
                    field.related_model if hasattr(field, "related_model") else None
                )
                result.append((field.name, Any, True, related_model))
                continue

            if field_class_name in ("ForeignKey", "OneToOneField"):
                related_model = (
                    field.related_model if hasattr(field, "related_model") else None
                )
                result.append((field.name, Any, True, related_model))
                attname = getattr(field, "attname", None)
                if attname and attname != field.name:
                    fk_type: type = int
                    if related_model is not None:
                        pk_field = related_model._meta.pk
                        if pk_field is not None:
                            fk_type = _DJANGO_FIELD_MAP.get(
                                type(pk_field).__name__, str
                            )
                    if getattr(field, "null", False):
                        fk_type = fk_type | None  # type: ignore[assignment]
                    result.append((attname, fk_type, False, related_model))
                continue

            py_type = _DJANGO_FIELD_MAP.get(field_class_name, str)
            result.append((field.name, py_type, False, None))

        return result

    def _get_pk_names(self, model: type) -> set[str]:
        meta = model._meta  # type: ignore[attr-defined]
        if meta.pk is None:
            return set()
        return {meta.pk.name}

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

            meta = model._meta  # type: ignore[attr-defined]

            col_types: dict[str, type] = {}
            rel_fields: dict[str, dict[str, Any]] = {}

            for field_obj in meta.get_fields():
                field_class_name = type(field_obj).__name__
                if field_class_name in (
                    "ManyToManyField",
                    "ManyToManyRel",
                    "ManyToOneRel",
                    "OneToOneRel",
                ):
                    rel_fields[field_obj.name] = {
                        "kind": "many" if field_class_name != "OneToOneRel" else "one",
                        "model": field_obj.related_model,
                    }
                    continue
                if field_class_name in ("ForeignKey", "OneToOneField"):
                    rel_fields[field_obj.name] = {
                        "kind": "fk" if field_class_name == "ForeignKey" else "one",
                        "model": field_obj.related_model,
                    }
                    attname = getattr(field_obj, "attname", None)
                    if attname and attname != field_obj.name:
                        fk_type: type = int
                        if getattr(field_obj, "null", False):
                            fk_type = int | None
                        col_types[attname] = fk_type
                    continue
                py_type = _DJANGO_FIELD_MAP.get(field_class_name, str)
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

                        def _make_resolver(fname: str, return_ann: Any) -> Any:
                            def resolver(self: Any) -> Any:
                                return list(getattr(self, fname).all())

                            resolver.__name__ = fname
                            resolver.__annotations__ = {"return": return_ann}
                            if self._django_async_safe:
                                resolver = async_safe_resolver(resolver)
                            return strawberry.field(resolver=resolver)

                        setattr(cls, field_name, _make_resolver(field_name, ann))
                elif kind in ("fk", "one"):

                    def _make_fk_resolver(fname: str, return_ann: Any) -> Any:
                        def resolver(self: Any) -> Any:
                            return getattr(self, fname)

                        resolver.__name__ = fname
                        resolver.__annotations__ = {"return": return_ann}
                        if self._django_async_safe:
                            resolver = async_safe_resolver(
                                resolver,
                                materialize=False,
                            )
                        return strawberry.field(resolver=resolver)

                    setattr(cls, field_name, _make_fk_resolver(field_name, ann))

            annotations = getattr(cls, "__annotations__", {})
            self._check_lazy_relation_fields(cls, model, annotations)
            graphql_type = self._finalize_type(cls, model, type_name, name)
            if self._django_async_safe:
                graphql_type = self._post_process_strawberry_fields(graphql_type)
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
        from strawberry_orm.repo import _check_auth

        def apply() -> None:
            manager = getattr(instance, field)
            rel_model = manager.model
            repo = self.get_repo(rel_model) if not authorize else None

            to_add: list[Any] = []
            to_unlink: list[Any] = []
            to_delete_ids: list[Any] = []

            for ref in refs:
                ref_create = getattr(ref, "create", strawberry.UNSET)
                ref_update = getattr(ref, "update", strawberry.UNSET)
                ref_unlink = getattr(ref, "unlink", strawberry.UNSET)
                ref_delete = getattr(ref, "delete", strawberry.UNSET)

                if ref_create is not strawberry.UNSET and ref_create is not None:
                    if authorize and not authorize("create", rel_model, None, info):
                        continue
                    data = input_to_dict(ref_create)
                    _check_auth(repo, "can_create", data, info)
                    obj = rel_model.objects.create(**data)
                    to_add.append(obj)
                elif ref_update is not strawberry.UNSET and ref_update is not None:
                    data = input_to_dict(ref_update)
                    pk = data.pop("id")
                    if authorize and not authorize("update", rel_model, pk, info):
                        continue
                    if repo is not None:
                        obj = repo._get(rel_model, pk, info)
                    else:
                        obj = rel_model.objects.filter(pk=pk).first()
                    if obj is not None:
                        _check_auth(repo, "can_update", obj, data, info)
                        _check_auth(repo, "can_link", instance, field, obj, info)
                        if data:
                            for k, v in data.items():
                                setattr(obj, k, v)
                            obj.save()
                        to_add.append(obj)
                elif ref_unlink is not strawberry.UNSET and ref_unlink is not None:
                    if authorize and not authorize(
                        "unlink", rel_model, ref_unlink.id, info
                    ):
                        continue
                    if repo is not None:
                        obj = repo._get(rel_model, ref_unlink.id, info)
                    else:
                        obj = rel_model.objects.filter(pk=ref_unlink.id).first()
                    if obj is not None:
                        _check_auth(repo, "can_unlink", instance, field, obj, info)
                        to_unlink.append(ref_unlink.id)
                elif ref_delete is not strawberry.UNSET and ref_delete is not None:
                    if authorize and not authorize(
                        "delete", rel_model, ref_delete.id, info
                    ):
                        continue
                    if repo is not None:
                        obj = repo._get(rel_model, ref_delete.id, info)
                    else:
                        obj = rel_model.objects.filter(pk=ref_delete.id).first()
                    if obj is not None:
                        _check_auth(repo, "can_delete", obj, info)
                        to_delete_ids.append(ref_delete.id)

            if to_add:
                manager.add(*to_add)
            if to_unlink:
                manager.remove(*rel_model.objects.filter(pk__in=to_unlink))
            if to_delete_ids:
                manager.remove(*rel_model.objects.filter(pk__in=to_delete_ids))
                rel_model.objects.filter(pk__in=to_delete_ids).delete()

        return run_sync(apply, thread_sensitive=True)

    # -- Query application ----------------------------------------------------

    def apply_filters(self, query: Any, filter_input: Any, model: type) -> Any:
        q_obj, query = _build_django_filter(
            filter_input,
            query=query,
            model=model,
            max_depth=self._max_filter_depth,
            max_branches=self._max_filter_branches,
            enable_regex=self._enable_regex_filters,
            max_in_list_size=self._max_in_list_size,
        )
        if q_obj is not None:
            query = query.filter(q_obj)
        return query

    def apply_ordering(self, query: Any, order_input: Any, model: type) -> Any:
        order_list = order_input if isinstance(order_input, list) else [order_input]
        clauses: list[Any] = []
        for entry in order_list:
            entry_clauses, query = _build_django_ordering(entry, query=query)
            clauses.extend(entry_clauses)
        if clauses:
            query = query.order_by(*clauses)
        return query

    # -- Grouping / aggregation -----------------------------------------------

    def apply_aggregation(
        self, query: Any, info: Any, aggregate_meta: AggregateMeta
    ) -> Any:
        from django.db.models import Avg, Count, Max, Min, Sum

        requested = requested_aggregates(info, "aggregates") or {}
        if not requested:
            return aggregate_meta.aggregates_type(count=0)

        agg_kwargs: dict[str, Any] = {}
        if requested.get("count"):
            agg_kwargs["_count"] = Count("*")
        _DJANGO_AGG = {"sum": Sum, "avg": Avg, "min": Min, "max": Max}
        for func_name, AggCls in _DJANGO_AGG.items():
            for fname in requested.get(func_name, []):
                agg_kwargs[f"_{func_name}_{fname}"] = AggCls(fname)

        if not agg_kwargs:
            return aggregate_meta.aggregates_type(count=0)

        def _run():
            result = query.aggregate(**agg_kwargs)
            row = type("Row", (), result)()
            return aggregate_meta.build_aggregates(row, requested)

        return run_sync(_run, thread_sensitive=True)

    def apply_grouping(
        self,
        query: Any,
        group_by_input: Any,
        info: Any,
        aggregate_meta: AggregateMeta,
        *,
        order_input: Any | None = None,
    ) -> list[Any]:
        from django.db.models import Avg, Count, Max, Min, Sum

        group_by_list = (
            group_by_input if isinstance(group_by_input, list) else [group_by_input]
        )
        group_fields, group_key_fields = _extract_django_group_fields(group_by_list)
        aggregate_meta.group_key_fields = group_key_fields

        if not group_fields:
            return []

        requested = requested_aggregates(info, "groups.aggregates") or {}
        agg_kwargs: dict[str, Any] = {}
        if requested.get("count"):
            agg_kwargs["_count"] = Count("*")
        _DJANGO_AGG = {"sum": Sum, "avg": Avg, "min": Min, "max": Max}
        for func_name, AggCls in _DJANGO_AGG.items():
            for fname in requested.get(func_name, []):
                agg_kwargs[f"_{func_name}_{fname}"] = AggCls(fname)

        def _run():
            qs = query.values(*group_fields).annotate(**agg_kwargs)
            if order_input:
                order_clauses = _extract_django_overlapping_order(
                    order_input, set(group_key_fields)
                )
                if order_clauses:
                    qs = qs.order_by(*order_clauses)
            results = list(qs)
            groups = []
            for row_dict in results:
                row = type("Row", (), row_dict)()
                key = aggregate_meta.build_group_key(row, group_key_fields)
                aggregates = aggregate_meta.build_aggregates(row, requested)
                group_obj = type(
                    "_Group",
                    (),
                    {
                        "key": key,
                        "aggregates": aggregates,
                        "edge_indices": [],
                        "_items_nodes": None,
                        "_orm_base_query": None,
                        "_orm_backend": None,
                        "_orm_model": None,
                    },
                )()
                groups.append(group_obj)
            return groups

        return run_sync(_run, thread_sensitive=True)

    def scope_query_to_group(self, query: Any, group_key: Any) -> Any:
        key_fields = group_key.__class__.__dataclass_fields__
        filters: dict[str, Any] = {}
        for fname in key_fields:
            val = getattr(group_key, fname, None)
            if val is not None:
                filters[fname] = val
        return query.filter(**filters)

    def batch_group_items(
        self,
        query: Any,
        group_key_fields: list[str],
        info: Any,
        model: type,
        *,
        per_group_limit: int,
        order_input: Any | None = None,
    ) -> dict[tuple, list[Any]]:
        from collections import defaultdict

        from django.db.models import F, Window
        from django.db.models.functions import RowNumber

        partition = [F(k) for k in group_key_fields]
        if order_input:
            ordering = _build_django_order_from_input(order_input)
        else:
            ordering = [F("pk")]

        def _run():
            qs = query.annotate(
                _rn=Window(
                    expression=RowNumber(),
                    partition_by=partition,
                    order_by=ordering,
                )
            ).filter(_rn__lte=per_group_limit)
            rows = list(qs)
            items_by_key: dict[tuple, list[Any]] = defaultdict(list)
            for row in rows:
                key = tuple(
                    str(getattr(row, k)) if getattr(row, k) is not None else None
                    for k in group_key_fields
                )
                items_by_key[key].append(row)
            return dict(items_by_key)

        return run_sync(_run, thread_sensitive=True)

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type) -> Any:
        qs = model.objects.all()
        if self._default_query_limit is not None:
            qs = qs[: self._default_query_limit]
        return qs

    def is_query_object(self, value: Any) -> bool:
        from django.db.models import QuerySet

        return isinstance(value, QuerySet)

    def materialize_query(self, query: Any, info: Any) -> Any:
        from strawberry_orm._async import in_async_context

        if not in_async_context():
            return list(query)
        return run_sync(list, query, thread_sensitive=True)

    def _post_process_strawberry_fields(self, graphql_type: type) -> type:
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
                    resolver.wrapped_func = async_safe_resolver(wrapped)
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

                return async_safe_resolver(_resolve_basic, materialize=False)

            field_type = field.type
            field.base_resolver = StrawberryResolver(
                _make_resolve_basic(),
                type_override=field_type if field_type is not UNRESOLVED else None,
            )

        return graphql_type

    # -- Optimizer -----------------------------------------------------------

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return OptimizerExtension.configure(backend=self, store=self._store)

    def apply_optimizer_hints(self, store: Any, query: Any, info: Any) -> Any:
        import re

        from strawberry_orm.optimizer.selections import (
            fragments_from_info,
            iter_field_nodes,
        )

        def optimize() -> Any:
            try:
                model = query.model
            except AttributeError:
                return query

            optimized_query = query
            get_qs = self._type_querysets.get(model)
            if get_qs is not None:
                optimized_query = get_qs(optimized_query, info)

            select_related: list[str] = []
            prefetch_related: list[Any] = []
            fragments = fragments_from_info(info)

            def _to_snake(name: str) -> str:
                return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()

            def _get_nested_queryset(
                parent_model: type,
                field_name: str,
                related_model: type,
            ) -> Any:
                """Build a custom queryset if the field has a load callable or
                the related model's type defines get_queryset."""
                get_qs = self._type_querysets.get(related_model)

                type_name = self._type_name_for_model(parent_model)
                load_fn = None
                if type_name and store:
                    hints = store.get(type_name, field_name)
                    if hints and callable(hints.load):
                        load_fn = hints.load

                if get_qs is None and load_fn is None:
                    return None

                qs = related_model.objects.all()
                if get_qs is not None:
                    qs = get_qs(qs, info)
                if load_fn is not None:
                    qs = load_fn(qs)
                return qs

            def _walk_selections(
                selection_set: Any,
                current_model: type,
                prefix: str = "",
                in_prefetch: bool = False,
            ) -> None:
                if selection_set is None:
                    return
                meta = current_model._meta  # type: ignore[attr-defined]
                for node in iter_field_nodes(selection_set, fragments):
                    field_name = _to_snake(node.name.value)
                    full_path = f"{prefix}__{field_name}" if prefix else field_name
                    try:
                        field_obj = meta.get_field(field_name)
                    except Exception:
                        continue

                    field_class_name = type(field_obj).__name__

                    if field_class_name in ("ForeignKey", "OneToOneField"):
                        attname = getattr(field_obj, "attname", None)
                        if (
                            attname
                            and field_name == attname
                            and field_name != field_obj.name
                        ):
                            continue
                        related_model = field_obj.related_model
                        custom_qs = _get_nested_queryset(
                            current_model, field_name, related_model
                        )
                        if custom_qs is not None:
                            from django.db.models import Prefetch

                            prefetch_related.append(
                                Prefetch(full_path, queryset=custom_qs)
                            )
                            if node.selection_set:
                                _walk_selections(
                                    node.selection_set,
                                    related_model,
                                    full_path,
                                    in_prefetch=True,
                                )
                        elif in_prefetch:
                            prefetch_related.append(full_path)
                            if node.selection_set:
                                _walk_selections(
                                    node.selection_set,
                                    related_model,
                                    full_path,
                                    in_prefetch,
                                )
                        else:
                            select_related.append(full_path)
                            if node.selection_set:
                                _walk_selections(
                                    node.selection_set,
                                    related_model,
                                    full_path,
                                    in_prefetch,
                                )
                    elif field_class_name == "OneToOneRel":
                        related_model = field_obj.related_model
                        custom_qs = _get_nested_queryset(
                            current_model, field_name, related_model
                        )
                        if custom_qs is not None:
                            from django.db.models import Prefetch

                            prefetch_related.append(
                                Prefetch(full_path, queryset=custom_qs)
                            )
                            if node.selection_set:
                                _walk_selections(
                                    node.selection_set,
                                    related_model,
                                    full_path,
                                    in_prefetch=True,
                                )
                        elif in_prefetch:
                            prefetch_related.append(full_path)
                            if node.selection_set:
                                _walk_selections(
                                    node.selection_set,
                                    related_model,
                                    full_path,
                                    in_prefetch,
                                )
                        else:
                            select_related.append(full_path)
                            if node.selection_set:
                                _walk_selections(
                                    node.selection_set,
                                    related_model,
                                    full_path,
                                    in_prefetch,
                                )
                    elif field_class_name in (
                        "ManyToManyField",
                        "ManyToManyRel",
                        "ManyToOneRel",
                    ):
                        related_model = field_obj.related_model
                        custom_qs = _get_nested_queryset(
                            current_model, field_name, related_model
                        )
                        if custom_qs is not None:
                            from django.db.models import Prefetch

                            prefetch_related.append(
                                Prefetch(full_path, queryset=custom_qs)
                            )
                        else:
                            prefetch_related.append(full_path)
                        if node.selection_set:
                            _walk_selections(
                                node.selection_set,
                                related_model,
                                full_path,
                                in_prefetch=True,
                            )

                    type_name = self._type_name_for_model(current_model)
                    if type_name and store:
                        hints = store.get(type_name, field_name)
                        if (
                            hints
                            and not hints.disable_optimization
                            and hints.load
                            and not callable(hints.load)
                        ):
                            for rel_name in hints.load:
                                try:
                                    rel_field = meta.get_field(rel_name)
                                except Exception:
                                    continue
                                rel_class = type(rel_field).__name__
                                rel_path = (
                                    f"{prefix}__{rel_name}" if prefix else rel_name
                                )
                                is_fk = rel_class in (
                                    "ForeignKey",
                                    "OneToOneField",
                                    "OneToOneRel",
                                )
                                if is_fk and not in_prefetch:
                                    select_related.append(rel_path)
                                else:
                                    prefetch_related.append(rel_path)

            for field_node in info.field_nodes:
                _walk_selections(field_node.selection_set, model)

            only_fields: list[str] = []

            type_name_root = self._type_name_for_model(model)
            if type_name_root and store:
                for field_node in info.field_nodes:
                    if field_node.selection_set:
                        for sel in iter_field_nodes(
                            field_node.selection_set, fragments
                        ):
                            fname = _to_snake(sel.name.value)
                            hints = store.get(type_name_root, fname)
                            if hints and hints.only:
                                only_fields.extend(hints.only)

            if select_related:
                optimized_query = optimized_query.select_related(*select_related)
            if prefetch_related:
                optimized_query = optimized_query.prefetch_related(*prefetch_related)
            if only_fields:
                optimized_query = optimized_query.only(*only_fields)

            return list(optimized_query)

        return run_sync(optimize, thread_sensitive=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_dj_rel_resolver(
    backend: Any,
    fname: str,
    rel_model: type,
    filter_type: Any,
    order_type: Any,
) -> Any:
    """Create a Strawberry field for a Django many-relation with filter/order."""

    def _resolve(
        self: Any,
        filter: Any = None,
        order: Any = None,
    ) -> list[Any]:
        qs = getattr(self, fname).all()
        if filter is not None:
            qs = backend.apply_filters(qs, filter, rel_model)
        if order is not None:
            qs = backend.apply_ordering(qs, order, rel_model)
        return list(qs)

    if filter_type and order_type:

        def resolver(
            self: Any,
            filter: Any = None,
            order: Any = None,
        ) -> list[Any]:
            return _resolve(self, filter=filter, order=order)

        resolver.__annotations__ = {
            "filter": filter_type | None,
            "order": list[order_type] | None,
        }
    elif filter_type:

        def resolver(self: Any, filter: Any = None) -> list[Any]:
            return _resolve(self, filter=filter, order=None)

        resolver.__annotations__ = {"filter": filter_type | None}
    else:

        def resolver(self: Any, order: Any = None) -> list[Any]:
            return _resolve(self, filter=None, order=order)

        resolver.__annotations__ = {"order": list[order_type] | None}

    if getattr(backend, "_django_async_safe", False):
        resolver = async_safe_resolver(resolver)
    return strawberry.field(resolver=resolver)


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


def _django_forward_fk_attname(model: type, rel_name: str) -> str | None:
    from django.db.models.fields.related import ForeignKey, OneToOneField

    try:
        field = model._meta.get_field(rel_name)  # type: ignore[attr-defined]
    except Exception:
        return None
    if isinstance(field, (ForeignKey, OneToOneField)):
        return field.attname
    return None


def _django_related_model(model: type, rel_name: str) -> type | None:
    from django.db.models.fields.related import ForeignKey, OneToOneField

    try:
        field = model._meta.get_field(rel_name)  # type: ignore[attr-defined]
    except Exception:
        return None
    if isinstance(field, (ForeignKey, OneToOneField)):
        return field.remote_field.model
    return None


def _build_django_filter(
    filter_input: Any,
    *,
    query: Any = None,
    model: type | None = None,
    info: Any = None,
    max_depth: int = 10,
    max_branches: int = 50,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
    _depth: int = 0,
    _prefix: str = "",
) -> tuple[Any, Any]:
    """Return ``(Q_clause | None, query)``."""
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
                rel_model = (
                    _django_related_model(model, rel_name)
                    if model is not None
                    else None
                )
                if model is not None:
                    fk_attname = _django_forward_fk_attname(model, rel_name)
                    if fk_attname is not None:
                        fk_prefix = f"{_prefix}{fk_attname}"
                        fk_clause = build_reference_object_filter_clause(
                            nested_filter,
                            build_field_clause=_build_django_reference_field_clause,
                            custom_filter_keys=custom_filter_keys,
                            max_branches=max_branches,
                            fk_prefix=fk_prefix,
                            enable_regex=enable_regex,
                            max_in_list_size=max_in_list_size,
                        )
                        if fk_clause is not None:
                            return fk_clause, query
                return _build_django_filter(
                    nested_filter,
                    query=query,
                    model=rel_model,
                    info=info,
                    max_depth=max_depth,
                    max_branches=max_branches,
                    enable_regex=enable_regex,
                    max_in_list_size=max_in_list_size,
                    _depth=_depth + 1,
                    _prefix=f"{_prefix}{rel_name}__",
                )
        elif key == "all":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            combined = Q()
            has_clause = False
            for f in val:
                sub_clause, query = _build_django_filter(
                    f, **{**recurse_kw, "query": query}
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

        val = _coerce_reference_value(val)

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
                sub_clauses, query = _build_django_ordering(
                    nested, _prefix=f"{_prefix}{rel_name}__", query=query, info=info
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


def _extract_django_group_fields(
    group_by_list: list[Any],
) -> tuple[list[str], list[str]]:
    """Extract field names for Django GROUP BY from group-by input."""

    fields: list[str] = []
    key_fields: list[str] = []
    seen: set[str] = set()

    for entry in group_by_list:
        field_val = getattr(entry, "field", None)
        if field_val is None or field_val is strawberry.UNSET:
            continue
        entry_fields = field_val.__class__.__dataclass_fields__
        for col_name in entry_fields:
            val = getattr(field_val, col_name)
            if val is strawberry.UNSET or val is None:
                continue
            if col_name in seen:
                continue
            seen.add(col_name)
            fields.append(col_name)
            key_fields.append(col_name)

    return fields, key_fields


def _extract_django_overlapping_order(
    order_input: Any, group_field_names: set[str]
) -> list[str]:
    """Extract Django order_by strings for group fields overlapping with root order."""
    order_list = order_input if isinstance(order_input, list) else [order_input]
    clauses: list[str] = []

    for entry in order_list:
        field_val = getattr(entry, "field", None)
        if field_val is None or field_val is strawberry.UNSET:
            continue
        entry_fields = field_val.__class__.__dataclass_fields__
        for col_name in entry_fields:
            direction = getattr(field_val, col_name)
            if direction is strawberry.UNSET or direction is None:
                continue
            if col_name not in group_field_names:
                continue
            dir_value = (
                direction.value if hasattr(direction, "value") else str(direction)
            )
            if dir_value.startswith("DESC"):
                clauses.append(f"-{col_name}")
            else:
                clauses.append(col_name)

    return clauses


def _build_django_order_from_input(order_input: Any) -> list[Any]:
    """Convert an order input to Django F() ordering expressions."""
    from django.db.models import F

    order_list = order_input if isinstance(order_input, list) else [order_input]
    clauses: list[Any] = []
    for entry in order_list:
        field_val = getattr(entry, "field", None)
        if field_val is None or field_val is strawberry.UNSET:
            continue
        entry_fields = field_val.__class__.__dataclass_fields__
        for col_name in entry_fields:
            direction = getattr(field_val, col_name)
            if direction is strawberry.UNSET or direction is None:
                continue
            dir_value = (
                direction.value if hasattr(direction, "value") else str(direction)
            )
            if dir_value.startswith("DESC"):
                clauses.append(F(col_name).desc())
            else:
                clauses.append(F(col_name).asc())
    if not clauses:
        clauses.append(F("pk"))
    return clauses
