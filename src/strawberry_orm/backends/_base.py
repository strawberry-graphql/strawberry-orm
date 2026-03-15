"""Base backend with shared logic for all ORM adapters."""

from __future__ import annotations

import re
import typing
import warnings
from typing import Any, Optional

import strawberry

from strawberry_orm.filters import TYPE_TO_LOOKUP, StringLookup, StringLookupNoRegex
from strawberry_orm.mutations import make_ref_type
from strawberry_orm.optimizer import OptimizerStore
from strawberry_orm.types import Ordering

FieldMeta = tuple[str, type, bool, type | None]

_SENSITIVE_PATTERNS = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|hash|ssn|"
    r"credit_card|creditcard|private_key|privatekey|admin|staff|"
    r"superuser|permission|role)",
    re.IGNORECASE,
)


def extract_element_type(ann: Any) -> Any:
    """Extract T from ``list[T]``, or return ``None``."""
    if typing.get_origin(ann) is list:
        args = typing.get_args(ann)
        if args:
            return args[0]
    return None


def input_to_dict(obj: Any) -> dict[str, Any]:
    """Convert a Strawberry input dataclass to a plain dict, skipping UNSET."""
    result: dict[str, Any] = {}
    for f in obj.__class__.__dataclass_fields__:
        val = getattr(obj, f)
        if val is not strawberry.UNSET:
            result[f] = val
    return result


class BaseBackend:
    """Shared foundation for Django, SQLAlchemy, and Tortoise backends.

    Subclasses must override at least:
    - ``_introspect_model``
    - ``apply_ref_list``
    - ``apply_filters`` / ``apply_ordering``
    - ``get_default_queryset`` / ``is_query_object``
    - ``optimizer_extension`` / ``apply_optimizer_hints``
    """

    def __init__(self, **kwargs: Any) -> None:
        self._store = OptimizerStore()
        self._filter_overrides: dict[type, type] = kwargs.get("filter_overrides") or {}
        self._type_registry: dict[str, type] = {}
        self._type_querysets: dict[type, Any] = {}
        self._warn_sensitive: bool = kwargs.get("warn_sensitive", True)
        self._exclude_sensitive_fields: bool = kwargs.get(
            "exclude_sensitive_fields", True
        )
        self._hard_delete_refs: bool = kwargs.get("hard_delete_refs", False)
        self._default_query_limit: int | None = kwargs.get("default_query_limit")

    # -- Abstract / hook methods (override in subclasses) --------------------

    def _introspect_model(
        self,
        model: type,
    ) -> list[FieldMeta]:
        """Return ``(field_name, python_type, is_relation, related_model)``
        for every field on *model*.  Must be overridden by subclasses."""
        raise NotImplementedError

    # -- Shared type generation ----------------------------------------------

    def _exclude_generated_sensitive_field(
        self,
        field_name: str,
        include: list[str] | tuple[str, ...] | set[str] | None,
    ) -> bool:
        return (
            self._exclude_sensitive_fields
            and not (include and field_name in include)
            and _SENSITIVE_PATTERNS.search(field_name) is not None
        )

    def input(self, model: type, **kwargs: Any) -> Any:
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        exclude_pk = kwargs.get("exclude_pk", True)
        name = kwargs.get("name")

        pk_names = self._get_pk_names(model) if exclude_pk else set()

        fields_meta = self._introspect_model(model)
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        for fname, ftype, is_relation, _rel_model in fields_meta:
            if fname in pk_names:
                continue
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if self._exclude_generated_sensitive_field(fname, include):
                continue
            if is_relation:
                continue
            annotations[fname] = Optional[ftype]
            defaults[fname] = strawberry.UNSET

        type_name = name or f"{model.__name__}Input"
        ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
        cls = type(type_name, (), ns)
        return strawberry.input(cls)

    def _get_pk_names(self, model: type) -> set[str]:
        """Return PK field names. Subclasses may override for ORM-specific logic."""
        fields_meta = self._introspect_model(model)
        pk_candidates = set()
        for fname, _ftype, _is_rel, _rel in fields_meta:
            if fname == "id" or fname.endswith("_id"):
                pk_candidates.add(fname)
        return {"id"} if "id" in pk_candidates else set()

    def partial(self, model: type, **kwargs: Any) -> Any:
        kwargs.setdefault("name", f"{model.__name__}PartialInput")
        return self.input(model, **kwargs)

    def filter(self, model_or_type: type, **kwargs: Any) -> Any:
        model = model_or_type
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")

        enable_regex = getattr(self, "_enable_regex_filters", False)

        fields_meta = self._introspect_model(model)

        field_annotations: dict[str, Any] = {}
        field_defaults: dict[str, Any] = {}

        for fname, ftype, is_relation, _rel_model in fields_meta:
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if self._exclude_generated_sensitive_field(fname, include):
                continue
            if is_relation:
                continue
            lookup_type = self._filter_overrides.get(ftype) or TYPE_TO_LOOKUP.get(ftype)
            if lookup_type is not None:
                if lookup_type is StringLookup and not enable_regex:
                    lookup_type = StringLookupNoRegex
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
        FilterType.__orm_model__ = model  # type: ignore[attr-defined]
        return FilterType

    def order(self, model_or_type: type, **kwargs: Any) -> Any:
        model = model_or_type
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")

        fields_meta = self._introspect_model(model)
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}

        for fname, _ftype, is_relation, _rel_model in fields_meta:
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if self._exclude_generated_sensitive_field(fname, include):
                continue
            if is_relation:
                continue
            annotations[fname] = Optional[Ordering]
            defaults[fname] = strawberry.UNSET

        type_name = f"{model.__name__}Order"
        ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
        cls = type(type_name, (), ns)
        result = strawberry.input(cls, one_of=True)
        result.__orm_model__ = model  # type: ignore[attr-defined]
        return result

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
        allowed = {"description", "deprecation_reason", "default", "resolver", "name"}
        return strawberry.field(**{k: v for k, v in kwargs.items() if k in allowed})

    def node(self, **kwargs: Any) -> Any:
        return strawberry.field(**kwargs)

    def connection(self, **kwargs: Any) -> Any:
        allowed = {"description", "deprecation_reason", "default", "resolver", "name"}
        return strawberry.field(**{k: v for k, v in kwargs.items() if k in allowed})

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

    # -- Shared helpers ------------------------------------------------------

    def _type_name_for_model(self, model: type) -> str | None:
        for type_name, m in self._type_registry.items():
            if m is model:
                return type_name
        return None

    def _process_type_annotations(
        self,
        cls: type,
        model: type,
        col_types: dict[str, type],
        *,
        include: Any = None,
        exclude: Any = None,
        name: str | None = None,
        filters: Any = None,
        order: Any = None,
    ) -> str:
        """Shared annotation processing for ``type()`` decorators.

        Replaces ``strawberry.auto`` annotations, sets ``__orm_model__`` and
        friends, processes ``FieldDefinition`` / ``_orm_auto_field`` attrs,
        registers ``get_queryset``, and returns the resolved type name.
        """
        from strawberry_orm.types import FieldDefinition

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

        if self._warn_sensitive:
            excluded = set(exclude or [])
            for field_name in annotations:
                if field_name in excluded:
                    continue
                if _SENSITIVE_PATTERNS.search(field_name):
                    warnings.warn(
                        f"Field '{field_name}' on {model.__name__} looks sensitive "
                        f"and is exposed in the GraphQL type. Consider adding it to "
                        f"exclude=['{field_name}'] in orm.type().",
                        stacklevel=4,
                    )

        if filters is not None:
            cls.__orm_filter__ = filters  # type: ignore[attr-defined]
        if order is not None:
            cls.__orm_order__ = order  # type: ignore[attr-defined]

        type_name = name or cls.__name__

        if hasattr(cls, "get_queryset") and isinstance(
            vars(cls).get("get_queryset"), classmethod
        ):
            self._type_querysets[model] = cls.get_queryset

        for attr_name in list(vars(cls)):
            val = getattr(cls, attr_name, None)
            if isinstance(val, FieldDefinition):
                self._store.register(type_name, attr_name, val.to_hints())
                if getattr(val, "permission_classes", None):
                    setattr(
                        cls,
                        attr_name,
                        strawberry.field(
                            permission_classes=val.permission_classes,
                            description=val.description,
                        ),
                    )
                else:
                    try:
                        delattr(cls, attr_name)
                    except AttributeError:
                        pass
            elif getattr(val, "_orm_auto_field", False):
                try:
                    delattr(cls, attr_name)
                except AttributeError:
                    pass

        return type_name

    def _finalize_type(
        self,
        cls: type,
        model: type,
        type_name: str,
        name: str | None,
    ) -> Any:
        """Call ``strawberry.type()`` and register the model in the type registry."""
        result = strawberry.type(cls, name=name if name else None)
        self._type_registry[type_name] = model
        return result
