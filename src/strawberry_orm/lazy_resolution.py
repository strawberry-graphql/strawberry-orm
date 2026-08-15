"""Runtime guardrails for unoptimized (lazy) ORM relation loads."""

from __future__ import annotations

import logging
import re
import warnings
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from strawberry.extensions import SchemaExtension

if TYPE_CHECKING:
    from strawberry_orm.backends.protocol import Backend

logger = logging.getLogger("strawberry_orm.lazy_query")


def extensions_include_lazy_resolution(extensions: list[Any]) -> bool:
    """Return True if *extensions* already contains a lazy-resolution extension."""
    for ext in extensions:
        if isinstance(ext, type) and issubclass(ext, LazyResolutionExtension):
            return True
        if getattr(ext, "__name__", "").startswith("LazyResolutionExtension_"):
            return True
    return False


def _field_name_from_info(info: Any) -> str | None:
    field_name = getattr(info, "python_name", None) or getattr(info, "field_name", None)
    if not field_name:
        return None
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", field_name).lower()


def _graphql_field_name(info: Any) -> str:
    return str(
        getattr(info, "field_name", None) or getattr(info, "python_name", None) or "?"
    )


def _path_field_names(info: Any) -> list[str]:
    path = getattr(info, "path", None)
    if path is None:
        field = _graphql_field_name(info)
        return [field] if field != "?" else []

    keys: list[str] = []
    node = path
    while node is not None:
        key = node.key
        if not isinstance(key, int):
            keys.append(str(key))
        node = node.prev
    keys.reverse()
    return keys


def _graphql_path_from_info(info: Any) -> str:
    keys = _path_field_names(info)
    return ".".join(keys) if keys else _graphql_field_name(info)


def _format_selection_set(selection_set: Any) -> str:
    if selection_set is None:
        return ""

    parts: list[str] = []
    for selection in selection_set.selections:
        name_node = getattr(selection, "name", None)
        if name_node is None:
            continue
        name = name_node.value
        nested = getattr(selection, "selection_set", None)
        if nested is not None and nested.selections:
            parts.append(f"{name} {{ {_format_selection_set(nested)} }}")
        else:
            parts.append(name)
    return " ".join(parts)


def _query_selection_path(info: Any) -> str:
    """Return the GraphQL selection path that requested this relation."""
    operation = getattr(info, "operation", None)
    path_fields = _path_field_names(info)
    if operation is None or not path_fields:
        return f"{{ {_graphql_path_from_info(info)} }}"

    def walk(selection_set: Any, remaining: list[str]) -> str | None:
        target = remaining[0]
        for selection in selection_set.selections:
            name_node = getattr(selection, "name", None)
            if name_node is None or name_node.value != target:
                continue

            nested = getattr(selection, "selection_set", None)
            if len(remaining) == 1:
                child = _format_selection_set(nested) if nested is not None else ""
                return f"{target} {{ {child} }}" if child else target

            if nested is None:
                return None
            inner = walk(nested, remaining[1:])
            if inner is None:
                return None
            return f"{target} {{ {inner} }}"

        return None

    inner = walk(operation.selection_set, path_fields)
    if inner is None:
        return f"{{ {_graphql_path_from_info(info)} }}"

    op_type = getattr(getattr(operation, "operation", None), "value", None)
    op_name = getattr(getattr(operation, "name", None), "value", None)
    if op_name:
        prefix = f"{op_type} {op_name}" if op_type else op_name
        return f"{prefix} {{ {inner} }}"
    if op_type:
        return f"{op_type} {{ {inner} }}"
    return f"{{ {inner} }}"


def _parent_graphql_type(info: Any) -> str:
    parent = getattr(info, "parent_type", None)
    if parent is None:
        return "?"
    return str(getattr(parent, "name", parent))


def _relation_hint(backend: Backend, instance: Any = None, field_name: str = "") -> str:
    expected = {
        "DjangoBackend": "QuerySet",
        "SQLAlchemyBackend": "Select",
        "TortoiseBackend": "QuerySet",
    }.get(backend.__class__.__name__, "queryset")
    return f"return a {expected} instead of list"


@dataclass(frozen=True)
class _UnoptimizedLoad:
    orm_field: str
    model_name: str
    query_path: str
    resolver_type: str
    graphql_field: str
    hint: str
    path: str = ""


@dataclass(frozen=True)
class _ResolverQueries:
    """A resolver that issued its own queries instead of reading loaded data."""

    resolver_type: str
    graphql_field: str
    model_name: str
    relations: tuple[str, ...]
    count: int
    rows: int = 1


def _django_relation_prefetched(instance: Any, field_name: str) -> bool | None:
    try:
        field = instance._meta.get_field(field_name)  # type: ignore[attr-defined]
    except Exception:
        return None

    if not getattr(field, "is_relation", False):
        return None

    if getattr(field, "many_to_one", False) or getattr(field, "one_to_one", False):
        is_cached = getattr(field, "is_cached", None)
        if callable(is_cached):
            return is_cached(instance)
        return True

    prefetched = getattr(instance, "_prefetched_objects_cache", None)
    return isinstance(prefetched, dict) and field_name in prefetched


def _sqlalchemy_relation_prefetched(instance: Any, field_name: str) -> bool | None:
    from sqlalchemy import inspect as sa_inspect

    try:
        state = sa_inspect(instance)
    except Exception:
        return None

    if field_name not in state.mapper.relationships:
        return None

    return field_name not in state.unloaded


def _tortoise_relation_prefetched(instance: Any, field_name: str) -> bool | None:
    meta = getattr(instance, "_meta", None)
    if meta is None:
        return None

    fields_map = getattr(meta, "fields_map", None)
    if not isinstance(fields_map, dict) or field_name not in fields_map:
        return None

    field = fields_map[field_name]
    related_model = getattr(field, "related_model", None)
    if related_model is None:
        return None

    fetched = getattr(instance, "_fetched", None)
    if isinstance(fetched, (set, frozenset)) and field_name in fetched:
        return True
    if isinstance(fetched, dict) and field_name in fetched:
        return True

    field_cls = type(field).__name__

    try:
        rel_value = getattr(instance, field_name)
    except Exception:
        return False

    if isinstance(rel_value, list) and field_cls in (
        "BackwardFKRelation",
        "BackwardOneToOneRelation",
        "ManyToManyFieldInstance",
    ):
        return True

    if getattr(rel_value, "_fetched", False):
        return True

    return isinstance(rel_value, related_model)


class QueryProbe:
    """Counts SQL statements issued while a resolver runs."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


def unloaded_relations(backend: Backend, instance: Any) -> set[str]:
    """Return the relations of *instance* that are not loaded yet."""
    try:
        names = backend.relation_names(type(instance))
    except Exception:
        return set()
    return {
        name
        for name in names
        if relation_is_prefetched(backend, instance, name) is False
    }


def relation_is_prefetched(
    backend: Backend, instance: Any, field_name: str
) -> bool | None:
    """Return prefetch state for an ORM relation, or None if not applicable."""
    backend_name = backend.__class__.__name__
    if backend_name == "DjangoBackend":
        return _django_relation_prefetched(instance, field_name)
    if backend_name == "SQLAlchemyBackend":
        return _sqlalchemy_relation_prefetched(instance, field_name)
    if backend_name == "TortoiseBackend":
        return _tortoise_relation_prefetched(instance, field_name)
    return None


def _format_cause(ancestor: str, loads: list[_UnoptimizedLoad]) -> str:
    """Describe the field that materialized rows and ended optimization."""
    relations = sorted({f"{load.model_name}.{load.orm_field}" for load in loads})
    return (
        f"  cause: {ancestor} returned rows instead of a query object, so "
        f"{len(relations)} relation(s) below it could not be eager-loaded\n"
        f"    fix: return an unexecuted query from {ancestor} "
        f"(loads: {', '.join(relations)})"
    )


def _format_resolver_queries(entry: _ResolverQueries) -> str:
    """Describe a resolver that read a relation nobody eager-loaded."""
    where = f"{entry.resolver_type}.{entry.graphql_field}"
    read = ", ".join(f"{entry.model_name}.{rel}" for rel in entry.relations)
    names = ", ".join(f'"{rel}"' for rel in entry.relations)
    if entry.count:
        detail = (
            f"{where} issued {entry.count} query(s) across "
            f"{entry.rows} row(s) reading {read}"
        )
    else:
        detail = f"{where} lazy-loaded {read} on {entry.rows} row(s)"
    return f"  - resolver: {detail}\n    fix: @orm.field.computed(using=[{names}])"


def _format_unoptimized_loads(
    loads: list[_UnoptimizedLoad],
    resolver_queries: list[_ResolverQueries] | None = None,
    causes: dict[str, list[_UnoptimizedLoad]] | None = None,
) -> str:
    grouped: Counter[_UnoptimizedLoad] = Counter(loads)
    lines = []

    for ancestor in sorted(causes or {}):
        lines.append(_format_cause(ancestor, (causes or {})[ancestor]))

    for load, count in sorted(
        grouped.items(),
        key=lambda item: (item[0].query_path, item[0].resolver_type, item[0].orm_field),
    ):
        suffix = f" ({count} instances)" if count > 1 else ""
        lines.append(
            f"  - path: {load.query_path}\n"
            f"    resolver: {load.resolver_type}.{load.graphql_field} "
            f"(ORM: {load.model_name}.{load.orm_field}){suffix}\n"
            f"    fix: {load.hint}"
        )

    merged_resolvers: dict[tuple[str, str], _ResolverQueries] = {}
    for entry in resolver_queries or []:
        key = (entry.resolver_type, entry.graphql_field)
        previous = merged_resolvers.get(key)
        if previous is None:
            merged_resolvers[key] = entry
            continue
        merged_resolvers[key] = _ResolverQueries(
            resolver_type=entry.resolver_type,
            graphql_field=entry.graphql_field,
            model_name=entry.model_name,
            relations=tuple(sorted(set(previous.relations) | set(entry.relations))),
            count=previous.count + entry.count,
            rows=previous.rows + entry.rows,
        )

    for key in sorted(merged_resolvers):
        lines.append(_format_resolver_queries(merged_resolvers[key]))

    total = len(loads) + sum(
        max(entry.count, entry.rows) for entry in merged_resolvers.values()
    )
    unique = len(grouped) + len(merged_resolvers)
    header = (
        f"Unoptimized relation loads detected ({total} total, {unique} unique) "
        f"— may waterfall (N+1):"
    )
    return f"{header}\n" + "\n".join(lines)


class LazyResolutionExtension(SchemaExtension):
    """Log or raise when a relation resolver loads without optimizer prefetch."""

    _backend: Backend | None = None
    _mode: str = "warn"

    def __init__(self, *, execution_context: Any | None = None) -> None:
        self.execution_context = execution_context  # type: ignore[assignment]
        self._loads: list[_UnoptimizedLoad] = []
        self._resolver_queries: list[_ResolverQueries] = []
        self._shapes: dict[str, bool] = {}
        self._shape_labels: dict[str, str] = {}

    @classmethod
    def configure(
        cls,
        backend: Backend,
        *,
        mode: str = "warn",
    ) -> type[LazyResolutionExtension]:
        return type(
            f"{cls.__name__}_{backend.__class__.__name__}",
            (cls,),
            {"_backend": backend, "_mode": mode},
        )

    def on_operation(self) -> Iterator[None]:
        self._loads = []
        self._resolver_queries = []
        self._shapes = {}
        self._shape_labels = {}
        yield
        self._flush_loads()

    def resolve(
        self, _next: Any, root: Any, info: Any, *args: Any, **kwargs: Any
    ) -> Any:
        self._record_relation_access(root, info)

        if not self._should_probe(root, info):
            result = _next(root, info, *args, **kwargs)
            if isawaitable(result):
                return self._await_result(result, info)
            self._record_return_shape(info, result)
            return result

        # The probe has to stay open across an async resolver's await, so the
        # context manager is driven by hand rather than with a ``with`` block.
        before = unloaded_relations(self._backend, root)
        probe_cm = self._backend.query_probe(info)
        probe = probe_cm.__enter__()
        try:
            result = _next(root, info, *args, **kwargs)
        except BaseException:
            probe_cm.__exit__(None, None, None)
            raise

        if isawaitable(result):
            return self._await_probed_result(
                probe_cm, probe, result, root, info, before
            )

        probe_cm.__exit__(None, None, None)
        self._record_resolver_queries(root, info, probe, before)
        self._record_return_shape(info, result)
        return result

    async def _await_result(self, awaitable: Any, info: Any) -> Any:
        result = await awaitable
        self._record_return_shape(info, result)
        return result

    async def _await_probed_result(
        self,
        probe_cm: Any,
        probe: Any,
        awaitable: Any,
        root: Any,
        info: Any,
        before: set[str],
    ) -> Any:
        try:
            result = await awaitable
        finally:
            probe_cm.__exit__(None, None, None)
        self._record_resolver_queries(root, info, probe, before)
        self._record_return_shape(info, result)
        return result

    def _should_probe(self, root: Any, info: Any) -> bool:
        """Probe resolvers on ORM types whose field is not itself a relation.

        Relation fields are already covered by :meth:`_record_relation_access`;
        what this catches is a computed field whose body reads a relation.
        """
        if self._mode == "off" or self._backend is None or root is None:
            return False
        field_name = _field_name_from_info(info)
        if not field_name:
            return False
        if relation_is_prefetched(self._backend, root, field_name) is not None:
            return False
        return self._backend._type_name_for_model(type(root)) is not None

    def _record_return_shape(self, info: Any, result: Any) -> None:
        """Remember whether each field handed back a still-optimizable query."""
        if self._mode == "off" or self._backend is None:
            return
        path = ".".join(_path_field_names(info))
        if not path or path in self._shapes:
            return
        if isawaitable(result):
            return
        self._shapes[path] = self._backend.is_query_object(result)
        self._shape_labels[path] = (
            f"{_parent_graphql_type(info)}.{_graphql_field_name(info)}"
        )

    def _materializing_ancestor(self, path: str) -> str | None:
        """Nearest ancestor of *path* that returned rows instead of a query."""
        parts = path.split(".")
        for depth in range(len(parts) - 1, 0, -1):
            ancestor = ".".join(parts[:depth])
            if self._shapes.get(ancestor) is False:
                return ancestor
        return None

    def _record_resolver_queries(
        self,
        root: Any,
        info: Any,
        probe: Any,
        before: set[str],
    ) -> None:
        # Only a relation that flipped from unloaded to loaded during this
        # resolver is safely attributable; sibling fields resolve concurrently,
        # so the raw statement count alone would over-attribute.
        newly_loaded = tuple(sorted(before - unloaded_relations(self._backend, root)))
        if not newly_loaded:
            return
        self._resolver_queries.append(
            _ResolverQueries(
                resolver_type=_parent_graphql_type(info),
                graphql_field=_graphql_field_name(info),
                model_name=type(root).__name__,
                relations=newly_loaded,
                count=probe.count,
            )
        )

    def _record_relation_access(self, root: Any, info: Any) -> None:
        if self._mode == "off" or self._backend is None or root is None:
            return

        field_name = _field_name_from_info(info)
        if not field_name:
            return

        prefetched = relation_is_prefetched(self._backend, root, field_name)
        if prefetched is None or prefetched:
            return

        model_name = type(root).__name__
        self._loads.append(
            _UnoptimizedLoad(
                orm_field=field_name,
                model_name=model_name,
                query_path=_query_selection_path(info),
                resolver_type=_parent_graphql_type(info),
                graphql_field=_graphql_field_name(info),
                hint=_relation_hint(self._backend, root, field_name),
                path=".".join(_path_field_names(info)),
            )
        )

    def _flush_loads(self) -> None:
        if (not self._loads and not self._resolver_queries) or self._mode == "off":
            return

        causes: dict[str, list[_UnoptimizedLoad]] = {}
        for load in self._loads:
            ancestor = self._materializing_ancestor(load.path)
            if ancestor is not None:
                label = self._shape_labels.get(ancestor, ancestor)
                causes.setdefault(label, []).append(load)

        message = _format_unoptimized_loads(self._loads, self._resolver_queries, causes)
        logger.warning(message)
        if self._mode == "error":
            raise RuntimeError(message)
        if self._mode == "warn":
            warnings.warn(message, UserWarning, stacklevel=2)
