"""Tortoise ORM backend -- built from scratch (no existing strawberry integration)."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Optional, get_type_hints

import strawberry
from strawberry.extensions import SchemaExtension

from strawberry_orm.filters import TYPE_TO_LOOKUP
from strawberry_orm.mutations import make_ref_type
from strawberry_orm.optimizer import OptimizerExtension, OptimizerStore
from strawberry_orm.types import Ordering

# Tortoise field type -> Python type mapping
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

        def decorator(cls: type) -> Any:
            from strawberry_orm.types import FieldDefinition
            from strawberry_orm.optimizer.store import FieldHints

            fields_meta = _introspect_model(model)
            col_types: dict[str, type] = {}
            for fname, ftype, is_relation, _rel_model in fields_meta:
                if not is_relation:
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
        return strawberry.field(**{
            k: v for k, v in kwargs.items()
            if k in ("description", "deprecation_reason", "default", "resolver", "name")
        })

    def node(self, **kwargs: Any) -> Any:
        return strawberry.field(**kwargs)

    def connection(self, **kwargs: Any) -> Any:
        return strawberry.field(**{
            k: v for k, v in kwargs.items()
            if k in ("description", "deprecation_reason", "default", "resolver", "name")
        })

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
        return OptimizerExtension.configure(backend=self, store=self._store)

    def apply_optimizer_hints(
        self, store: Any, query: Any, info: Any
    ) -> Any:
        return query


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _introspect_model(
    model: type,
) -> list[tuple[str, type, bool, type | None]]:
    """Return a list of (field_name, python_type, is_relation, related_model)
    for every field on a Tortoise model."""
    meta = model._meta  # type: ignore[attr-defined]
    result: list[tuple[str, type, bool, type | None]] = []

    for name, field_obj in meta.fields_map.items():
        field_class_name = type(field_obj).__name__

        if field_class_name in ("ForeignKeyFieldInstance", "BackwardFKRelation",
                                "ManyToManyFieldInstance", "BackwardOneToOneRelation"):
            related_model = field_obj.related_model if hasattr(field_obj, "related_model") else None
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
