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
from difflib import get_close_matches
from typing import Any, Literal

import strawberry

from strawberry_orm.filters import (
    _CUSTOM_AGGREGATE_ATTR,
    _CUSTOM_FILTER_ATTR,
    _CUSTOM_GROUP_ATTR,
    _CUSTOM_ORDER_ATTR,
    TYPE_TO_LOOKUP,
    ReferenceLookup,
    StringLookup,
    StringLookupNoRegex,
)
from strawberry_orm.mutations import make_ref_type
from strawberry_orm.optimizer import OptimizerStore
from strawberry_orm.types import DateGroupByOption, Ordering

LazyResolutionMode = Literal["off", "warn", "error"]
_LAZY_RESOLUTION_MODES: frozenset[str] = frozenset({"off", "warn", "error"})

FieldMeta = tuple[str, type, bool, type | None]

_FILTER_RELATION_PRESENCE_ERROR = (
    "Filter is_null is only valid under object.<relation> for FK presence on the "
    "parent row. Use field.<fk_id>.isNull for the FK column, or nest under object."
)

_SENSITIVE_PATTERNS = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|hash|ssn|"
    r"credit_card|creditcard|private_key|privatekey|admin|staff|"
    r"superuser|permission|role)",
    re.IGNORECASE,
)


def _annotate_filter_relation_presence(FilterCls: type) -> None:
    """Add ``is_null`` for FK presence when the filter is used under ``object``."""
    FilterCls.__annotations__["is_null"] = bool | None
    FilterCls.is_null = strawberry.UNSET


_ROW_SCOPE_HOOK = "scope_rows"
#: What the hook was called before 0.15. Left in place it is simply not read,
#: so the rows it was written to hide come back instead.
_LEGACY_ROW_SCOPE_HOOK = "get_queryset"


_KNOWN_FILTER_KEYS = frozenset({"field", "object", "all", "any", "not_", "one_of"})
_KNOWN_ORDER_KEYS = frozenset({"field", "object"})


def _set_scoped_ordering_allowance(order_type: type, model: type, allowed: Any) -> None:
    """Record which relations may be ordered through into a scoped type.

    Kept on the order type rather than keyed by model, so one order input
    opting in cannot widen another order input over the same model.

    ``True`` allows every relation this order type can traverse. Naming them
    individually is the safer habit, but it cannot be done without first
    knowing the set, which only exists once the type is built - so a schema
    that has decided its parents always imply readable children would
    otherwise have to list them by hand.
    """
    traversable = set(getattr(order_type, "_relation_models", {}))
    if allowed is True:
        order_type._scoped_ordering_allowed = frozenset(traversable)  # type: ignore[attr-defined]
        return
    names = frozenset(allowed or ())
    order_type._scoped_ordering_allowed = names  # type: ignore[attr-defined]
    unknown = names - traversable
    if unknown:
        raise ValueError(
            f"allow_scoped_ordering names {sorted(unknown)} on "
            f"{model.__name__}, which {order_type.__name__} cannot order "
            f"through. Expected one of: "
            f"{sorted(getattr(order_type, '_relation_models', {})) or 'none'}."
        )


def scoped_ordering_allowed(order_type: Any, relation: str) -> bool:
    """Whether *order_type* opted out of the scoped-ordering restriction."""
    return relation in getattr(order_type, "_scoped_ordering_allowed", ())


def _nested_order_types(order_type: Any) -> dict[str, type]:
    """Order input reachable through each relation, by relation name.

    Read off the annotations rather than the model-keyed registry: the nested
    input was captured when this order type was built, so a later order type
    over the same model is a different class and must not stand in for it.
    """
    object_type = getattr(order_type, "_object_type", None)
    nested: dict[str, type] = {}
    for relation, annotation in getattr(object_type, "__annotations__", {}).items():
        for arg in typing.get_args(annotation):
            if isinstance(arg, type) and arg is not type(None):
                nested[relation] = arg
                break
    return nested


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


@dataclass
class RelationConnectionSpec:
    """What a connection field needs in order to be read one window at a time."""

    model: type
    field_name: str
    relation: str
    related_model: type
    #: Column on the related rows holding the parent key, so a window can
    #: partition by it and a grouped count can total by it.
    key_field: str


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
        self._relation_connections: dict[tuple[str, str], Any] = {}
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
        self._type_queryset_owner: dict[type, str] = {}
        self._warn_sensitive: bool = kwargs.get("warn_sensitive", True)
        self._warn_missing_scope: bool = kwargs.get("warn_missing_scope", True)
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
        self._enable_optimizer: bool = kwargs.get("enable_optimizer", True)
        self._strict_hints: bool = kwargs.get("strict_hints", True)
        self._batch_relations: bool = kwargs.get("batch_relations", True)
        self._type_excludes: dict[type, set[str]] = {}
        self._input_registry: dict[type, list[tuple[str, set[str]]]] = {}

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
        self._check_input_does_not_expose_excluded(model, type_name, annotations)
        ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
        cls = type(type_name, (), ns)
        self._input_registry.setdefault(model, []).append((type_name, set(annotations)))
        return strawberry.input(cls)

    def _get_pk_names(self, model: type) -> set[str]:
        """Return PK field names. Subclasses may override for ORM-specific logic."""
        fields_meta = self._introspect_model(model)
        pk_candidates = set()
        for fname, _ftype, _is_rel, _rel in fields_meta:
            if fname == "id" or fname.endswith("_id"):
                pk_candidates.add(fname)
        return {"id"} if "id" in pk_candidates else set()

    @staticmethod
    def _is_forward_fk_attname_field(
        is_relation: bool,
        related_model: type | None,
    ) -> bool:
        """True for FK column attnames (e.g. ``author_id``), not relation keys."""
        return not is_relation and related_model is not None

    def _resolve_filter_lookup_type(
        self,
        fname: str,
        ftype: type,
        *,
        pk_names: set[str],
        enable_regex: bool = False,
    ) -> type | None:
        """Pick a lookup input type for a scalar filter field."""
        if fname in pk_names and ftype is not int:
            return ReferenceLookup
        lookup_type = self._filter_overrides.get(ftype) or TYPE_TO_LOOKUP.get(ftype)
        if lookup_type is None:
            return None
        if lookup_type is StringLookup and not enable_regex:
            return StringLookupNoRegex
        return lookup_type

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
        pending_self_relations: list[str] = []
        pk_names = self._get_pk_names(model)

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
                elif rel_model == model:
                    pending_self_relations.append(fname)
                    object_defaults[fname] = strawberry.UNSET
                    relation_models[fname] = rel_model
                continue
            if self._is_forward_fk_attname_field(is_relation, rel_model):
                continue
            lookup_type = self._resolve_filter_lookup_type(
                fname,
                ftype,
                pk_names=pk_names,
                enable_regex=enable_regex,
            )
            if lookup_type is not None:
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
        _annotate_filter_relation_presence(FilterCls)

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

        if pending_self_relations:
            if project is None:
                self._register_model_filter(model, FilterType)
            return self.filter(
                model,
                include=include,
                exclude=exclude,
                project=project,
            )

        if project is None:
            self._register_model_filter(model, FilterType)
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
        allow_scoped_ordering = kwargs.get("allow_scoped_ordering")

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
        _set_scoped_ordering_allowance(OrderType, model, allow_scoped_ordering)
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
        pk_names = self._get_pk_names(model)
        fk_attnames = {
            fname
            for fname, _, is_rel, rel in fields_meta
            if self._is_forward_fk_attname_field(is_rel, rel)
        }

        user_annotations = typing.get_type_hints(cls, include_extras=True)

        field_annotations: dict[str, Any] = {}
        field_defaults: dict[str, Any] = {}
        object_annotations: dict[str, Any] = {}
        object_defaults: dict[str, Any] = {}
        relation_models: dict[str, type] = {}
        pending_self_relations: list[str] = []
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
                    if fname in fk_attnames:
                        continue
                    ftype = col_types[fname]
                    lookup_type = self._resolve_filter_lookup_type(
                        fname,
                        ftype,
                        pk_names=pk_names,
                        enable_regex=enable_regex,
                    )
                    if lookup_type is not None:
                        field_annotations[fname] = lookup_type | None
                        field_defaults[fname] = strawberry.UNSET
                elif fname in rel_info:
                    rel_model = rel_info[fname]
                    rel_filter = self._filter_registry.get(rel_model)
                    if rel_filter is not None:
                        object_annotations[fname] = rel_filter | None
                        object_defaults[fname] = strawberry.UNSET
                        relation_models[fname] = rel_model
                    elif rel_model == model:
                        pending_self_relations.append(fname)
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
        _annotate_filter_relation_presence(FilterCls)

        ObjectType = None
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
            FilterCls = type(filter_type_name, (), filter_ns)
            FilterCls.__annotations__["all"] = list[FilterCls] | None
            FilterCls.__annotations__["any"] = list[FilterCls] | None
            FilterCls.__annotations__["not_"] = FilterCls | None
            FilterCls.__annotations__["one_of"] = list[FilterCls] | None
            FilterCls.all = strawberry.UNSET
            FilterCls.any = strawberry.UNSET
            FilterCls.not_ = strawberry.field(default=strawberry.UNSET, name="not")
            FilterCls.one_of = strawberry.UNSET
            _annotate_filter_relation_presence(FilterCls)

        FilterType = strawberry.input(FilterCls, one_of=True)
        FilterType.__orm_model__ = model  # type: ignore[attr-defined]
        if FieldType is not None:
            FilterType._field_type = FieldType  # type: ignore[attr-defined]
        if object_annotations:
            FilterType._object_type = ObjectType  # type: ignore[attr-defined]
            FilterType._relation_models = relation_models  # type: ignore[attr-defined]
        if custom_filters:
            FilterType._custom_filters = custom_filters  # type: ignore[attr-defined]

        if pending_self_relations:
            self._filter_registry[model] = FilterType
            return self._build_custom_filter_type(
                cls, model, include=include, exclude=exclude
            )

        self._filter_registry[model] = FilterType
        return FilterType

    def order_type(
        self,
        model: type,
        *,
        include: list[str] | tuple[str, ...] | set[str] | None = None,
        exclude: list[str] | tuple[str, ...] | set[str] | None = None,
        allow_scoped_ordering: list[str]
        | tuple[str, ...]
        | set[str]
        | bool
        | None = None,
    ) -> Callable[[type], type]:
        """Decorator that builds a ``@oneOf`` order input from a user class.

        ``auto`` annotations are expanded to ``Ordering | None``.
        Methods decorated with ``@order_field`` become additional top-level
        keys on the order input alongside ``field`` and ``object``.

        ``allow_scoped_ordering`` names relations that may be ordered through
        even though the type on the far side defines ``scope_rows``. See
        :meth:`check_scoped_order_traversal`.
        """

        def decorator(cls: type) -> type:
            return self._build_custom_order_type(
                cls,
                model,
                include=include,
                exclude=exclude,
                allow_scoped_ordering=allow_scoped_ordering,
            )

        return decorator

    def _build_custom_order_type(
        self,
        cls: type,
        model: type,
        *,
        include: Any = None,
        exclude: Any = None,
        allow_scoped_ordering: Any = None,
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
        _set_scoped_ordering_allowance(OrderType, model, allow_scoped_ordering)

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
            # min/max return exact column values and the group key echoes them
            # back, so a sensitive column is as exposed here as it would be on
            # the output type. Filters, ordering and group-by already drop it.
            if self._exclude_generated_sensitive_field(fname, include_fields):
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

        hint_keys = {"using", "scope", "compute", "disable_optimization"}
        if hint_keys & set(kwargs):
            return FieldDefinition(
                using=kwargs.get("using"),
                scope=kwargs.get("scope"),
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

    def _relation_target_model(self, model: type, relation: str) -> type | None:
        """Model on the far side of *relation*, or ``None`` if unknown."""
        return None

    def apply_type_scope(self, query: Any, model: type | None, info: Any) -> Any:
        """Apply *model*'s ``scope_rows`` to *query*, if one is registered."""
        if model is None:
            return query
        get_qs = self._type_querysets.get(model)
        return query if get_qs is None else get_qs(query, info)

    # -- Scoping across relation traversal -----------------------------------

    def _field_scope(self, model: type, relation: str) -> Any | None:
        """The ``scope=`` declared on *model*'s own ``relation`` field, if any."""
        type_name = self._type_name_for_model(model)
        if type_name is None:
            return None
        hints = self._store.get(type_name, relation)
        return getattr(hints, "scope", None) if hints else None

    def relation_scope(
        self, model: type, relation: str, info: Any, *, on: str | None = None
    ) -> Any | None:
        """Everything that restricts the far side of *relation*, or ``None``.

        Filtering reaches the related table directly, which would otherwise
        bypass the row scoping applied when that relation is read. Both layers
        count: the related type's ``scope_rows`` and any ``scope=`` on this
        edge. They are composed in that order, the same order the read path
        uses.

        A ``on=`` field is not a relation, so the far side is found through
        the relation it names while the edge's own scope stays keyed to the
        field. Without that split the scope silently resolves to nothing.
        """
        related = self._relation_target_model(model, on or relation)
        if related is None:
            return None
        get_qs = self._type_querysets.get(related)
        field_scope = self._field_scope(model, relation)
        if get_qs is None and field_scope is None:
            return None
        if info is None:
            raise ValueError(
                f"Cannot filter through {model.__name__}.{relation}: "
                f"{related.__name__} is row-scoped and applying that needs the "
                f"resolver's info. Pass info= to apply_filters()."
            )

        from strawberry_orm.fields import call_scope

        def restrict(query: Any, resolver_info: Any) -> Any:
            if get_qs is not None:
                query = get_qs(query, resolver_info)
            if field_scope is not None:
                query = call_scope(field_scope, query, resolver_info)
            return query

        return restrict

    def reject_scoped_order_traversal(
        self, model: type, relation: str, order_type: Any = None
    ) -> None:
        """Refuse to order by a relation whose rows the caller cannot read.

        Unlike filtering, ordering cannot be made safe by restricting the join:
        the resulting sequence itself ranks the hidden rows.

        ``orm.schema()`` rejects these at build time. This is the backstop for
        schemas built with ``strawberry.Schema`` directly, which skips that
        check.
        """
        related = self._relation_target_model(model, relation)
        if related is None or related not in self._type_querysets:
            return
        if scoped_ordering_allowed(order_type, relation):
            return
        raise ValueError(self._scoped_order_message(model, relation, related))

    @staticmethod
    def _scoped_order_message(model: type, relation: str, related: type) -> str:
        return (
            f"Cannot order by {model.__name__}.{relation}: {related.__name__} is "
            f"scoped by scope_rows, so ordering would rank rows the caller "
            f"cannot read. Order by a column on {model.__name__}, or pass "
            f"allow_scoped_ordering=['{relation}'] when building the order type "
            f"for {model.__name__} if every readable {model.__name__} is "
            f"guaranteed to have a readable {related.__name__}."
        )

    def check_scoped_order_traversal(self) -> None:
        """Raise if any exposed order input can sort by rows scoping hides.

        Runs at schema build so the problem surfaces on startup rather than on
        the first client query that happens to use that sort.
        """
        problems: list[str] = []
        seen: set[tuple[int, int]] = set()

        def walk(order_type: Any, model: type) -> None:
            key = (id(order_type), id(model))
            if order_type is None or key in seen:
                return
            seen.add(key)
            nested = _nested_order_types(order_type)
            for relation, related in getattr(
                order_type, "_relation_models", {}
            ).items():
                if related in self._type_querysets and not scoped_ordering_allowed(
                    order_type, relation
                ):
                    problems.append(
                        self._scoped_order_message(model, relation, related)
                    )
                walk(nested.get(relation), related)

        for model, graphql_type in self._graphql_type_registry.items():
            walk(getattr(graphql_type, "__orm_order__", None), model)

        if problems:
            raise ValueError("\n".join(dict.fromkeys(problems)))

    def _check_input_does_not_expose_excluded(
        self, model: type, type_name: str, fields: dict[str, Any]
    ) -> None:
        """Reject a mutation input that can write a column the type hides.

        Excluding a column from the output type is a read control; it does not
        touch the generated input, so the field stays writable. That is mass
        assignment: a caller can set a value they are not allowed to read back.
        """
        hidden = self._type_excludes.get(model)
        if not hidden:
            return
        leaked = sorted(hidden & set(fields))
        if leaked:
            raise ValueError(
                f"{type_name} can write {leaked}, which the GraphQL type for "
                f"{model.__name__} excludes. Hiding a column from reads leaves it "
                f"writable unless the input excludes it too. Pass "
                f"exclude={leaked!r} to the orm.input()/orm.partial() call."
            )

    def _check_excluded_fields_are_not_writable(
        self,
        model: type,
        type_name: str,
        exclude: list[str] | tuple[str, ...] | set[str] | None,
    ) -> None:
        """The same check for inputs generated *before* the type was declared."""
        if not exclude:
            return
        excluded = set(exclude)
        for input_name, fields in self._input_registry.get(model, []):
            leaked = sorted(excluded & fields)
            if leaked:
                raise ValueError(
                    f"{type_name} excludes {leaked} but {input_name} can still "
                    f"write them, so a caller can set values they cannot read. "
                    f"Pass exclude={leaked!r} to the orm.input()/orm.partial() "
                    f"call that builds {input_name}."
                )

    def _register_model_filter(self, model: type, filter_type: type) -> None:
        """Record the filter used for *model*, refusing to widen it silently.

        Nested ``object:`` traversal reuses the filter registered for the
        related model, and the registry is keyed by model. A second, broader
        filter built anywhere for the same model therefore re-exposes columns a
        narrower filter had excluded - including through other types' filters.
        """
        existing = self._filter_registry.get(model)
        if existing is not None and existing is not filter_type:
            before = self._generated_input_field_names(existing)
            after = self._generated_input_field_names(filter_type)
            widened = sorted(after - before)
            if widened:
                raise ValueError(
                    f"A filter for {model.__name__} already excludes {widened}, "
                    f"but another orm.filter({model.__name__}) call exposes them. "
                    f"Nested object traversal reuses one filter per model, so the "
                    f"broader one would re-expose those columns everywhere. Build "
                    f"a single filter per model, or pass the same exclude to both."
                )
        self._filter_registry[model] = filter_type

    @staticmethod
    def _generated_input_field_names(input_cls: Any) -> set[str]:
        """Column names reachable through a generated filter/order input."""
        if input_cls is None:
            return set()
        nested = getattr(input_cls, "__annotations__", {}).get("field")
        args = getattr(nested, "__args__", None)
        inner = args[0] if args else nested
        return set(getattr(inner, "__annotations__", {}))

    def _check_excluded_fields_are_not_queryable(
        self,
        type_name: str,
        exclude: list[str] | tuple[str, ...] | set[str] | None,
        filters: Any,
        order: Any,
        group: Any,
    ) -> None:
        """Refuse to expose an excluded column through filter/order/group.

        Hiding a column from the output type but leaving it filterable turns it
        into an oracle: ``startsWith`` probes read the value one character at a
        time. The generated inputs are built from the model, so they do not know
        about the type's ``exclude`` unless it is passed to them as well.
        """
        if not exclude:
            return

        excluded = set(exclude)
        for label, input_cls in (
            ("filters", filters),
            ("order", order),
            ("group", group),
        ):
            leaked = sorted(excluded & self._generated_input_field_names(input_cls))
            if leaked:
                raise ValueError(
                    f"{type_name} excludes {leaked} but its {label} input still "
                    f"exposes them, which makes the hidden values readable one "
                    f"probe at a time. Pass exclude={leaked!r} to the "
                    f"orm.{'filter' if label == 'filters' else label}() call too."
                )

    def _register_type_scope(self, cls: type, model: type, type_name: str) -> None:
        """Record ``scope_rows`` for *model*, refusing to silently replace it.

        Row scoping is resolved from the model, because at prefetch time the
        optimizer only knows which table it is loading. Two GraphQL types over
        the same model therefore cannot each carry their own scope: the second
        registration would overwrite the first and every reader would silently
        get the last one, turning a restrictive scope into a permissive one.
        That is an authorization bypass, so it is rejected outright.
        """
        existing = self._type_querysets.get(model)
        new = getattr(cls, _ROW_SCOPE_HOOK)
        if existing is not None and getattr(existing, "__func__", existing) is not (
            getattr(new, "__func__", new)
        ):
            previous = self._type_queryset_owner.get(model, "another type")
            raise ValueError(
                f"{type_name} and {previous} both define scope_rows for "
                f"{model.__name__}. Row scoping is resolved per model, so the "
                f"second definition would silently replace the first for every "
                f"reader. Expose one GraphQL type per model, or move the "
                f"difference into the resolver that returns each type."
            )
        self._type_querysets[model] = new
        self._type_queryset_owner[model] = type_name

    def relation_names(self, model: type) -> set[str]:
        """Return the names of *model*'s relations, for hint validation."""
        raise NotImplementedError

    # -- Relation batching ---------------------------------------------------
    #
    # Backends that can introspect a query opt in by overriding these three.
    # The default refuses to batch, which is always correct - just slower.

    def instance_pk(self, instance: Any) -> Any:
        """Primary key of *instance*, used to key batched rows back to parents."""
        return None

    def split_parent_predicate(
        self, query: Any, parent_pk: Any
    ) -> tuple[str, Any, Any] | None:
        """Split *query* into its parent predicate and the rest.

        Returns ``(attr_name, key_handle, remainder)`` when exactly one
        top-level equality predicate matches *parent_pk*, or ``None`` when the
        rewrite cannot be proven equivalent.
        """
        return None

    def query_signature(self, query: Any) -> str | None:
        """Stable structural signature of *query*, or ``None`` if uncomputable."""
        return None

    def apply_key_filter(
        self, query: Any, attr_name: str, key_handle: Any, keys: list[Any]
    ) -> Any:
        """Restrict *query* to rows whose parent key is one of *keys*."""
        raise NotImplementedError  # pragma: no cover

    def _validate_scoped_relation(
        self,
        model: type,
        type_name: str,
        field_name: str,
        hints: Any,
    ) -> None:
        """A scope narrows a relation, so the field has to name one.

        ``on=`` supplies that name when the field is called something else,
        which is also the only way one relation can back more than one field.
        """
        if (hints.scope is None and hints.on is None) or not self._strict_hints:
            return
        names = self.relation_names(model)
        relation = hints.on or field_name
        if relation in names:
            return
        suggestion = get_close_matches(relation, sorted(names), n=1)
        hint_text = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
        if hints.on is not None:
            raise ValueError(
                f"{type_name}.{field_name}: on={relation!r} names the relation "
                f"this field is served from, but {model.__name__} has no relation "
                f"{relation!r}.{hint_text}"
            )
        raise ValueError(
            f"{type_name}.{field_name}: scope= narrows the rows loaded through a "
            f"relation, but {model.__name__} has no relation {field_name!r}."
            f"{hint_text} Pass on= if the relation is named something else."
        )

    def _validate_hints(
        self,
        model: type,
        type_name: str,
        field_name: str,
        hints: Any,
    ) -> None:
        """Reject ``using=`` names that can never resolve, at schema-build time."""
        if not self._strict_hints or not hints.using:
            return

        names = self.relation_names(model)
        for rel in hints.using:
            if rel in names:
                continue
            where = f"{type_name}.{field_name}: "
            if "__" in rel or "." in rel:
                raise ValueError(
                    f"{where}multi-hop using={rel!r} is not supported. Declare the "
                    f"first hop only, or scope the relation on its own type."
                )
            suggestion = get_close_matches(rel, sorted(names), n=1)
            hint_text = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
            raise ValueError(
                f"{where}{model.__name__} has no relation {rel!r}.{hint_text}"
            )

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
                f"orm.field(disable_optimization=True) to silence, and "
                f"use orm.schema(...) (optimizer enabled by default) for "
                f"eager loading."
            )
            if self._lazy_resolution == "error":
                raise ValueError(message)
            warnings.warn(message, stacklevel=4)

    def _check_missing_scope(self, cls: type, model: type, type_name: str) -> None:
        if not self._warn_missing_scope:
            return
        if model in self._type_querysets:
            return
        warnings.warn(
            f"GraphQL type '{type_name}' (model {model.__name__}) has no "
            f"scope_rows classmethod. Row-level scoping is not applied when "
            f"this model's rows load — define scope_rows on the @orm.type "
            f"class or set warn_missing_scope=False on the ORM.",
            stacklevel=4,
        )

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
        registers ``scope_rows``, and returns the resolved type name.
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

        self._check_excluded_fields_are_not_queryable(
            type_name, exclude, filters, order, group
        )
        self._check_excluded_fields_are_not_writable(model, type_name, exclude)
        if exclude:
            self._type_excludes.setdefault(model, set()).update(exclude)

        if isinstance(vars(cls).get(_ROW_SCOPE_HOOK), classmethod):
            self._register_type_scope(cls, model, type_name)
        elif isinstance(vars(cls).get(_LEGACY_ROW_SCOPE_HOOK), classmethod):
            # Refused rather than warned about: the class reads as scoped and
            # is not, so every row it meant to hide is being returned. That is
            # a widening of what a caller can read, and it happens on upgrade
            # without anything in the schema changing shape.
            raise ValueError(
                f"{type_name} defines {_LEGACY_ROW_SCOPE_HOOK}, which was "
                f"renamed to {_ROW_SCOPE_HOOK} in 0.15 and is no longer read. "
                f"Left as it is, {model.__name__} rows load unscoped and the "
                f"rows it was written to hide are returned. Rename it to "
                f"{_ROW_SCOPE_HOOK}."
            )

        self._check_missing_scope(cls, model, type_name)

        for attr_name in list(vars(cls)):
            val = getattr(cls, attr_name, None)
            if isinstance(val, FieldDefinition):
                hints = val.to_hints()
                self._validate_scoped_relation(model, type_name, attr_name, hints)
                self._validate_hints(model, type_name, attr_name, hints)
                self._store.register(type_name, attr_name, hints)
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
            elif getattr(val, "_orm_computed_hints", None) is not None:
                self._validate_hints(
                    model, type_name, attr_name, val._orm_computed_hints
                )
                self._store.register(type_name, attr_name, val._orm_computed_hints)
            elif getattr(val, "_orm_auto_field", False):
                pending = getattr(val, "_orm_pending_connection", None)
                if pending is not None:
                    self._rebuild_relation_connection(
                        cls, model, type_name, attr_name, pending
                    )
                    continue
                delattr(cls, attr_name)

        return type_name

    def _register_relation_connection(
        self, model: type, type_name: str, field_name: str, relation: str
    ) -> None:
        """Record what a connection field is served by, for the windowed read."""
        spec = self._relation_connection_spec(model, field_name, relation)
        if spec is None:
            raise ValueError(
                f"{type_name}.{field_name}: a connection over {relation!r} cannot "
                f"take each parent's page in one query, because those rows carry "
                f"no column tying them to a parent for a window to partition by. "
                f"Give it a resolver with @orm.connection.lazy instead."
            )
        # Resolution hands the extension a raw graphql-core info, which knows
        # the camelCase field name and not the Python one, so register both.
        head, *rest = field_name.split("_")
        camel = head + "".join(part.title() for part in rest)
        self._relation_connections[(type_name, field_name)] = spec
        self._relation_connections[(type_name, camel)] = spec

    def relation_connection_spec(self, info: Any) -> Any:
        """The spec for the connection being resolved, if this field is one."""
        type_name = getattr(getattr(info, "parent_type", None), "name", None)
        for attr in ("python_name", "field_name"):
            field_name = getattr(info, attr, None)
            if field_name is None:
                continue
            spec = self._relation_connections.get((type_name, field_name))
            if spec is not None:
                return spec
        return None

    def relation_base_query(self, spec: Any, pks: list[Any], info: Any) -> Any:
        """Every parent's related rows in one scoped query, ready to window."""
        raise NotImplementedError

    def _rebuild_relation_connection(
        self, cls: type, model: type, type_name: str, attr_name: str, pending: Any
    ) -> None:
        """Point a connection declared on a type at the parent's relation.

        Built from the annotation alone it queries the whole table, because
        ``__set_name__`` runs before ``@orm.type`` knows what the parent is.
        Here the model is known, so the field is rebuilt around a resolver that
        follows the relation of the same name.
        """
        hints = self._store.get(type_name, attr_name)
        relation = getattr(hints, "on", None) if hints else None
        relation = relation or attr_name
        if relation not in self.relation_names(model):
            raise ValueError(
                f"{type_name}.{attr_name}: a connection on a type is served by "
                f"the parent's relation, but {model.__name__} has no relation "
                f"{relation!r}. Name it with on=, or give the connection a "
                f"resolver with @orm.connection.lazy."
            )
        if not self._supports_windowed_pages:
            raise ValueError(
                f"{type_name}.{attr_name}: a connection over a relation needs a "
                f"window function to take each parent's page in one query, which "
                f"this backend does not have - every parent would cost its own "
                f"query. Give it a resolver with @orm.connection.lazy, which says "
                f"that plainly."
            )
        resolver = self._make_relation_query_resolver(model, attr_name, relation)
        setattr(cls, attr_name, pending.rebuild_for_relation(resolver))
        self._register_relation_connection(model, type_name, attr_name, relation)

    def _make_relation_query_resolver(
        self, model: type, field_name: str, relation: str
    ) -> Any:
        """Backend hook: a resolver returning the parent's rows as a query.

        A connection paginates and counts what it is given, so this always
        returns an unexecuted query rather than rows.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot serve a connection over a relation."
        )

    def resolves_itself(self, type_name: str | None, field_name: str) -> bool:
        """True when *field_name* answers without the prefetch the walk would add.

        Only a relation connection qualifies today. It is served from a
        windowed page covering every parent, so the prefetch the walk would
        otherwise add for a field sharing a relation's name is read by nobody.

        Having a resolver is not evidence of this: the backends install one for
        ordinary relation fields as well, and those do read the prefetch.
        """
        return (type_name, field_name) in self._relation_connections

    # -- on= fields ---------------------------------------------------------

    #: True when the backend can load one relation into an attribute of our
    #: choosing, so several views of it can be prefetched side by side.
    _supports_on_prefetch: bool = False

    #: True when the backend implements ``split_parent_predicate``, without
    #: which the batcher cannot collapse per-parent relation queries.
    _supports_relation_batching: bool = False

    #: True when ``batch_group_items`` really windows rather than looping per
    #: group, which is what lets a connection take every parent's page at once.
    _supports_windowed_pages: bool = False

    def _install_on_resolvers(
        self, graphql_type: type, model: type, type_name: str
    ) -> None:
        """Serve every ``on=`` field, which has no attribute of its own."""
        from strawberry.types.field import UNRESOLVED
        from strawberry.types.fields.resolver import StrawberryResolver

        for field in graphql_type.__strawberry_definition__.fields:
            field_name = field.python_name
            if not field_name or field.base_resolver is not None:
                continue
            hints = self._store.get(type_name, field_name)
            relation = getattr(hints, "on", None) if hints else None
            if relation is None:
                continue
            if not self._supports_on_prefetch:
                # Nothing here can prefetch a second view of a relation, so the
                # batcher is the only thing keeping this field's promise of one
                # query per view rather than one per parent row.
                if not self._supports_relation_batching:
                    raise ValueError(
                        f"{type_name}.{field_name}: on= cannot be eager on this "
                        f"backend. It can neither load a second view of "
                        f"{relation!r} alongside the first nor batch the "
                        f"per-parent queries, so the field would cost one query "
                        f"per parent row. Write it as orm.field.lazy, which says "
                        f"that plainly."
                    )
                if not self._batch_relations:
                    # Batching is on unless it was turned off deliberately, and
                    # quietly turning it back on would override that choice.
                    raise ValueError(
                        f"{type_name}.{field_name}: on= needs batch_relations on "
                        f"this backend, which cannot load a second view of "
                        f"{relation!r} alongside the first. With batching off the "
                        f"field would cost a query per parent row. Enable "
                        f"batching, or write it as orm.field.lazy instead."
                    )

            return_ann = getattr(graphql_type, "__annotations__", {})[field_name]
            field.base_resolver = StrawberryResolver(
                self._make_on_resolver(model, field_name, relation, return_ann),
                type_override=field.type if field.type is not UNRESOLVED else None,
            )

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
        self._install_on_resolvers(result, model, type_name)
        return result
