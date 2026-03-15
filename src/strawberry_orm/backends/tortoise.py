"""Tortoise ORM backend -- built from scratch (no existing strawberry integration)."""

from __future__ import annotations

import asyncio
import datetime
import re
from collections import defaultdict
from decimal import Decimal
from typing import Any, Optional

import strawberry
from strawberry.extensions import SchemaExtension

from strawberry_orm.filters import TYPE_TO_LOOKUP
from strawberry_orm.mutations import make_ref_type
from strawberry_orm.optimizer import OptimizerStore
from strawberry_orm.types import Ordering

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


class TortoiseBackend:
    """Backend adapter for Tortoise ORM (async)."""

    def __init__(self, **kwargs: Any) -> None:
        self._store = OptimizerStore()
        self._type_cache: dict[tuple[int, str], type] = {}
        self._type_registry: dict[str, type] = {}
        self._type_querysets: dict[type, Any] = {}

    # -- Type generation -----------------------------------------------------

    def type(self, model: type, **kwargs: Any) -> Any:
        backend = self
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        name = kwargs.get("name")
        filters = kwargs.get("filters")
        order = kwargs.get("order")

        def decorator(cls: type) -> Any:
            from strawberry_orm.types import FieldDefinition
            from strawberry_orm.optimizer.store import FieldHints

            fields_meta = _introspect_model(model)
            col_types: dict[str, type] = {}
            rel_fields: dict[str, dict[str, Any]] = {}

            for fname, ftype, is_relation, rel_model in fields_meta:
                if is_relation:
                    rel_fields[fname] = {"model": rel_model}
                else:
                    col_types[fname] = ftype

            annotations = getattr(cls, "__annotations__", {}).copy()

            for field_name in list(annotations):
                if include and field_name not in include:
                    continue
                if exclude and field_name in exclude:
                    del annotations[field_name]
                    continue

                ann = annotations[field_name]
                if ann is strawberry.auto:
                    if field_name in col_types:
                        annotations[field_name] = col_types[field_name]

            cls.__annotations__ = annotations
            cls.__orm_model__ = model  # type: ignore[attr-defined]

            if filters is not None:
                cls.__orm_filter__ = filters  # type: ignore[attr-defined]
            if order is not None:
                cls.__orm_order__ = order  # type: ignore[attr-defined]

            type_name = name or cls.__name__

            if hasattr(cls, "get_queryset") and isinstance(
                vars(cls).get("get_queryset"), classmethod
            ):
                backend._type_querysets[model] = cls.get_queryset

            for attr_name in list(vars(cls)):
                val = getattr(cls, attr_name, None)
                if isinstance(val, FieldDefinition):
                    hints = FieldHints(
                        load=val.load,
                        only=val.only,
                        compute=val.compute,
                        disable_optimization=val.disable_optimization,
                    )
                    backend._store.register(type_name, attr_name, hints)
                    try:
                        delattr(cls, attr_name)
                    except AttributeError:
                        pass
                elif getattr(val, "_orm_auto_field", False):
                    try:
                        delattr(cls, attr_name)
                    except AttributeError:
                        pass

            for field_name in list(annotations):
                if field_name not in rel_fields:
                    continue
                if field_name in vars(cls):
                    continue
                ann = annotations[field_name]
                el_type = _extract_element_type(ann)
                if el_type is None:
                    continue

                def _make_resolver(fname: str, return_ann: Any) -> Any:
                    def resolver(self: Any) -> Any:
                        return list(getattr(self, fname))

                    resolver.__name__ = fname
                    resolver.__annotations__ = {"return": return_ann}
                    return strawberry.field(resolver=resolver)

                setattr(cls, field_name, _make_resolver(field_name, ann))

            result = strawberry.type(cls, name=name if name else None)

            backend._type_registry[type_name] = model

            return result

        return decorator

    def input(self, model: type, **kwargs: Any) -> Any:
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        name = kwargs.get("name")

        fields_meta = _introspect_model(model)
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        for fname, ftype, is_relation, _rel_model in fields_meta:
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if is_relation:
                continue
            annotations[fname] = Optional[ftype]
            defaults[fname] = strawberry.UNSET

        type_name = name or f"{model.__name__}Input"
        ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
        cls = type(type_name, (), ns)
        return strawberry.input(cls)

    def partial(self, model: type, **kwargs: Any) -> Any:
        return self.input(model, name=f"{model.__name__}PartialInput", **kwargs)

    def filter(self, model_or_type: type, **kwargs: Any) -> Any:
        model = model_or_type
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")

        fields_meta = _introspect_model(model)

        field_annotations: dict[str, Any] = {}
        field_defaults: dict[str, Any] = {}

        for fname, ftype, is_relation, _rel_model in fields_meta:
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if is_relation:
                continue
            lookup_type = TYPE_TO_LOOKUP.get(ftype)
            if lookup_type is not None:
                field_annotations[fname] = Optional[lookup_type]
                field_defaults[fname] = strawberry.UNSET

        field_type_name = f"{model.__name__}Field"
        field_ns: dict[str, Any] = {
            "__annotations__": field_annotations,
            **field_defaults,
        }
        field_cls = type(field_type_name, (), field_ns)
        FieldType = strawberry.input(field_cls, one_of=True)

        filter_type_name = f"{model.__name__}Filter"

        filter_ns: dict[str, Any] = {
            "__annotations__": {"field": Optional[FieldType]},
            "field": strawberry.UNSET,
        }
        FilterCls = type(filter_type_name, (), filter_ns)

        FilterCls.__annotations__["all"] = Optional[list[FilterCls]]
        FilterCls.__annotations__["any"] = Optional[list[FilterCls]]
        FilterCls.__annotations__["not_"] = Optional[FilterCls]
        FilterCls.__annotations__["one_of"] = Optional[list[FilterCls]]
        FilterCls.all = strawberry.UNSET
        FilterCls.any = strawberry.UNSET
        FilterCls.not_ = strawberry.field(default=strawberry.UNSET, name="not")
        FilterCls.one_of = strawberry.UNSET

        FilterType = strawberry.input(FilterCls, one_of=True)
        FilterType._field_type = FieldType  # type: ignore[attr-defined]
        return FilterType

    def order(self, model_or_type: type, **kwargs: Any) -> Any:
        model = model_or_type
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")

        fields_meta = _introspect_model(model)
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}

        for fname, ftype, is_relation, _rel_model in fields_meta:
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if is_relation:
                continue
            annotations[fname] = Optional[Ordering]
            defaults[fname] = strawberry.UNSET

        type_name = f"{model.__name__}Order"
        ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
        cls = type(type_name, (), ns)
        return strawberry.input(cls)

    def aggregate(self, model: type, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "aggregate() is not yet supported for the Tortoise backend"
        )

    # -- Fields --------------------------------------------------------------

    def field(self, **kwargs: Any) -> Any:
        from strawberry_orm.types import FieldDefinition

        hint_keys = {"load", "only", "compute", "disable_optimization"}
        if hint_keys & set(kwargs):
            return FieldDefinition(
                load=kwargs.get("load"),
                only=kwargs.get("only"),
                compute=kwargs.get("compute"),
                disable_optimization=kwargs.get("disable_optimization", False),
                description=kwargs.get("description"),
            )
        return strawberry.field(
            **{
                k: v
                for k, v in kwargs.items()
                if k
                in ("description", "deprecation_reason", "default", "resolver", "name")
            }
        )

    def node(self, **kwargs: Any) -> Any:
        return strawberry.field(**kwargs)

    def connection(self, **kwargs: Any) -> Any:
        return strawberry.field(
            **{
                k: v
                for k, v in kwargs.items()
                if k
                in ("description", "deprecation_reason", "default", "resolver", "name")
            }
        )

    # -- Mutations -----------------------------------------------------------

    def create(self, input_type: type, **kwargs: Any) -> Any:
        return strawberry.field(description=kwargs.get("description"))

    def update(self, input_type: type, **kwargs: Any) -> Any:
        return strawberry.field(description=kwargs.get("description"))

    def delete(self, **kwargs: Any) -> Any:
        return strawberry.field(description=kwargs.get("description"))

    # -- Related list refs ---------------------------------------------------

    def ref(
        self,
        model: type,
        *,
        create: type | None = None,
        update: type | None = None,
        delete: bool = False,
    ) -> type:
        return make_ref_type(model, create=create, update=update, delete=delete)

    async def apply_ref_list(
        self, instance: Any, field: str, refs: list[Any], info: Any
    ) -> None:
        manager = getattr(instance, field)
        rel_model = manager.remote_model

        new_related: list[Any] = []
        to_delete_ids: list[Any] = []

        for ref in refs:
            if hasattr(ref, "id") and ref.id is not None:
                obj = await rel_model.get(pk=ref.id)
                new_related.append(obj)
            elif hasattr(ref, "create") and ref.create is not None:
                obj = await rel_model.create(**_input_to_dict(ref.create))
                new_related.append(obj)
            elif hasattr(ref, "update") and ref.update is not None:
                data = _input_to_dict(ref.update)
                pk = data.pop("id")
                await rel_model.filter(pk=pk).update(**data)
                obj = await rel_model.get(pk=pk)
                new_related.append(obj)
            elif hasattr(ref, "delete") and ref.delete is not None:
                to_delete_ids.append(ref.delete.id)

        await manager.clear()
        if new_related:
            await manager.add(*new_related)
        if to_delete_ids:
            await rel_model.filter(pk__in=to_delete_ids).delete()

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type) -> Any:
        return model.all()

    def is_query_object(self, value: Any) -> bool:
        try:
            from tortoise.queryset import QuerySet

            return isinstance(value, QuerySet)
        except ImportError:
            return False

    # -- Optimizer -----------------------------------------------------------

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return _TortoiseOptimizerExtension.configure(
            backend=self,
            store=self._store,
        )

    async def apply_optimizer_hints(
        self,
        store: Any,
        query: Any,
        info: Any,
    ) -> Any:

        try:
            model = query.model
        except AttributeError:
            return list(await query)

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
                return
            meta = current_model._meta  # type: ignore[attr-defined]
            for node in selection_set.selections:
                field_name = _to_snake(node.name.value)
                full_path = f"{prefix}__{field_name}" if prefix else field_name

                if field_name not in meta.fields_map:
                    continue
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
                        fk_col = getattr(field_obj, "relation_field", None)

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
                    if hints and not hints.disable_optimization:
                        if hints.load and not callable(hints.load):
                            for rel_name in hints.load:
                                if rel_name in meta.fields_map:
                                    rel_path = (
                                        f"{prefix}__{rel_name}" if prefix else rel_name
                                    )
                                    prefetch_paths.append(rel_path)

        for field_node in info.field_nodes:
            _walk_selections(field_node.selection_set, model)

        if prefetch_paths:
            query = query.prefetch_related(*prefetch_paths)

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
                    setattr(parent, f"_{crel.field_name}", items)

    def _type_name_for_model(self, model: type) -> str | None:
        for type_name, m in self._type_registry.items():
            if m is model:
                return type_name
        return None


# ---------------------------------------------------------------------------
# Async optimizer extension for Tortoise
# ---------------------------------------------------------------------------


class _TortoiseOptimizerExtension(SchemaExtension):
    """Async-aware optimizer extension for the Tortoise backend."""

    _backend: TortoiseBackend | None = None
    _store: OptimizerStore | None = None

    @classmethod
    def configure(
        cls,
        backend: TortoiseBackend,
        store: OptimizerStore,
    ) -> type[_TortoiseOptimizerExtension]:
        return type(
            f"{cls.__name__}_TortoiseBackend",
            (cls,),
            {"_backend": backend, "_store": store},
        )

    def on_execute(self) -> Any:
        yield

    async def resolve(
        self,
        _next: Any,
        root: Any,
        info: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = _next(root, info, *args, **kwargs)
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            result = await result

        backend = self._backend
        if backend is None:
            return result

        if backend.is_query_object(result) and self._store is not None:
            result = await backend.apply_optimizer_hints(
                self._store,
                result,
                info,
            )

        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_element_type(ann: Any) -> Any:
    """Extract T from list[T]."""
    import typing

    if typing.get_origin(ann) is list:
        args = typing.get_args(ann)
        if args:
            return args[0]
    return None


def _get_reverse_fk_field(
    related_model: type,
    parent_model: type,
    field_name: str,
) -> str:
    """Find the FK column on related_model that points back to parent_model."""
    meta = related_model._meta  # type: ignore[attr-defined]
    for name, field_obj in meta.fields_map.items():
        if type(field_obj).__name__ == "ForeignKeyFieldInstance":
            if field_obj.related_model is parent_model:
                return getattr(field_obj, "source_field", f"{name}_id")
    return f"{field_name}_id"


def _introspect_model(
    model: type,
) -> list[tuple[str, type, bool, type | None]]:
    """Return a list of (field_name, python_type, is_relation, related_model)
    for every field on a Tortoise model."""
    meta = model._meta  # type: ignore[attr-defined]
    result: list[tuple[str, type, bool, type | None]] = []

    for name, field_obj in meta.fields_map.items():
        field_class_name = type(field_obj).__name__

        if field_class_name in (
            "ForeignKeyFieldInstance",
            "BackwardFKRelation",
            "ManyToManyFieldInstance",
            "BackwardOneToOneRelation",
        ):
            related_model = (
                field_obj.related_model if hasattr(field_obj, "related_model") else None
            )
            result.append((name, Any, True, related_model))
            continue

        py_type = _TORTOISE_FIELD_MAP.get(field_class_name, str)
        result.append((name, py_type, False, None))

    return result


def _input_to_dict(obj: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for f in obj.__class__.__dataclass_fields__:
        val = getattr(obj, f)
        if val is not strawberry.UNSET:
            result[f] = val
    return result
