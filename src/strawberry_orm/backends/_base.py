"""Base backend with shared logic for all ORM adapters."""

from __future__ import annotations

import datetime
import hashlib
import inspect
import re
import typing
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Literal

import strawberry

from strawberry_orm.filters import (
    _CUSTOM_AGGREGATE_ATTR,
    _CUSTOM_FILTER_ATTR,
    _CUSTOM_GROUP_ATTR,
    _CUSTOM_ORDER_ATTR,
    TYPE_TO_LOOKUP,
    StringLookup,
    StringLookupNoRegex,
)
from strawberry_orm.mutations import make_ref_type
from strawberry_orm.optimizer import OptimizerStore
from strawberry_orm.types import DateGroupByOption, Ordering

LazyResolutionMode = Literal["off", "warn", "error"]
_LAZY_RESOLUTION_MODES: frozenset[str] = frozenset({"off", "warn", "error"})

FieldMeta = tuple[str, type, bool, type | None]

_SENSITIVE_PATTERNS = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|hash|ssn|"
    r"credit_card|creditcard|private_key|privatekey|admin|staff|"
    r"superuser|permission|role)",
    re.IGNORECASE,
)


_KNOWN_FILTER_KEYS = frozenset({"field", "object", "all", "any", "not_", "one_of"})
_KNOWN_ORDER_KEYS = frozenset({"field", "object"})


@dataclass
class AggregateMeta:
    """Holds the auto-generated aggregate output types and field metadata."""

    model: type
    aggregates_type: type
    group_key_type: type
    sum_type: type | None = None
    avg_type: type | None = None
    min_type: type | None = None
    max_type: type | None = None
    numeric_fields: list[tuple[str, type]] = dc_field(default_factory=list)
    comparable_fields: list[tuple[str, type]] = dc_field(default_factory=list)
    groupable_fields: list[tuple[str, type]] = dc_field(default_factory=list)

    group_key_fields: list[str] = dc_field(default_factory=list)

    custom_fields: list[tuple[str, Callable[..., Any], type]] = dc_field(
        default_factory=list,
    )
    """Each entry is ``(field_name, handler_callable, python_return_type)``."""

    def build_aggregates(self, row: Any, requested: dict[str, Any]) -> Any:
        """Construct an ``Aggregates`` instance from a SQL result *row*."""
        kwargs: dict[str, Any] = {}
        kwargs["count"] = getattr(row, "_count", 0) if requested.get("count") else 0

        for func_name, SubType in [
            ("sum", self.sum_type),
            ("avg", self.avg_type),
            ("min", self.min_type),
            ("max", self.max_type),
        ]:
            col_names = requested.get(func_name, [])
            if SubType is not None and col_names:
                sub_kwargs = {}
                for col in col_names:
                    sub_kwargs[col] = getattr(row, f"_{func_name}_{col}", None)
                kwargs[func_name] = SubType(**sub_kwargs)

        for field_name, _handler, _rtype in self.custom_fields:
            val = getattr(row, f"_custom_{field_name}", None)
            if val is not None:
                kwargs[field_name] = val

        return self.aggregates_type(**kwargs)

    def build_group_key(self, row: Any, key_fields: list[str]) -> Any:
        """Construct a ``GroupKey`` instance from a SQL result *row*."""
        kwargs: dict[str, Any] = {}
        for fname in key_fields:
            val = getattr(row, fname, None)
            kwargs[fname] = str(val) if val is not None else None
        return self.group_key_type(**kwargs)


def _find_selection(info: Any, field_path: str) -> Any:
    """Walk ``info.selected_fields`` to find a nested selection by dot-path.

    Strawberry wraps the current field in ``info.selected_fields``, so
    the first entry is the field being resolved.  We unwrap that
    automatically when the first path component does not match the
    top-level field name.
    """
    parts = field_path.split(".")
    selections = info.selected_fields

    if (
        selections
        and len(parts) > 0
        and parts[0] != getattr(selections[0], "name", None)
    ):
        inner: list[Any] = []
        for sel in selections:
            inner.extend(getattr(sel, "selections", []))
        if inner:
            selections = inner

    for part in parts:
        found = None
        for sel in selections:
            if sel.name == part:
                found = sel
                break
        if found is None:
            return None
        selections = found.selections if hasattr(found, "selections") else []
    return found


def _selection_requests(info: Any, *path: str) -> bool:
    """Return ``True`` if the dot-separated *path* is in the selection set."""
    return _find_selection(info, ".".join(path)) is not None


def requested_aggregates(
    info: Any, field_path: str = "aggregates"
) -> dict[str, Any] | None:
    """Parse the selection set to determine which aggregates are needed.

    Returns a dict like::

        {'count': True, 'sum': ['amount'], 'avg': [], 'min': [], 'max': []}

    or ``None`` if the field is not in the selection set at all.
    """
    agg_selection = _find_selection(info, field_path)
    if agg_selection is None:
        return None

    result: dict[str, Any] = {}
    for fld in agg_selection.selections:
        name = fld.name
        if name == "count":
            result["count"] = True
        elif name in ("sum", "avg", "min", "max"):
            sub_fields = (
                [sf.name for sf in fld.selections] if hasattr(fld, "selections") else []
            )
            result[name] = sub_fields
    return result


def invoke_custom_callback(
    callback: Callable[..., Any],
    instance: Any,
    *,
    query: Any,
    value: Any,
    info: Any = None,
) -> Any:
    """Call a user-defined ``@filter_field`` / ``@order_field`` callback.

    The callback is inspected once to determine whether it accepts ``info``.
    """
    sig = inspect.signature(callback)
    kwargs: dict[str, Any] = {"value": value, "query": query}
    if "info" in sig.parameters:
        kwargs["info"] = info
    return callback(instance, **kwargs)


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
        self._repos: dict[type, type] = {}
        self._filter_overrides: dict[type, type] = kwargs.get("filter_overrides") or {}
        self._type_registry: dict[str, type] = {}
        self._graphql_type_registry: dict[type, type] = {}
        self._filter_registry: dict[type, type] = {}
        self._projected_filter_cache: dict[tuple[type, Any], type] = {}
        self._order_registry: dict[type, type] = {}
        self._group_registry: dict[type, type] = {}
        self._aggregate_type_cache: dict[type, AggregateMeta] = {}
        self._type_querysets: dict[type, Any] = {}
        self._warn_sensitive: bool = kwargs.get("warn_sensitive", True)
        self._exclude_sensitive_fields: bool = kwargs.get(
            "exclude_sensitive_fields", True
        )
        lazy_resolution = kwargs.get("lazy_resolution", "warn")
        if lazy_resolution not in _LAZY_RESOLUTION_MODES:
            raise ValueError(
                "lazy_resolution must be one of 'off', 'warn', or 'error'; "
                f"got {lazy_resolution!r}"
            )
        self._lazy_resolution: LazyResolutionMode = lazy_resolution
        self._default_query_limit: int | None = kwargs.get("default_query_limit")

    def get_repo(self, model: type) -> Any | None:
        """Return an instantiated repo for *model*, or ``None``."""
        repo_cls = self._repos.get(model)
        if repo_cls is None:
            return None
        repo = repo_cls(self)
        if repo.model is None:
            repo.model = model
        return repo

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
            annotations[fname] = ftype | None
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
        project: dict[str, Any] | None = kwargs.get("project")

        enable_regex = getattr(self, "_enable_regex_filters", False)

        fields_meta = self._introspect_model(model)

        if project is not None:
            relation_names = {fname for fname, _, is_rel, _ in fields_meta if is_rel}
            unknown = set(project.keys()) - relation_names
            if unknown:
                raise ValueError(
                    f"Unknown relation(s) in project for {model.__name__}: "
                    f"{', '.join(sorted(unknown))}"
                )

        # Check the projected-filter cache for projected (non-default) calls.
        if project is not None:
            cache_key = (model, self._filter_project_signature(project))
            cached = self._projected_filter_cache.get(cache_key)
            if cached is not None:
                return cached

        suffix = self._filter_project_suffix(project)

        field_annotations: dict[str, Any] = {}
        field_defaults: dict[str, Any] = {}

        object_annotations: dict[str, Any] = {}
        object_defaults: dict[str, Any] = {}
        relation_models: dict[str, type] = {}

        for fname, ftype, is_relation, rel_model in fields_meta:
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if self._exclude_generated_sensitive_field(fname, include):
                continue
            if is_relation:
                if rel_model is None:
                    continue
                if project is not None and fname not in project:
                    continue
                sub_project = project[fname] if project is not None else None
                if sub_project is not None:
                    rel_filter = self._get_projected_filter(rel_model, sub_project)
                else:
                    rel_filter = self._filter_registry.get(rel_model)
                if rel_filter is not None:
                    object_annotations[fname] = rel_filter | None
                    object_defaults[fname] = strawberry.UNSET
                    relation_models[fname] = rel_model
                continue
            lookup_type = self._filter_overrides.get(ftype) or TYPE_TO_LOOKUP.get(ftype)
            if lookup_type is not None:
                if lookup_type is StringLookup and not enable_regex:
                    lookup_type = StringLookupNoRegex
                field_annotations[fname] = lookup_type | None
                field_defaults[fname] = strawberry.UNSET

        # Reuse the existing FieldType when creating a projected variant.
        base_filter = self._filter_registry.get(model)
        if (
            project is not None
            and base_filter is not None
            and hasattr(base_filter, "_field_type")
        ):
            FieldType = base_filter._field_type
        else:
            field_type_name = f"{model.__name__}Field{suffix}"
            field_ns: dict[str, Any] = {
                "__annotations__": field_annotations,
                **field_defaults,
            }
            field_cls = type(field_type_name, (), field_ns)
            FieldType = strawberry.input(field_cls, one_of=True)

        filter_type_name = f"{model.__name__}Filter{suffix}"
        filter_ns: dict[str, Any] = {
            "__annotations__": {"field": FieldType | None},
            "field": strawberry.UNSET,
        }
        FilterCls = type(filter_type_name, (), filter_ns)

        FilterCls.__annotations__["all"] = list[FilterCls] | None
        FilterCls.__annotations__["any"] = list[FilterCls] | None
        FilterCls.__annotations__["not_"] = FilterCls | None
        FilterCls.__annotations__["one_of"] = list[FilterCls] | None
        FilterCls.all = strawberry.UNSET
        FilterCls.any = strawberry.UNSET
        FilterCls.not_ = strawberry.field(default=strawberry.UNSET, name="not")
        FilterCls.one_of = strawberry.UNSET

        if object_annotations:
            obj_type_name = f"{model.__name__}FilterObject{suffix}"
            obj_ns: dict[str, Any] = {
                "__annotations__": object_annotations,
                **object_defaults,
            }
            obj_cls = type(obj_type_name, (), obj_ns)
            ObjectType = strawberry.input(obj_cls, one_of=True)
            FilterCls.__annotations__["object"] = ObjectType | None
            FilterCls.object = strawberry.UNSET

        FilterType = strawberry.input(FilterCls, one_of=True)
        FilterType._field_type = FieldType  # type: ignore[attr-defined]
        FilterType.__orm_model__ = model  # type: ignore[attr-defined]
        if object_annotations:
            FilterType._object_type = ObjectType  # type: ignore[attr-defined]
            FilterType._relation_models = relation_models  # type: ignore[attr-defined]

        if project is None:
            self._filter_registry[model] = FilterType
        else:
            self._projected_filter_cache[cache_key] = FilterType
        return FilterType

    # -- Filter project helpers ------------------------------------------------

    def _get_projected_filter(self, model: type, sub_project: dict[str, Any]) -> Any:
        """Return a filter type for *model* constrained by *sub_project*.

        If *sub_project* is empty the returned filter has no ``object`` type
        (leaf).  Otherwise only the relations listed in *sub_project* are
        exposed, each recursively constrained by their own sub-project.
        """
        sig = self._filter_project_signature(sub_project)
        cache_key = (model, sig)
        cached = self._projected_filter_cache.get(cache_key)
        if cached is not None:
            return cached
        if not sub_project:
            base = self._filter_registry.get(model)
            if base is not None and not hasattr(base, "_object_type"):
                self._projected_filter_cache[cache_key] = base
                return base
        result = self.filter(model, project=sub_project)
        return result

    @staticmethod
    def _filter_project_signature(project: dict[str, Any] | None) -> Any:
        """Turn a project dict into a hashable, comparable value."""
        if project is None:
            return None
        if not project:
            return ()
        return tuple(
            sorted(
                (k, BaseBackend._filter_project_signature(v))
                for k, v in project.items()
            )
        )

    @staticmethod
    def _filter_project_suffix(project: dict[str, Any] | None) -> str:
        """Short hash suffix for projected type names (empty for default)."""
        sig = BaseBackend._filter_project_signature(project)
        if sig is None:
            return ""
        if not sig:
            return "_leaf"
        digest = hashlib.sha1(repr(sig).encode("utf-8")).hexdigest()[:8]
        return f"_{digest}"

    def order(self, model_or_type: type, **kwargs: Any) -> Any:
        model = model_or_type
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")

        fields_meta = self._introspect_model(model)

        field_annotations: dict[str, Any] = {}
        field_defaults: dict[str, Any] = {}

        object_annotations: dict[str, Any] = {}
        object_defaults: dict[str, Any] = {}
        relation_models: dict[str, type] = {}

        for fname, _ftype, is_relation, rel_model in fields_meta:
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if self._exclude_generated_sensitive_field(fname, include):
                continue
            if is_relation:
                if rel_model is not None:
                    rel_order = self._order_registry.get(rel_model)
                    if rel_order is not None:
                        object_annotations[fname] = rel_order | None
                        object_defaults[fname] = strawberry.UNSET
                        relation_models[fname] = rel_model
                continue
            field_annotations[fname] = Ordering | None
            field_defaults[fname] = strawberry.UNSET

        field_type_name = f"{model.__name__}OrderField"
        field_ns: dict[str, Any] = {
            "__annotations__": field_annotations,
            **field_defaults,
        }
        field_cls = type(field_type_name, (), field_ns)
        OrderFieldType = strawberry.input(field_cls, one_of=True)

        order_type_name = f"{model.__name__}Order"
        order_ns: dict[str, Any] = {
            "__annotations__": {"field": OrderFieldType | None},
            "field": strawberry.UNSET,
        }
        OrderCls = type(order_type_name, (), order_ns)

        if object_annotations:
            obj_type_name = f"{model.__name__}OrderObject"
            obj_ns: dict[str, Any] = {
                "__annotations__": object_annotations,
                **object_defaults,
            }
            obj_cls = type(obj_type_name, (), obj_ns)
            OrderObjectType = strawberry.input(obj_cls, one_of=True)
            OrderCls.__annotations__["object"] = OrderObjectType | None
            OrderCls.object = strawberry.UNSET

        OrderType = strawberry.input(OrderCls, one_of=True)
        OrderType._field_type = OrderFieldType  # type: ignore[attr-defined]
        OrderType.__orm_model__ = model  # type: ignore[attr-defined]
        if object_annotations:
            OrderType._object_type = OrderObjectType  # type: ignore[attr-defined]
            OrderType._relation_models = relation_models  # type: ignore[attr-defined]
        self._order_registry[model] = OrderType
        return OrderType

    # -- Custom filter / order types -----------------------------------------

    def filter_type(
        self,
        model: type,
        *,
        include: list[str] | tuple[str, ...] | set[str] | None = None,
        exclude: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> Callable[[type], type]:
        """Decorator that builds a ``@oneOf`` filter input from a user class.

        ``auto`` annotations are expanded to the standard lookup type for
        the corresponding model column.  Methods decorated with
        ``@filter_field`` become additional top-level keys on the filter
        input alongside ``field``, ``object``, ``all``, ``any``, ``not``,
        and ``one_of``.
        """

        def decorator(cls: type) -> type:
            return self._build_custom_filter_type(
                cls, model, include=include, exclude=exclude
            )

        return decorator

    def _build_custom_filter_type(
        self,
        cls: type,
        model: type,
        *,
        include: Any = None,
        exclude: Any = None,
    ) -> type:
        enable_regex = getattr(self, "_enable_regex_filters", False)
        fields_meta = self._introspect_model(model)
        col_types = {
            fname: ftype for fname, ftype, is_rel, _ in fields_meta if not is_rel
        }
        rel_info = {
            fname: rel
            for fname, _, is_rel, rel in fields_meta
            if is_rel and rel is not None
        }

        user_annotations = typing.get_type_hints(cls, include_extras=True)

        field_annotations: dict[str, Any] = {}
        field_defaults: dict[str, Any] = {}
        object_annotations: dict[str, Any] = {}
        object_defaults: dict[str, Any] = {}
        relation_models: dict[str, type] = {}
        custom_filter_annotations: dict[str, Any] = {}
        custom_filter_defaults: dict[str, Any] = {}
        custom_filters: dict[str, Callable[..., Any]] = {}

        for fname, ann in user_annotations.items():
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if ann is strawberry.auto:
                if fname in col_types:
                    ftype = col_types[fname]
                    lookup_type = self._filter_overrides.get(
                        ftype
                    ) or TYPE_TO_LOOKUP.get(ftype)
                    if lookup_type is not None:
                        if lookup_type is StringLookup and not enable_regex:
                            lookup_type = StringLookupNoRegex
                        field_annotations[fname] = lookup_type | None
                        field_defaults[fname] = strawberry.UNSET
                elif fname in rel_info:
                    rel_model = rel_info[fname]
                    rel_filter = self._filter_registry.get(rel_model)
                    if rel_filter is not None:
                        object_annotations[fname] = rel_filter | None
                        object_defaults[fname] = strawberry.UNSET
                        relation_models[fname] = rel_model

        for attr_name in list(vars(cls)):
            method = getattr(cls, attr_name, None)
            if callable(method) and getattr(method, _CUSTOM_FILTER_ATTR, False):
                sig = inspect.signature(method)
                params = list(sig.parameters.values())
                value_param = None
                for p in params:
                    if p.name == "value":
                        value_param = p
                        break
                if (
                    value_param is None
                    or value_param.annotation is inspect.Parameter.empty
                ):
                    raise TypeError(
                        f"@filter_field method '{attr_name}' must have a "
                        f"'value' parameter with a type annotation."
                    )
                value_type = value_param.annotation
                custom_filter_annotations[attr_name] = value_type | None
                custom_filter_defaults[attr_name] = strawberry.UNSET
                custom_filters[attr_name] = method

        field_type_name = f"{model.__name__}Field"
        if field_annotations:
            field_ns: dict[str, Any] = {
                "__annotations__": field_annotations,
                **field_defaults,
            }
            field_cls = type(field_type_name, (), field_ns)
            FieldType = strawberry.input(field_cls, one_of=True)
        else:
            FieldType = None

        filter_type_name = f"{model.__name__}Filter"
        filter_ns: dict[str, Any] = {"__annotations__": {}}
        if FieldType is not None:
            filter_ns["__annotations__"]["field"] = FieldType | None
            filter_ns["field"] = strawberry.UNSET

        if object_annotations:
            obj_type_name = f"{model.__name__}FilterObject"
            obj_ns: dict[str, Any] = {
                "__annotations__": object_annotations,
                **object_defaults,
            }
            obj_cls = type(obj_type_name, (), obj_ns)
            ObjectType = strawberry.input(obj_cls, one_of=True)
            filter_ns["__annotations__"]["object"] = ObjectType | None
            filter_ns["object"] = strawberry.UNSET

        for cname, cann in custom_filter_annotations.items():
            filter_ns["__annotations__"][cname] = cann
            filter_ns[cname] = custom_filter_defaults[cname]

        FilterCls = type(filter_type_name, (), filter_ns)

        FilterCls.__annotations__["all"] = list[FilterCls] | None
        FilterCls.__annotations__["any"] = list[FilterCls] | None
        FilterCls.__annotations__["not_"] = FilterCls | None
        FilterCls.__annotations__["one_of"] = list[FilterCls] | None
        FilterCls.all = strawberry.UNSET
        FilterCls.any = strawberry.UNSET
        FilterCls.not_ = strawberry.field(default=strawberry.UNSET, name="not")
        FilterCls.one_of = strawberry.UNSET

        FilterType = strawberry.input(FilterCls, one_of=True)
        FilterType.__orm_model__ = model  # type: ignore[attr-defined]
        if FieldType is not None:
            FilterType._field_type = FieldType  # type: ignore[attr-defined]
        if object_annotations:
            FilterType._object_type = ObjectType  # type: ignore[attr-defined]
            FilterType._relation_models = relation_models  # type: ignore[attr-defined]
        if custom_filters:
            FilterType._custom_filters = custom_filters  # type: ignore[attr-defined]

        self._filter_registry[model] = FilterType
        return FilterType

    def order_type(
        self,
        model: type,
        *,
        include: list[str] | tuple[str, ...] | set[str] | None = None,
        exclude: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> Callable[[type], type]:
        """Decorator that builds a ``@oneOf`` order input from a user class.

        ``auto`` annotations are expanded to ``Ordering | None``.
        Methods decorated with ``@order_field`` become additional top-level
        keys on the order input alongside ``field`` and ``object``.
        """

        def decorator(cls: type) -> type:
            return self._build_custom_order_type(
                cls, model, include=include, exclude=exclude
            )

        return decorator

    def _build_custom_order_type(
        self,
        cls: type,
        model: type,
        *,
        include: Any = None,
        exclude: Any = None,
    ) -> type:
        fields_meta = self._introspect_model(model)
        col_types = {
            fname: ftype for fname, ftype, is_rel, _ in fields_meta if not is_rel
        }
        rel_info = {
            fname: rel
            for fname, _, is_rel, rel in fields_meta
            if is_rel and rel is not None
        }

        user_annotations = typing.get_type_hints(cls, include_extras=True)

        field_annotations: dict[str, Any] = {}
        field_defaults: dict[str, Any] = {}
        object_annotations: dict[str, Any] = {}
        object_defaults: dict[str, Any] = {}
        relation_models: dict[str, type] = {}
        custom_order_annotations: dict[str, Any] = {}
        custom_order_defaults: dict[str, Any] = {}
        custom_orders: dict[str, Callable[..., Any]] = {}

        for fname, ann in user_annotations.items():
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if ann is strawberry.auto:
                if fname in col_types:
                    field_annotations[fname] = Ordering | None
                    field_defaults[fname] = strawberry.UNSET
                elif fname in rel_info:
                    rel_model = rel_info[fname]
                    rel_order = self._order_registry.get(rel_model)
                    if rel_order is not None:
                        object_annotations[fname] = rel_order | None
                        object_defaults[fname] = strawberry.UNSET
                        relation_models[fname] = rel_model

        for attr_name in list(vars(cls)):
            method = getattr(cls, attr_name, None)
            if callable(method) and getattr(method, _CUSTOM_ORDER_ATTR, False):
                custom_order_annotations[attr_name] = Ordering | None
                custom_order_defaults[attr_name] = strawberry.UNSET
                custom_orders[attr_name] = method

        field_type_name = f"{model.__name__}OrderField"
        if field_annotations:
            field_ns: dict[str, Any] = {
                "__annotations__": field_annotations,
                **field_defaults,
            }
            field_cls = type(field_type_name, (), field_ns)
            OrderFieldType = strawberry.input(field_cls, one_of=True)
        else:
            OrderFieldType = None

        order_type_name = f"{model.__name__}Order"
        order_ns: dict[str, Any] = {"__annotations__": {}}
        if OrderFieldType is not None:
            order_ns["__annotations__"]["field"] = OrderFieldType | None
            order_ns["field"] = strawberry.UNSET

        if object_annotations:
            obj_type_name = f"{model.__name__}OrderObject"
            obj_ns: dict[str, Any] = {
                "__annotations__": object_annotations,
                **object_defaults,
            }
            obj_cls = type(obj_type_name, (), obj_ns)
            OrderObjectType = strawberry.input(obj_cls, one_of=True)
            order_ns["__annotations__"]["object"] = OrderObjectType | None
            order_ns["object"] = strawberry.UNSET

        for cname, cann in custom_order_annotations.items():
            order_ns["__annotations__"][cname] = cann
            order_ns[cname] = custom_order_defaults[cname]

        OrderCls = type(order_type_name, (), order_ns)
        OrderType = strawberry.input(OrderCls, one_of=True)
        OrderType.__orm_model__ = model  # type: ignore[attr-defined]
        if OrderFieldType is not None:
            OrderType._field_type = OrderFieldType  # type: ignore[attr-defined]
        if object_annotations:
            OrderType._object_type = OrderObjectType  # type: ignore[attr-defined]
            OrderType._relation_models = relation_models  # type: ignore[attr-defined]
        if custom_orders:
            OrderType._custom_orders = custom_orders  # type: ignore[attr-defined]

        self._order_registry[model] = OrderType
        return OrderType

    # -- Group-by type generation --------------------------------------------

    _NUMERIC_TYPES: tuple[type, ...] = (int, float)
    _COMPARABLE_TYPES: tuple[type, ...] = (
        int,
        float,
        datetime.date,
        datetime.time,
        datetime.datetime,
    )

    def group(self, model_or_type: type, **kwargs: Any) -> Any:
        """Generate a ``@oneOf`` group-by input for *model*.

        Boolean fields use ``Boolean`` (set to ``true`` to group by that
        column); date/datetime fields use ``DateGroupByOption`` so the
        caller can choose a truncation interval.
        """
        model = model_or_type
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")

        fields_meta = self._introspect_model(model)

        field_annotations: dict[str, Any] = {}
        field_defaults: dict[str, Any] = {}

        for fname, ftype, is_relation, _rel in fields_meta:
            if is_relation:
                continue
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if self._exclude_generated_sensitive_field(fname, include):
                continue
            if ftype in (datetime.date, datetime.datetime):
                field_annotations[fname] = DateGroupByOption | None
            else:
                field_annotations[fname] = bool | None
            field_defaults[fname] = strawberry.UNSET

        field_type_name = f"{model.__name__}GroupByField"
        field_ns: dict[str, Any] = {
            "__annotations__": field_annotations,
            **field_defaults,
        }
        field_cls = type(field_type_name, (), field_ns)
        GroupByFieldType = strawberry.input(field_cls, one_of=True)

        group_type_name = f"{model.__name__}GroupBy"
        group_ns: dict[str, Any] = {
            "__annotations__": {"field": GroupByFieldType | None},
            "field": strawberry.UNSET,
        }
        GroupByCls = type(group_type_name, (), group_ns)
        GroupByType = strawberry.input(GroupByCls, one_of=True)
        GroupByType._field_type = GroupByFieldType  # type: ignore[attr-defined]
        GroupByType.__orm_model__ = model  # type: ignore[attr-defined]

        self._group_registry[model] = GroupByType
        return GroupByType

    def group_type(
        self,
        model: type,
        *,
        include: list[str] | tuple[str, ...] | set[str] | None = None,
        exclude: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> Callable[[type], type]:
        """Decorator that builds a ``@oneOf`` group-by input from a user class.

        ``auto`` annotations are expanded to ``Boolean`` or
        ``DateGroupByOption``.  Methods decorated with ``@group_field``
        become additional top-level keys on the group-by input.
        """

        def decorator(cls: type) -> type:
            return self._build_custom_group_type(
                cls, model, include=include, exclude=exclude
            )

        return decorator

    def _build_custom_group_type(
        self,
        cls: type,
        model: type,
        *,
        include: Any = None,
        exclude: Any = None,
    ) -> type:
        fields_meta = self._introspect_model(model)
        col_types = {
            fname: ftype for fname, ftype, is_rel, _ in fields_meta if not is_rel
        }

        user_annotations = typing.get_type_hints(cls, include_extras=True)

        field_annotations: dict[str, Any] = {}
        field_defaults: dict[str, Any] = {}
        custom_group_annotations: dict[str, Any] = {}
        custom_group_defaults: dict[str, Any] = {}
        custom_groups: dict[str, Callable[..., Any]] = {}

        for fname, ann in user_annotations.items():
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if ann is strawberry.auto and fname in col_types:
                ftype = col_types[fname]
                if ftype in (datetime.date, datetime.datetime):
                    field_annotations[fname] = DateGroupByOption | None
                else:
                    field_annotations[fname] = bool | None
                field_defaults[fname] = strawberry.UNSET

        for attr_name in list(vars(cls)):
            method = getattr(cls, attr_name, None)
            if callable(method) and getattr(method, _CUSTOM_GROUP_ATTR, False):
                custom_group_annotations[attr_name] = bool | None
                custom_group_defaults[attr_name] = strawberry.UNSET
                custom_groups[attr_name] = method

        field_type_name = f"{model.__name__}GroupByField"
        if field_annotations:
            field_ns: dict[str, Any] = {
                "__annotations__": field_annotations,
                **field_defaults,
            }
            field_cls = type(field_type_name, (), field_ns)
            GroupByFieldType = strawberry.input(field_cls, one_of=True)
        else:
            GroupByFieldType = None

        group_type_name = f"{model.__name__}GroupBy"
        group_ns: dict[str, Any] = {"__annotations__": {}}
        if GroupByFieldType is not None:
            group_ns["__annotations__"]["field"] = GroupByFieldType | None
            group_ns["field"] = strawberry.UNSET

        for cname, cann in custom_group_annotations.items():
            group_ns["__annotations__"][cname] = cann
            group_ns[cname] = custom_group_defaults[cname]

        GroupByCls = type(group_type_name, (), group_ns)
        GroupByType = strawberry.input(GroupByCls, one_of=True)
        GroupByType.__orm_model__ = model  # type: ignore[attr-defined]
        if GroupByFieldType is not None:
            GroupByType._field_type = GroupByFieldType  # type: ignore[attr-defined]
        if custom_groups:
            GroupByType._custom_groups = custom_groups  # type: ignore[attr-defined]

        self._group_registry[model] = GroupByType
        return GroupByType

    # -- Aggregate type registration -----------------------------------------

    def aggregate(self, model_or_type: type, **kwargs: Any) -> Any:
        """Return a marker that stores the aggregate class for *model*.

        With no custom class, aggregation uses all introspected fields.
        """
        return None

    def aggregate_type(
        self,
        model: type,
        *,
        include: list[str] | tuple[str, ...] | set[str] | None = None,
        exclude: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> Callable[[type], type]:
        """Decorator that registers a user-defined aggregate class.

        ``auto`` annotations select which fields get standard sum/avg/min/max.
        Methods decorated with ``@aggregate_field`` add custom computed
        aggregate fields.

        Usage::

            @orm.aggregate_type(Order)
            class OrderAggregation:
                amount: auto
                quantity: auto

                @aggregate_field
                def total_revenue(self, columns) -> float:
                    from sqlalchemy import func
                    return func.sum(columns.amount * columns.quantity)
        """

        def decorator(cls: type) -> type:
            cls.__orm_aggregate_model__ = model  # type: ignore[attr-defined]
            return cls

        return decorator

    # -- Aggregate output type generation ------------------------------------

    def _build_aggregate_types(
        self, model: type, aggregate_cls: type | None = None
    ) -> AggregateMeta:
        """Build the aggregate output types and return an ``AggregateMeta``.

        When *aggregate_cls* is provided (from ``@orm.aggregate_type``),
        only fields annotated with ``auto`` on that class are included in
        the standard sub-aggregates (sum/avg/min/max), and methods
        decorated with ``@aggregate_field`` become additional top-level
        fields on the aggregates type.

        Cached per ``(model, aggregate_cls)`` so repeated calls return
        the same types.
        """
        import datetime as _dt

        cache_key = (model, aggregate_cls)
        cached = self._aggregate_type_cache.get(cache_key)
        if cached is not None:
            return cached

        fields_meta = self._introspect_model(model)
        model_name = model.__name__

        include_fields: set[str] | None = None
        custom_agg_handlers: list[tuple[str, Callable[..., Any], type]] = []

        if aggregate_cls is not None:
            user_hints = typing.get_type_hints(aggregate_cls, include_extras=True)
            include_fields = {k for k, v in user_hints.items() if v is strawberry.auto}
            for attr_name in list(vars(aggregate_cls)):
                handler = getattr(aggregate_cls, attr_name, None)
                if callable(handler) and getattr(
                    handler, _CUSTOM_AGGREGATE_ATTR, False
                ):
                    ret_type = typing.get_type_hints(handler).get("return", float)
                    custom_agg_handlers.append((attr_name, handler, ret_type))

        numeric_fields: list[tuple[str, type]] = []
        comparable_fields: list[tuple[str, type]] = []
        groupable_fields: list[tuple[str, type]] = []

        for fname, ftype, is_relation, _rel in fields_meta:
            if is_relation:
                continue
            if include_fields is not None and fname not in include_fields:
                groupable_fields.append((fname, ftype))
                continue
            if ftype in self._NUMERIC_TYPES:
                numeric_fields.append((fname, ftype))
                comparable_fields.append((fname, ftype))
            elif ftype in (_dt.date, _dt.datetime, _dt.time):
                comparable_fields.append((fname, ftype))
            groupable_fields.append((fname, ftype))

        def _make_sub_agg(prefix: str, fields: list[tuple[str, type]]) -> type | None:
            if not fields:
                return None
            ann: dict[str, Any] = {}
            defs: dict[str, Any] = {}
            for fname, _ in fields:
                ann[fname] = float | None
                defs[fname] = None
            cls_name = f"{model_name}{prefix}Aggregates"
            ns = {"__annotations__": ann, **defs}
            return strawberry.type(type(cls_name, (), ns))

        SumType = _make_sub_agg("Sum", numeric_fields)
        AvgType = _make_sub_agg("Avg", numeric_fields)
        MinType = _make_sub_agg("Min", comparable_fields)
        MaxType = _make_sub_agg("Max", comparable_fields)

        agg_ann: dict[str, Any] = {"count": int}
        agg_defs: dict[str, Any] = {"count": 0}
        for label, sub_type in [
            ("sum", SumType),
            ("avg", AvgType),
            ("min", MinType),
            ("max", MaxType),
        ]:
            if sub_type is not None:
                agg_ann[label] = sub_type | None
                agg_defs[label] = None

        for field_name, _handler, ret_type in custom_agg_handlers:
            agg_ann[field_name] = ret_type | None
            agg_defs[field_name] = None

        agg_cls_name = f"{model_name}Aggregates"
        agg_ns = {"__annotations__": agg_ann, **agg_defs}
        AggregatesType = strawberry.type(type(agg_cls_name, (), agg_ns))

        key_ann: dict[str, Any] = {}
        key_defs: dict[str, Any] = {}
        for fname, _ftype in groupable_fields:
            key_ann[fname] = str | None
            key_defs[fname] = None
        GroupKeyType = strawberry.type(
            type(f"{model_name}GroupKey", (), {"__annotations__": key_ann, **key_defs})
        )

        meta = AggregateMeta(
            model=model,
            aggregates_type=AggregatesType,
            group_key_type=GroupKeyType,
            sum_type=SumType,
            avg_type=AvgType,
            min_type=MinType,
            max_type=MaxType,
            numeric_fields=numeric_fields,
            comparable_fields=comparable_fields,
            groupable_fields=groupable_fields,
            custom_fields=custom_agg_handlers,
        )
        self._aggregate_type_cache[cache_key] = meta
        return meta

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

    def materialize_query(self, query: Any, info: Any) -> Any:
        return query

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
        unlink: bool = False,
        delete: bool = False,
    ) -> type:
        return make_ref_type(
            model, create=create, update=update, unlink=unlink, delete=delete
        )

    # -- Shared helpers ------------------------------------------------------

    def _type_name_for_model(self, model: type) -> str | None:
        for type_name, m in self._type_registry.items():
            if m is model:
                return type_name
        return None

    def _relation_type_from_annotation(self, ann: Any) -> Any | None:
        import typing as _typing
        from types import UnionType

        el = extract_element_type(ann)
        if el is not None:
            candidate = el
        else:
            origin = _typing.get_origin(ann)
            if origin in (_typing.Union, UnionType):
                args = [a for a in _typing.get_args(ann) if a is not type(None)]
                candidate = args[0] if len(args) == 1 else None
            else:
                candidate = ann
        if candidate is None or not isinstance(candidate, type):
            return None
        if getattr(candidate, "__orm_model__", None) is not None:
            return candidate
        return None

    def _check_lazy_relation_fields(
        self,
        cls: type,
        model: type,
        annotations: dict[str, Any],
    ) -> None:
        if self._lazy_resolution == "off":
            return

        from strawberry_orm.types import FieldDefinition

        for field_name, ann in annotations.items():
            if self._relation_type_from_annotation(ann) is None:
                continue
            if field_name in vars(cls):
                val = vars(cls)[field_name]
                if isinstance(val, FieldDefinition):
                    if val.disable_optimization:
                        continue
                elif callable(val):
                    continue
            message = (
                f"Field '{field_name}' on {model.__name__} resolves a related ORM "
                f"type lazily. Add an explicit resolver, use "
                f"orm.field(load=[...], disable_optimization=True) to silence, and "
                f"mount extensions=[orm.optimizer_extension()] on the schema for "
                f"eager loading."
            )
            if self._lazy_resolution == "error":
                raise ValueError(message)
            warnings.warn(message, stacklevel=4)

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
        group: Any = None,
        aggregate: Any = None,
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
            if ann is strawberry.auto and field_name in col_types:
                annotations[field_name] = col_types[field_name]

        cls.__annotations__ = annotations
        cls.__orm_model__ = model  # type: ignore[attr-defined]

        if self._warn_sensitive:
            for field_name in annotations:
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

        if group is not None:
            cls.__orm_group__ = group  # type: ignore[attr-defined]

        if aggregate is not None:
            cls.__orm_aggregate__ = aggregate  # type: ignore[attr-defined]

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
                    delattr(cls, attr_name)
            elif getattr(val, "_orm_auto_field", False):
                delattr(cls, attr_name)

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
        self._graphql_type_registry[model] = result
        return result
