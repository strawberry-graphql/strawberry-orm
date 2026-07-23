"""Tortoise ORM backend -- built from scratch (no existing strawberry integration)."""

from __future__ import annotations

import datetime
import importlib
import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

import strawberry
from strawberry.extensions import SchemaExtension

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

_QUERY_ORDERINGS: dict[int, list[tuple[str, bool, bool | None, bool | None]]] = {}


def _primary_key(value: Any) -> Any:
    return getattr(value, "id", getattr(value, "pk", None))


def _remember_query_ordering(
    query: Any,
    orderings: list[tuple[str, bool, bool | None, bool | None]],
) -> None:
    _QUERY_ORDERINGS[id(query)] = orderings


def _query_orderings(
    query: Any,
) -> list[tuple[str, bool, bool | None, bool | None]] | None:
    return _QUERY_ORDERINGS.get(id(query))


def _coalesce_tortoise_prefetch_paths(model: type, paths: list[str]) -> list[Any]:
    """Merge nested prefetch strings into ``Prefetch`` objects for reverse relations.

    Tortoise does not always apply nested prefetches such as ``posts__tags`` on
    reverse FK relations; building an explicit ``Prefetch('posts', Post...tags)``
    queryset matches what ``fetch_related`` does for direct models.
    """
    from tortoise.query_utils import Prefetch

    nested: dict[str, list[str]] = {}
    roots: list[str] = []
    for path in paths:
        if "__" in path:
            root, sub = path.split("__", 1)
            nested.setdefault(root, []).append(sub)
        else:
            roots.append(path)

    result: list[Any] = []
    used_roots: set[str] = set()
    meta = model._meta  # type: ignore[attr-defined]
    for root, subs in nested.items():
        field = meta.fields_map.get(root)
        related_model = (
            getattr(field, "related_model", None) if field is not None else None
        )
        if related_model is None:
            continue
        qs = related_model.all()
        if subs:
            qs = qs.prefetch_related(*subs)
        result.append(Prefetch(root, qs))
        used_roots.add(root)

    for root in roots:
        if root not in used_roots and root in meta.fields_map:
            result.append(root)
    return result


class _CustomRel:
    """Holds metadata for a relationship that uses a custom queryset."""

    __slots__ = (
        "full_path",
        "field_name",
        "related_model",
        "fk_col",
        "qs_fn",
        "sub_prefetches",
    )

    def __init__(
        self,
        full_path: str,
        field_name: str,
        related_model: type,
        fk_col: str | None,
        qs_fn: Any,
        sub_prefetches: list[str],
    ) -> None:
        self.full_path = full_path
        self.field_name = field_name
        self.related_model = related_model
        self.fk_col = fk_col
        self.qs_fn = qs_fn
        self.sub_prefetches = sub_prefetches


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
                el_type = extract_element_type(ann)
                if el_type is None:
                    continue

                f_type = getattr(el_type, "__orm_filter__", None)
                o_type = getattr(el_type, "__orm_order__", None)
                rel_model = rel_fields[field_name]["model"]

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
                            rel_value = getattr(self, fname)
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

    def apply_filters(self, query: Any, filter_input: Any, model: type) -> Any:
        q_obj, query = _build_tortoise_filter(
            filter_input,
            query=query,
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
        clauses: list[str] = []
        python_orderings: list[tuple[str, bool, bool | None, bool | None]] = []
        for entry in order_list:
            entry_orderings, query = _build_tortoise_ordering(entry, query=query)
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
        from strawberry_orm.repo import _check_auth

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

            if ref_create is not strawberry.UNSET and ref_create is not None:
                if authorize and not authorize("create", rel_model, None, info):
                    continue
                data = input_to_dict(ref_create)
                _check_auth(repo, "can_create", data, info)
                obj = await rel_model.create(**data)
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
                    _check_auth(repo, "can_update", obj, data, info)
                    _check_auth(repo, "can_link", instance, field, obj, info)
                    if data:
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

        result = await query.annotate(**agg_kwargs).values(*agg_kwargs.keys())
        if result:
            row = type("Row", (), result[0])()
        else:
            row = type("Row", (), {k: 0 for k in agg_kwargs})()  # pragma: no cover
        return aggregate_meta.build_aggregates(row, requested)

    async def apply_grouping(
        self,
        query: Any,
        group_by_input: Any,
        info: Any,
        aggregate_meta: AggregateMeta,
        *,
        order_input: Any | None = None,
    ) -> list[Any]:
        from tortoise.functions import Avg, Count, Max, Min, Sum

        group_by_list = (
            group_by_input if isinstance(group_by_input, list) else [group_by_input]
        )
        group_fields, group_key_fields = _extract_tortoise_group_fields(group_by_list)
        aggregate_meta.group_key_fields = group_key_fields

        if not group_fields:
            return []

        requested = requested_aggregates(info, "groups.aggregates") or {}
        agg_kwargs: dict[str, Any] = {}
        if requested.get("count"):
            agg_kwargs["_count"] = Count("id")
        _TORT_AGG = {"sum": Sum, "avg": Avg, "min": Min, "max": Max}
        for func_name, AggCls in _TORT_AGG.items():
            for fname in requested.get(func_name, []):
                agg_kwargs[f"_{func_name}_{fname}"] = AggCls(fname)

        qs = (
            query.annotate(**agg_kwargs)
            .group_by(*group_fields)
            .values(*group_fields, *agg_kwargs.keys())
        )
        if order_input:
            order_clauses = _extract_tortoise_overlapping_order(
                order_input, set(group_key_fields)
            )
            if order_clauses:
                qs = qs.order_by(*order_clauses)

        rows = await qs
        groups = []
        for row_dict in rows:
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

    def scope_query_to_group(self, query: Any, group_key: Any) -> Any:
        key_fields = group_key.__class__.__dataclass_fields__
        filters: dict[str, Any] = {}
        for fname in key_fields:
            val = getattr(group_key, fname, None)
            if val is not None:
                filters[fname] = val
        return query.filter(**filters)

    async def batch_group_items(
        self,
        query: Any,
        group_key_fields: list[str],
        info: Any,
        model: type,
        *,
        per_group_limit: int,
        order_input: Any | None = None,
    ) -> dict[tuple, list[Any]]:
        """Per-group fallback since Tortoise has limited window function support."""
        groups_qs = await query.group_by(*group_key_fields).values(*group_key_fields)
        items_by_key: dict[tuple, list[Any]] = {}

        for group_dict in groups_qs:
            key = tuple(
                str(group_dict[k]) if group_dict[k] is not None else None
                for k in group_key_fields
            )
            scoped = query.filter(**{k: group_dict[k] for k in group_key_fields})
            if order_input:
                order_clauses = _build_tortoise_order_from_input(order_input)
                if order_clauses:
                    scoped = scoped.order_by(*order_clauses)
            items = await scoped.limit(per_group_limit)
            items_by_key[key] = list(items)

        return items_by_key

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type) -> Any:
        qs = model.all()
        if self._default_query_limit is not None:
            qs = qs.limit(self._default_query_limit)
        return qs

    def is_query_object(self, value: Any) -> bool:
        from tortoise.queryset import QuerySet

        return isinstance(value, QuerySet)

    async def count_query(self, query: Any, info: Any) -> int:
        return await query.count()

    async def materialize_query(self, query: Any, info: Any) -> list[Any]:
        return _apply_python_ordering(
            list(await query),
            _query_orderings(query),
        )

    # -- Optimizer -----------------------------------------------------------

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return OptimizerExtension.configure(backend=self, store=self._store)

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

        type_name = self._type_name_for_model(parent_model)
        if type_name:
            hints = self._store.get(type_name, field_name)
            if hints and callable(hints.load):
                qs = hints.load(qs)

        return qs

    async def apply_optimizer_hints(
        self,
        store: Any,
        query: Any,
        info: Any,
    ) -> Any:
        from strawberry_orm.optimizer.selections import (
            field_nodes_from_info,
            fragments_from_info,
            iter_field_nodes,
        )

        orderings = _query_orderings(query)
        fragments = fragments_from_info(info)

        try:
            model = query.model
        except AttributeError:
            return _apply_python_ordering(
                list(await query),
                orderings,
            )

        get_qs = self._type_querysets.get(model)
        if get_qs is not None:
            query = get_qs(query, info)

        prefetch_paths: list[str] = []
        custom_rels: list[_CustomRel] = []
        custom_sub_prefetches: dict[str, list[str]] = {}

        def _to_snake(name: str) -> str:
            return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()

        def _get_custom_qs(
            parent_model: type,
            field_name: str,
            related_model: type,
        ) -> Any:
            """Return a load callable or None."""
            nested_get_qs = self._type_querysets.get(related_model)
            type_name = self._type_name_for_model(parent_model)
            load_fn = None
            if type_name and store:
                hints = store.get(type_name, field_name)
                if hints and callable(hints.load):
                    load_fn = hints.load
            if nested_get_qs is None and load_fn is None:
                return None

            def combined(qs: Any) -> Any:
                if nested_get_qs is not None:
                    qs = nested_get_qs(qs, info)
                if load_fn is not None:
                    qs = load_fn(qs)
                return qs

            return combined

        def _find_custom_ancestor(full_path: str) -> str | None:
            for cp in custom_sub_prefetches:
                if full_path.startswith(cp + "__"):
                    return cp
            return None

        def _walk_selections(
            selection_set: Any,
            current_model: type,
            prefix: str = "",
        ) -> None:
            if selection_set is None:
                return  # pragma: no cover
            meta = current_model._meta  # type: ignore[attr-defined]
            for node in iter_field_nodes(selection_set, fragments):
                field_name = _to_snake(node.name.value)
                full_path = f"{prefix}__{field_name}" if prefix else field_name

                if field_name not in meta.fields_map:
                    continue  # pragma: no cover
                field_obj = meta.fields_map[field_name]
                field_cls = type(field_obj).__name__

                is_rel = (
                    field_cls == "ForeignKeyFieldInstance"
                    or field_cls in _MANY_REL_TYPES
                )
                if not is_rel:
                    continue

                related_model = field_obj.related_model

                ancestor = _find_custom_ancestor(full_path)
                if ancestor is not None:
                    sub_path = full_path[len(ancestor) + 2 :]
                    custom_sub_prefetches[ancestor].append(sub_path)
                    if node.selection_set:
                        _walk_selections(
                            node.selection_set,
                            related_model,
                            full_path,
                        )
                    continue

                custom = _get_custom_qs(
                    current_model,
                    field_name,
                    related_model,
                )
                if custom is not None:
                    if field_cls == "ForeignKeyFieldInstance":
                        fk_col = _get_reverse_fk_field(
                            related_model,
                            current_model,
                            field_name,
                        )
                    elif field_cls == "BackwardFKRelation":
                        fk_col = field_obj.relation_field
                    elif field_cls == "ManyToManyFieldInstance":
                        fk_col = None
                    else:
                        fk_col = getattr(
                            field_obj, "relation_field", None
                        )  # pragma: no cover

                    custom_sub_prefetches[full_path] = []
                    custom_rels.append(
                        _CustomRel(
                            full_path=full_path,
                            field_name=field_name,
                            related_model=related_model,
                            fk_col=fk_col,
                            qs_fn=custom,
                            sub_prefetches=custom_sub_prefetches[full_path],
                        )
                    )
                else:
                    prefetch_paths.append(full_path)

                if node.selection_set:
                    _walk_selections(
                        node.selection_set,
                        related_model,
                        full_path,
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
                            if rel_name in meta.fields_map:
                                rel_path = (
                                    f"{prefix}__{rel_name}" if prefix else rel_name
                                )
                                prefetch_paths.append(rel_path)

        for field_node in field_nodes_from_info(info):
            _walk_selections(field_node.selection_set, model)

        only_fields: list[str] = []
        type_name_root = self._type_name_for_model(model)
        if type_name_root and store:
            for field_node in field_nodes_from_info(info):
                if field_node.selection_set:
                    for sel in iter_field_nodes(field_node.selection_set, fragments):
                        fname = _to_snake(sel.name.value)
                        hints = store.get(type_name_root, fname)
                        if hints and hints.only:
                            only_fields.extend(hints.only)

        if prefetch_paths:
            query = query.prefetch_related(
                *_coalesce_tortoise_prefetch_paths(model, prefetch_paths)
            )
        if only_fields:
            query = query.only(*only_fields)
        if orderings:
            _remember_query_ordering(query, orderings)

        results = list(await query)

        if custom_rels:
            await self._apply_custom_prefetch(results, custom_rels)

        return results

    async def _apply_custom_prefetch(
        self,
        parents: list[Any],
        custom_rels: list[_CustomRel],
    ) -> None:
        """Execute batch queries for relationships that need custom querysets
        (load callable or nested get_queryset) and assign results to parents."""
        if not parents:
            return

        for crel in custom_rels:
            parent_ids = [p.id for p in parents if hasattr(p, "id")]
            if not parent_ids:
                continue

            if crel.fk_col is not None:
                qs = crel.related_model.filter(
                    **{f"{crel.fk_col}__in": parent_ids},
                )
            else:
                qs = crel.related_model.all()

            qs = crel.qs_fn(qs)

            if crel.sub_prefetches:
                qs = qs.prefetch_related(*crel.sub_prefetches)

            items = list(await qs)
            if _query_orderings(qs) is None:
                items = sorted(items, key=lambda item: getattr(item, "id", 0))

            if crel.fk_col is not None:
                groups: dict[int, list[Any]] = defaultdict(list)
                for item in items:
                    pid = getattr(item, crel.fk_col, None)
                    if pid is not None:
                        groups[pid].append(item)
                for parent in parents:
                    setattr(
                        parent,
                        f"_{crel.field_name}",
                        groups.get(parent.id, []),
                    )
            else:
                for parent in parents:
                    rel_value = getattr(parent, crel.field_name)
                    parent_qs = (
                        rel_value.all()
                        if hasattr(rel_value, "all")
                        else crel.related_model.filter(
                            pk__in=[item.id for item in list(rel_value)]
                        )
                    )
                    parent_qs = crel.qs_fn(parent_qs)
                    if crel.sub_prefetches:
                        parent_qs = parent_qs.prefetch_related(
                            *crel.sub_prefetches
                        )  # pragma: no cover
                    setattr(
                        parent,
                        f"_{crel.field_name}",
                        _apply_python_ordering(
                            sorted(
                                list(await parent_qs),
                                key=lambda item: getattr(item, "id", 0),
                            )
                            if _query_orderings(parent_qs) is None
                            else list(await parent_qs),
                            _query_orderings(parent_qs),
                        ),
                    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_reverse_fk_field(
    related_model: type,
    parent_model: type,
    field_name: str,
) -> str:
    """Find the FK column on related_model that points back to parent_model."""
    meta = related_model._meta  # type: ignore[attr-defined]
    for name, field_obj in meta.fields_map.items():
        if (
            type(field_obj).__name__ == "ForeignKeyFieldInstance"
            and field_obj.related_model is parent_model
        ):
            return getattr(field_obj, "source_field", f"{name}_id")
    return f"{field_name}_id"


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
                qs = backend.apply_filters(qs, filter, rel_model)
            if order is not None:
                qs = backend.apply_ordering(qs, order, rel_model)
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
                qs = backend.apply_filters(qs, filter, rel_model)
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
                qs = backend.apply_ordering(qs, order, rel_model)
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


def _build_tortoise_filter(
    filter_input: Any,
    *,
    query: Any = None,
    info: Any = None,
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
                    return fk_clause, query
                return _build_tortoise_filter(
                    nested_filter,
                    query=query,
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
                sub_clauses, query = _build_tortoise_ordering(
                    nested,
                    _prefix=f"{_prefix}{rel_name}__",
                    query=query,
                    info=info,
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


def _extract_tortoise_group_fields(
    group_by_list: list[Any],
) -> tuple[list[str], list[str]]:
    """Extract field names for Tortoise GROUP BY from group-by input."""
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


def _extract_tortoise_overlapping_order(
    order_input: Any, group_field_names: set[str]
) -> list[str]:
    """Extract Tortoise order_by strings for group fields overlapping with root order."""
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


def _build_tortoise_order_from_input(order_input: Any) -> list[str]:
    """Convert an order input to Tortoise order_by strings."""
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
            dir_value = (
                direction.value if hasattr(direction, "value") else str(direction)
            )
            if dir_value.startswith("DESC"):
                clauses.append(f"-{col_name}")
            else:
                clauses.append(col_name)
    return clauses
