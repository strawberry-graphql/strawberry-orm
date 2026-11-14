"""Base backend with shared logic for all ORM adapters."""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import inspect
import re
import typing
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from decimal import Decimal
from difflib import get_close_matches
from types import MappingProxyType
from typing import Any, Literal

import strawberry
from strawberry.types.field import StrawberryField

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


def _metadata_only_field(value: Any) -> bool:
    """True for ``strawberry.field(metadata=...)`` that sets nothing else.

    On an ``@orm.type`` such a field only tags a model attribute, so the
    attribute is resolved as if the field were not there.
    """
    return (
        isinstance(value, StrawberryField)
        and bool(value.metadata)
        and value.base_resolver is None
        and value.default is dataclasses.MISSING
        and value.default_factory is dataclasses.MISSING
        and value.graphql_name is None
        and value.description is None
        and value.deprecation_reason is None
        and not value.permission_classes
        and not value.directives
        and not value.extensions
    )


def _reads_as_is(value: Any) -> bool:
    """True when a type field reads its column or relation exactly as stored.

    A resolver of the developer's own, an ``on=`` view, ``compute=`` or
    permission classes all show the caller something other than the column,
    or show it to fewer callers. Filtering or sorting on the column would get
    around that, so such a field does not open its column to the inputs. A
    ``scope=`` does: filter traversal applies the field's scope too.
    """
    from strawberry_orm.types import FieldDefinition

    if isinstance(value, FieldDefinition):
        return not (value.on or value.compute or value.permission_classes)
    if isinstance(value, StrawberryField):
        pending = getattr(value, "_orm_pending_connection", None)
        if pending is not None:
            return pending._kwargs.get("scope") is None
        return getattr(value, "_orm_reads_as_is", False) or _metadata_only_field(value)
    return True


def _inherited_reads_as_is(cls: type, *, excluded: set[str]) -> frozenset[str]:
    """Fields a base Strawberry type shows as stored that *cls* does not redeclare.

    ``cls.__annotations__`` holds only the class's own names, so a column an
    interface declares would otherwise never open to the type's filters and
    orders. Walk each base's Strawberry fields and count a field the same way
    an own annotation is counted: no resolver of its own, no permission
    classes, and not listed in the type's ``exclude=``.
    """
    own_declared = set(getattr(cls, "__annotations__", {})) | set(vars(cls))
    found: set[str] = set()
    for base in cls.__mro__[1:]:
        definition = getattr(base, "__strawberry_definition__", None)
        if definition is None:
            continue
        for field in definition.fields:
            name = field.python_name
            if not name or name in own_declared or name in excluded or name in found:
                continue
            if field.base_resolver is not None or field.permission_classes:
                continue
            found.add(name)
    return frozenset(found)


def _node_id_attr(graphql_type: type) -> str | None:
    """The attribute a Relay node annotates ``NodeID``, or ``None``.

    Strawberry evaluates every annotation of the class to find it, so this is
    only asked once a schema is built, when the types they name exist.
    """
    from strawberry import relay
    from strawberry.relay.exceptions import NodeIDAnnotationError

    if not issubclass(graphql_type, relay.Node):
        return None
    try:
        return graphql_type.resolve_id_attr()
    except (NodeIDAnnotationError, NameError):
        return None


def _common_tags(
    first: frozenset[str] | None, second: frozenset[str] | None
) -> frozenset[str] | None:
    """The schemas both tag sets are in, where ``None`` means every schema."""
    if first is None:
        return second
    if second is None:
        return first
    return first & second


def _in_schema(tags: frozenset[str] | None, schema_tags: frozenset[str] | None) -> bool:
    return schema_tags is None or tags is None or bool(tags & schema_tags)


def _intersect(
    covers: list[dict[str, frozenset[str] | None]],
) -> dict[str, frozenset[str] | None]:
    """The fields every cover keeps, each in the schemas all of them keep it in."""
    first, *rest = covers
    cover = dict(first)
    for other in rest:
        cover = {
            name: _common_tags(tags, other[name])
            for name, tags in cover.items()
            if name in other
        }
    return cover


_AGGREGATES = ("sum", "avg", "min", "max")


def _reached_input(item: Any, reached: set[Any]) -> bool:
    if isinstance(item, AggregateMeta):
        return item.aggregates_type in reached or item.group_key_type in reached
    return item in reached


def _input_parts(item: Any) -> list[tuple[str | None, Any, Any]]:
    """Each part an input is cut in, as ``(field on its container, part, container)``.

    A filter or order is cut in its ``field`` and ``object`` parts. Aggregates
    are cut in each sub-aggregate and in the group key, which has no container
    to drop it from: a connection that groups needs one.
    """
    if isinstance(item, AggregateMeta):
        parts: list[tuple[str | None, Any, Any]] = [
            (label, getattr(item, f"{label}_type"), item.aggregates_type)
            for label in _AGGREGATES
        ]
        parts.append((None, item.group_key_type, None))
    else:
        parts = [
            (key, getattr(item, f"_{key}_type", None), item)
            for key in ("field", "object")
        ]
    return [(label, part, container) for label, part, container in parts if part]


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


def hint_hops(path: str) -> list[str]:
    """The relations a ``using=`` entry walks, in order.

    ``author.profile`` and Django's ``author__profile`` name the same path.
    """
    return path.replace("__", ".").split(".")


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

    custom_fields: list[tuple[str, Callable[..., Any], type]] = dc_field(
        default_factory=list,
    )
    """Each entry is ``(field_name, handler_callable, python_return_type)``."""

    relation_keys: dict[str, AggregateMeta] = dc_field(default_factory=dict)
    """The related model's metadata behind each relation the key nests."""

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
        self._group_key_cache: dict[tuple, tuple[type, dict[str, Any]]] = {}
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
        #: Per GraphQL type, the columns and relations it shows as they are,
        #: each with the tags of the field showing it.
        self._shown_by_type: dict[type, dict[str, frozenset[str] | None]] = {}
        self._types_by_model: dict[type, list[type]] = {}
        #: Per model, every filter, order and group-by input and aggregate type
        #: built for it, so each is cut again as types arrive.
        self._model_inputs: dict[type, list[Any]] = {}

    def get_repo(self, model: type) -> Any | None:
        """Return an instantiated repo for *model*, or ``None``."""
        repo_cls = self._repos.get(model)
        if repo_cls is None:
            return None
        repo = repo_cls(self)
        if repo.model is None:
            repo.model = model
        return repo

    # -- Upserts -------------------------------------------------------------

    def apply_upsert(
        self, payload: Any, info: Any, *, authorize: Any | None = None
    ) -> Any:
        """Apply an input built by ``orm.mutations.upsert_node_input()``."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement the upsert operation"
        )

    def _find_for_upsert(
        self, repo: Any, model: type, query: dict[str, Any], info: Any
    ) -> Any:
        raise NotImplementedError(
            f"{type(self).__name__} does not implement the upsert operation"
        )

    async def _find_for_upsert_async(
        self, repo: Any, model: type, query: dict[str, Any], info: Any
    ) -> Any:
        raise NotImplementedError(
            f"{type(self).__name__} does not implement the upsert operation"
        )

    def _upsert_ops(self, info: Any) -> Any:
        """The calls :class:`~strawberry_orm.upserts.UpsertWriter` makes."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement the upsert operation"
        )

    def _upsert_relation_spec(self, model: type, field: str) -> Any:
        raise NotImplementedError(
            f"{type(self).__name__} does not implement the upsert operation"
        )

    def _upsert_writer(self, info: Any, authorize: Any | None) -> Any:
        from strawberry_orm.upserts import UpsertWriter

        return UpsertWriter(self, self._upsert_ops(info), info, authorize)

    def _ref_list_writer(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        authorize: Any | None,
    ) -> Any:
        """Validate the generated items in *refs*; ``None`` when there are none."""
        from strawberry_orm.upserts import has_upsert

        if not any(
            has_upsert(ref) or hasattr(type(ref), "__relation_policy__") for ref in refs
        ):
            return None
        writer = self._upsert_writer(info, authorize)
        spec = self._upsert_relation_spec(type(instance), field)
        writer.validate_refs(type(instance), spec, refs)
        return writer

    def _single_upsert_match(self, model: type, rows: list[Any]) -> Any:
        from strawberry_orm.upserts import UpsertValidationError

        if len(rows) > 1:
            raise UpsertValidationError(
                f"The upsert where matches more than one {model.__name__}; "
                f"match on fields that identify a single row"
            )
        return rows[0] if rows else None

    @staticmethod
    def _upsert_lookup_kwargs(query: dict[str, Any]) -> dict[str, Any]:
        """*query* as ORM keyword lookups, with ``id`` meaning the primary key."""
        return {
            ("pk" if name == "id" else name): value for name, value in query.items()
        }

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
        self._cover_shown_fields(model, FilterType)

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
        self._cover_shown_fields(model, OrderType)
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
        self._cover_shown_fields(model, FilterType)

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
        self._cover_shown_fields(model, OrderType)

        self._order_registry[model] = OrderType
        return OrderType

    # -- Group-by type generation --------------------------------------------

    _NUMERIC_TYPES: tuple[type, ...] = (int, float, Decimal)
    _COMPARABLE_TYPES: tuple[type, ...] = (
        int,
        float,
        Decimal,
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
        object_type, relation_models = self._group_object_arm(
            model, fields_meta, include, exclude
        )
        if object_type is not None:
            group_ns["__annotations__"]["object"] = object_type | None
            group_ns["object"] = strawberry.UNSET
        GroupByCls = type(group_type_name, (), group_ns)
        GroupByType = strawberry.input(GroupByCls, one_of=True)
        GroupByType._field_type = GroupByFieldType  # type: ignore[attr-defined]
        GroupByType.__orm_model__ = model  # type: ignore[attr-defined]
        if object_type is not None:
            GroupByType._object_type = object_type  # type: ignore[attr-defined]
            GroupByType._relation_models = relation_models  # type: ignore[attr-defined]
        self._cover_shown_fields(model, GroupByType)

        self._group_registry[model] = GroupByType
        return GroupByType

    def _group_object_arm(
        self, model: type, fields_meta: Any, include: Any, exclude: Any
    ) -> tuple[type | None, dict[str, type]]:
        """The ``object:`` input reaching each related model that can be grouped.

        As with ordering, a relation is offered once its model has a group-by
        type of its own, which is what it is grouped through.
        """
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        relation_models: dict[str, type] = {}
        for fname, _ftype, is_relation, rel_model in fields_meta:
            if not is_relation or rel_model is None:
                continue
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if self._exclude_generated_sensitive_field(fname, include):
                continue
            rel_group = self._group_registry.get(rel_model)
            if rel_group is None:
                continue
            annotations[fname] = rel_group | None
            defaults[fname] = strawberry.UNSET
            relation_models[fname] = rel_model
        if not annotations:
            return None, {}
        object_cls = type(
            f"{model.__name__}GroupByObject",
            (),
            {"__annotations__": annotations, **defaults},
        )
        return strawberry.input(object_cls, one_of=True), relation_models

    def relation_hop(self, model: type, relation: str) -> Any:
        """The ``GroupHop`` across *model*'s *relation*: its target, and whether
        a row can have many of them."""
        raise NotImplementedError(
            f"{type(self).__name__} cannot group through relations"
        )

    def filter_crosses_to_many(self, filter_input: Any, model: type | None) -> bool:
        """True when *filter_input* reaches across a relation a row has many of.

        Filtered through a join, such a relation repeats a row once for every
        related row that matches.
        """
        if model is None or filter_input is None or filter_input is strawberry.UNSET:
            return False
        for key in filter_input.__class__.__dataclass_fields__:
            value = getattr(filter_input, key)
            if value is strawberry.UNSET or value is None:
                continue
            if key in ("all", "any", "one_of"):
                if any(self.filter_crosses_to_many(entry, model) for entry in value):
                    return True
            elif key == "not_":
                if self.filter_crosses_to_many(value, model):
                    return True
            elif key == "object":
                for relation in value.__class__.__dataclass_fields__:
                    nested = getattr(value, relation)
                    if nested is strawberry.UNSET or nested is None:
                        continue
                    hop = self.relation_hop(model, relation)
                    if hop.many or self.filter_crosses_to_many(nested, hop.target):
                        return True
        return False

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

        relation_names = {
            fname
            for fname, ann in user_annotations.items()
            if ann is strawberry.auto
            and fname not in col_types
            and (not include or fname in include)
        }
        object_type, relation_models = (
            self._group_object_arm(model, fields_meta, relation_names, exclude)
            if relation_names
            else (None, {})
        )
        if object_type is not None:
            group_ns["__annotations__"]["object"] = object_type | None
            group_ns["object"] = strawberry.UNSET

        for cname, cann in custom_group_annotations.items():
            group_ns["__annotations__"][cname] = cann
            group_ns[cname] = custom_group_defaults[cname]

        GroupByCls = type(group_type_name, (), group_ns)
        GroupByType = strawberry.input(GroupByCls, one_of=True)
        GroupByType.__orm_model__ = model  # type: ignore[attr-defined]
        if GroupByFieldType is not None:
            GroupByType._field_type = GroupByFieldType  # type: ignore[attr-defined]
        if object_type is not None:
            GroupByType._object_type = object_type  # type: ignore[attr-defined]
            GroupByType._relation_models = relation_models  # type: ignore[attr-defined]
        if custom_groups:
            GroupByType._custom_groups = custom_groups  # type: ignore[attr-defined]
        self._cover_shown_fields(model, GroupByType)

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
        self,
        model: type,
        aggregate_cls: type | None = None,
        group_type: Any = None,
    ) -> AggregateMeta:
        """Build the aggregate output types and return an ``AggregateMeta``.

        When *aggregate_cls* is provided (from ``@orm.aggregate_type``),
        only fields annotated with ``auto`` on that class are included in
        the standard sub-aggregates (sum/avg/min/max), and methods
        decorated with ``@aggregate_field`` become additional top-level
        fields on the aggregates type.

        The group key has a string for each column, a nested key for each
        relation *group_type* reaches through ``object:``, and a string for
        each of its ``@group_field`` methods.

        Cached per ``(model, aggregate_cls, group_type)`` so repeated calls
        return the same types.
        """
        import datetime as _dt

        cache_key = (model, aggregate_cls, group_type)
        cached = self._aggregate_type_cache.get(cache_key)
        if cached is not None:
            return cached
        if group_type is not None:
            # The aggregates do not depend on how rows are grouped, and two
            # types of one name cannot share a schema.
            plain = self._build_aggregate_types(model, aggregate_cls)
            key_type, relation_keys = self._group_key_type(
                model, [fname for fname, _ftype in plain.groupable_fields], group_type
            )
            meta = dataclasses.replace(
                plain, group_key_type=key_type, relation_keys=relation_keys
            )
            self._cover_shown_fields(model, meta)
            self._aggregate_type_cache[cache_key] = meta
            return meta

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

        GroupKeyType, relation_keys = self._group_key_type(
            model, [fname for fname, _ftype in groupable_fields], group_type
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
            relation_keys=relation_keys,
        )
        self._cover_shown_fields(model, meta)
        self._aggregate_type_cache[cache_key] = meta
        return meta

    def _group_key_type(
        self, model: type, columns: list[str], group_type: Any
    ) -> tuple[type, dict[str, AggregateMeta]]:
        """The ``GroupKey`` type for *group_type*, and the metadata behind each
        relation it nests.

        Shared by every connection keyed the same way: a related model's key
        reached through ``object:`` is the one its own connection reports, and
        two types of one name cannot share a schema.
        """
        cache_key = (model, tuple(columns), group_type)
        cached = self._group_key_cache.get(cache_key)
        if cached is not None:
            return cached
        key_ann: dict[str, Any] = {}
        key_defs: dict[str, Any] = {}
        for fname in columns:
            key_ann[fname] = str | None
            key_defs[fname] = None
        relation_keys: dict[str, AggregateMeta] = {}
        object_type = getattr(group_type, "_object_type", None)
        for relation, rel_model in getattr(group_type, "_relation_models", {}).items():
            nested_group = typing.get_args(object_type.__annotations__[relation])[0]
            relation_keys[relation] = self._build_aggregate_types(
                rel_model, None, nested_group
            )
            key_ann[relation] = relation_keys[relation].group_key_type | None
            key_defs[relation] = None
        custom_keys = set(getattr(group_type, "_custom_groups", {}))
        for name in custom_keys:
            key_ann[name] = str | None
            key_defs[name] = None
        GroupKeyType = strawberry.type(
            type(
                f"{model.__name__}GroupKey",
                (),
                {"__annotations__": key_ann, **key_defs},
            )
        )
        # A @group_field is the developer's own expression, not a column the
        # types might hide, so the cut keeps it.
        GroupKeyType._orm_uncut = frozenset(custom_keys)  # type: ignore[attr-defined]
        self._group_key_cache[cache_key] = (GroupKeyType, relation_keys)
        return GroupKeyType, relation_keys

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

    def query_model(self, query: Any) -> type | None:
        """Model a query object reads, or ``None`` if this backend cannot tell."""
        return None

    def apply_page(self, query: Any, offset: int, limit: int) -> Any | None:
        """Cut *query* to one page in SQL, or ``None`` to have it read whole."""
        return None

    def apply_type_scope(self, query: Any, model: type | None, info: Any) -> Any:
        """Apply *model*'s ``scope_rows`` to *query*, if one is registered.

        A query is scoped at most once. The optimizer and a resolver can both
        call this; the second call is a no-op so a scope that joins is not
        applied twice.
        """
        if model is None:
            return query
        from strawberry_orm.lazy_resolution import mark_query_scoped, query_is_scoped

        if query_is_scoped(query):
            return query
        get_qs = self._type_querysets.get(model)
        if get_qs is None:
            return query
        scoped = get_qs(query, info)
        mark_query_scoped(scoped)
        return scoped

    def filter_scoped_pks(self, model: type, pks: list[Any], info: Any) -> Any:
        """Return the subset of *pks* ``scope_rows`` still allows.

        May be a set or an awaitable of a set. Override per backend.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot re-check scope_rows against loaded rows"
        )

    def scope_instances(self, rows: list[Any], info: Any) -> Any:
        """Drop instances whose model ``scope_rows`` rejects.

        Returns a list, or an awaitable of a list on async backends. Instances
        of models with no ``scope_rows`` pass through. A scoped model whose
        instance has no primary key cannot be checked, so that raises rather
        than returning the row.
        """
        from inspect import isawaitable

        from strawberry_orm.lazy_resolution import mark_rows_scoped, row_is_scoped

        groups: dict[type, list[Any]] = {}
        for row in rows:
            if row_is_scoped(row):
                continue
            model = type(row)
            if model in self._type_querysets:
                groups.setdefault(model, []).append(row)
        if not groups:
            return list(rows)

        pending: list[tuple[type, Any]] = []
        for model, group in groups.items():
            pks: list[Any] = []
            for row in group:
                pk = self.instance_pk(row)
                if pk is None:
                    raise RuntimeError(
                        f"{model.__name__} is row-scoped by scope_rows, but a "
                        f"resolver returned a {model.__name__} with no primary "
                        f"key, so the scope could not be checked."
                    )
                pks.append(pk)
            pending.append((model, self.filter_scoped_pks(model, pks, info)))

        def finish(allowed: dict[type, set[Any]]) -> list[Any]:
            kept = [
                row
                for row in rows
                if row_is_scoped(row)
                or type(row) not in allowed
                or self.instance_pk(row) in allowed[type(row)]
            ]
            mark_rows_scoped(kept)
            return kept

        if any(isawaitable(pks) for _model, pks in pending):

            async def _await_allowed() -> list[Any]:
                allowed: dict[type, set[Any]] = {}
                for model, pks in pending:
                    allowed[model] = await pks if isawaitable(pks) else pks
                return finish(allowed)

            return _await_allowed()

        return finish({model: pks for model, pks in pending})

    # -- Scoping across relation traversal -----------------------------------

    def _field_scope(self, model: type, relation: str, info: Any = None) -> Any | None:
        """The ``scope=`` declared on *model*'s own ``relation`` field, if any."""
        type_name = self._resolved_type_name(model, info)
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
        field_scope = self._field_scope(model, relation, info)
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
            from strawberry_orm.lazy_resolution import mark_query_scoped

            if get_qs is not None:
                query = get_qs(query, resolver_info)
            if field_scope is not None:
                query = call_scope(field_scope, query, resolver_info)
            # The query is scoped, so a later apply_type_scope must not apply
            # scope_rows a second time.
            mark_query_scoped(query)
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
        nested = getattr(input_cls, "__annotations__", {}).get("field")
        args = getattr(nested, "__args__", None)
        inner = args[0] if args else nested
        return set(getattr(inner, "__annotations__", {}))

    def _cover_shown_fields(self, model: type, item: Any) -> Any:
        """Track *item*, so every schema cuts it to what its types show there.

        The inputs are built from the model, which knows every column, while
        the types decide which of them a caller may read. A hidden column left
        in a filter is read one ``startsWith`` probe at a time, and through a
        group key or ``min``/``max`` it is read outright.
        """
        tracked = self._model_inputs.setdefault(model, [])
        if all(existing is not item for existing in tracked):
            tracked.append(item)
        return item

    def _attach_to_type(self, model: type, item: Any, graphql_type: type) -> None:
        """Record that *item* is the input *graphql_type* uses."""
        owners = getattr(item, "_orm_owners", None)
        if owners is None:
            owners = item._orm_owners = []
        if graphql_type not in owners:
            owners.append(graphql_type)
        self._cover_shown_fields(model, item)

    def _show_fields(
        self,
        model: type,
        graphql_type: type,
        shown: dict[str, frozenset[str] | None],
    ) -> None:
        """Record the columns and relations a type of *model* shows as they are."""
        self._shown_by_type[graphql_type] = shown
        self._types_by_model.setdefault(model, []).append(graphql_type)

    def _shown(self, graphql_type: type) -> dict[str, frozenset[str] | None]:
        """What *graphql_type* shows, with the column a Relay node serves as its id.

        The id is encoded, not hidden, so its column counts as shown.
        """
        from strawberry_orm.tags import tags_of

        shown = self._shown_by_type[graphql_type]
        id_attr = _node_id_attr(graphql_type)
        reads_as_is = vars(graphql_type).get("_orm_reads_as_is", frozenset())
        if id_attr is None or id_attr in shown or id_attr not in reads_as_is:
            return shown
        definition = graphql_type.__strawberry_definition__  # type: ignore[attr-defined]
        global_id = next(f for f in definition.fields if f.graphql_name == "id")
        return {**shown, id_attr: tags_of(global_id)}

    def input_cuts(
        self, reached: set[Any], schema_tags: frozenset[str] | None = None
    ) -> dict[Any, Any]:
        """How a schema reaching *reached* cuts the generated inputs it reaches.

        Each input covers what the types it serves in that schema show: the
        types it is attached to, and the types its relations lead to through
        ``object:`` from the types its parent serves. One serving several types
        covers what all of them show. An input nothing attaches, such as one
        passed to a field directly, covers what every type of its model the
        schema reaches shows.
        """
        from strawberry_orm.cover import InputCut
        from strawberry_orm.tags import tags_of

        items = [
            (model, item)
            for model, inputs in self._model_inputs.items()
            for item in inputs
            if _reached_input(item, reached)
        ]
        uses = self._input_uses(items, reached)
        part_covers: dict[Any, list[dict[str, frozenset[str] | None]]] = {}
        holders: dict[Any, list[tuple[Any, str | None]]] = {}
        messages: dict[Any, str] = {}
        for model, item in items:
            if not uses[id(item)]:
                continue
            cover = _intersect([self._shown(node) for node in uses[id(item)]])
            for label, part, container in _input_parts(item):
                part_covers.setdefault(part, []).append(cover)
                holders.setdefault(part, []).append((container, label))
                if container is None:
                    messages[part] = (
                        f"{model.__name__} cannot be grouped: the GraphQL types "
                        f"its connection serves show none of its columns to "
                        f"group by."
                    )
                elif container is item:
                    messages[container] = (
                        f"{item.__strawberry_definition__.name} is left with "
                        f"nothing to use: the GraphQL types it serves show none "
                        f"of {model.__name__}'s columns. Show one on the type, "
                        f"or drop the input from it."
                    )

        cuts: dict[Any, Any] = {}
        dropped: dict[Any, set[str]] = {}
        for part, covers in part_covers.items():
            cover = _intersect(covers)
            fields = part.__strawberry_definition__.fields
            uncut = getattr(part, "_orm_uncut", frozenset())
            keep = frozenset(
                field.python_name
                for field in fields
                if field.python_name in uncut
                or (
                    field.python_name in cover
                    and _in_schema(
                        _common_tags(tags_of(field), cover[field.python_name]),
                        schema_tags,
                    )
                )
            )
            if len(keep) == len(fields):
                continue
            cuts[part] = InputCut(keep, messages.get(part))
            if not keep:
                for container, label in holders[part]:
                    if container is not None:
                        dropped.setdefault(container, set()).add(label)
        for container, labels in dropped.items():
            names = {f.python_name for f in container.__strawberry_definition__.fields}
            cuts[container] = InputCut(
                frozenset(names - labels), messages.get(container)
            )
        return cuts

    def _input_uses(
        self, items: list[tuple[type, Any]], reached: set[Any]
    ) -> dict[int, list[type]]:
        """The types each input serves in a schema that reaches *reached*."""
        uses = {
            id(item): [o for o in getattr(item, "_orm_owners", ()) if o in reached]
            for _, item in items
        }
        self._follow_relations(items, uses, reached)
        for model, item in items:
            if not uses[id(item)]:
                types = self._types_by_model.get(model, [])
                uses[id(item)] = [t for t in types if t in reached] or list(types)
        self._follow_relations(items, uses, reached)
        return uses

    def _follow_relations(
        self,
        items: list[tuple[type, Any]],
        uses: dict[int, list[type]],
        reached: set[Any],
    ) -> None:
        """Add, to each input behind ``object:``, the types its relation leads to.

        A parent serving ``PostNode`` reaches ``author`` as whatever type
        ``PostNode.author`` returns, so the input behind it serves that type.
        """
        from strawberry_orm.cover import named_type

        inputs = {item for _, item in items if isinstance(item, type)}
        changed = True
        while changed:
            changed = False
            for _, parent in items:
                relations = getattr(parent, "_object_type", None)
                if relations is None or relations not in reached:
                    continue
                for field in relations.__strawberry_definition__.fields:
                    child = named_type(field.type)
                    if child not in inputs:
                        continue
                    for node in list(uses[id(parent)]):
                        target = self._relation_target(node, field.python_name)
                        if target in reached and target not in uses[id(child)]:
                            uses[id(child)].append(target)
                            changed = True

    def _relation_target(self, graphql_type: type, relation: str) -> type | None:
        """The type *graphql_type* shows *relation* as, if it shows it as it is."""
        from strawberry_orm.cover import named_type

        if relation not in self._shown_by_type.get(graphql_type, {}):
            return None
        definition = graphql_type.__strawberry_definition__  # type: ignore[attr-defined]
        field = next(f for f in definition.fields if f.python_name == relation)
        target = named_type(field.type)
        return target if target in self._shown_by_type else None

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
        """Reject ``using=`` paths that can never resolve, at schema-build time."""
        if not self._strict_hints or not hints.using:
            return

        for rel in hints.using:
            current: type | None = model
            for hop in hint_hops(rel):
                if current is None:
                    # The far side is not known yet (Tortoise before init), so
                    # the rest of the path cannot be checked.
                    break
                names = self.relation_names(current)
                if hop not in names:
                    where = f"{type_name}.{field_name}: "
                    path = f" (in using={rel!r})" if hop != rel else ""
                    suggestion = get_close_matches(hop, sorted(names), n=1)
                    hint_text = (
                        f" Did you mean {suggestion[0]!r}?" if suggestion else ""
                    )
                    raise ValueError(
                        f"{where}{current.__name__} has no relation {hop!r}"
                        f"{path}.{hint_text}"
                    )
                current = self._relation_target_model(current, hop)

    def _type_name_for_model(self, model: type) -> str | None:
        for type_name, m in self._type_registry.items():
            if m is model:
                return type_name
        return None

    # One model can be registered as several GraphQL types, each with its own
    # field hints, so ``_type_name_for_model`` - which can only ever return the
    # first registration - must not decide which hints apply. The three lookups
    # below read the type out of the request instead, and fall back to the
    # registration-order name only when the request does not carry one (a test
    # double without a schema, a wrapper referencing two types of one model).

    def _selection_type_name(self, model: type, info: Any) -> str | None:
        """Name of *model*'s type whose selection the optimizer is walking."""
        from strawberry_orm.optimizer.selections import (
            graphql_resolve_info,
            registered_type_name,
        )

        return_type = getattr(graphql_resolve_info(info), "return_type", None)
        name = registered_type_name(return_type, model, self._type_registry)
        return name if name is not None else self._type_name_for_model(model)

    def _child_type_name(
        self,
        parent_type_name: str | None,
        field_name: str,
        model: type,
        info: Any,
    ) -> str | None:
        """Name of *model*'s type that *field_name* under *parent_type_name* yields."""
        from strawberry_orm.optimizer.selections import (
            child_field_type,
            graphql_resolve_info,
            registered_type_name,
        )

        schema = getattr(graphql_resolve_info(info), "schema", None)
        field_type = child_field_type(schema, parent_type_name, field_name)
        name = registered_type_name(field_type, model, self._type_registry)
        return name if name is not None else self._type_name_for_model(model)

    def _resolved_type_name(self, model: type, info: Any) -> str | None:
        """Name of *model*'s type that the current field is being resolved on."""
        from strawberry_orm.optimizer.selections import graphql_resolve_info

        parent_type = (
            getattr(graphql_resolve_info(info), "parent_type", None)
            if info is not None
            else None
        )
        name = getattr(parent_type, "name", None)
        if name is not None and self._type_registry.get(name) is model:
            return name
        return self._type_name_for_model(model)

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
        # Read before the backend installs resolvers of its own, which would
        # make every relation look like a field with a custom resolver. Own
        # annotations alone miss columns an interface or mixin declares.
        excluded = set(exclude) if exclude else set()
        cls._orm_reads_as_is = frozenset(  # type: ignore[attr-defined]
            name for name in annotations if _reads_as_is(vars(cls).get(name))
        ) | _inherited_reads_as_is(cls, excluded=excluded)

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

        # Metadata of the placeholders removed below, so the backend builds
        # the field as usual; _finalize_type puts it back on that field.
        removed_metadata: dict[str, Any] = {}
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
                            metadata=val.metadata,
                        ),
                    )
                else:
                    if val.metadata:
                        removed_metadata[attr_name] = val.metadata
                    delattr(cls, attr_name)
            elif _metadata_only_field(val) and attr_name in annotations:
                removed_metadata[attr_name] = dict(val.metadata)
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
                if getattr(val, "metadata", None):
                    removed_metadata[attr_name] = dict(val.metadata)
                delattr(cls, attr_name)

        cls._orm_removed_metadata = removed_metadata  # type: ignore[attr-defined]
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
        from strawberry_orm.tags import tags_of

        result = strawberry.type(cls, name=name if name else None)
        removed = vars(cls).get("_orm_removed_metadata")
        if removed:
            for field in result.__strawberry_definition__.fields:
                if field.python_name in removed:
                    field.metadata = MappingProxyType(
                        {**field.metadata, **removed[field.python_name]}
                    )
        reads_as_is = vars(cls).get("_orm_reads_as_is", frozenset())
        shown = {
            field.python_name: tags_of(field)
            for field in result.__strawberry_definition__.fields
            if field.python_name in reads_as_is
        }
        self._show_fields(model, result, shown)
        for attr in ("__orm_filter__", "__orm_order__", "__orm_group__"):
            attached = vars(cls).get(attr)
            if (
                attached is not None
                and getattr(attached, "__orm_model__", None) is model
            ):
                self._attach_to_type(model, attached, result)
        self._type_registry[type_name] = model
        self._graphql_type_registry[model] = result
        self._install_on_resolvers(result, model, type_name)
        return result
