"""Runtime guardrails for unoptimized (lazy) ORM relation loads."""

from __future__ import annotations

import logging
import re
import threading
import warnings
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from graphql import OperationType
from strawberry.extensions import SchemaExtension

if TYPE_CHECKING:
    from strawberry_orm.backends.protocol import Backend

logger = logging.getLogger("strawberry_orm.lazy_query")

#: Relations this library loaded after applying ``scope_rows``. A cache filled
#: any other way — ``select_related``, ``prefetch_related``, ``joinedload`` —
#: is not in the set and must be re-checked.
_SCOPED_RELATIONS = "_strawberry_orm_scoped_relations"
_ROW_SCOPED = "_strawberry_orm_row_scoped"
_QUERY_SCOPED = "_strawberry_orm_type_scoped"
_SCOPED_QUERIES: WeakKeyDictionary[Any, object] = WeakKeyDictionary()


class _Execution:
    """One execution of a schema, opened by the library's schema extensions.

    The marks below hold only for the execution that made them. Rows, relation
    caches and queries can outlive a request (an identity map shared between
    requests, a model instance or statement kept at module level), and a mark
    from one request's scope says nothing about the next request's.
    """

    __slots__ = ("casts", "connection_states", "filter_costs", "mark", "plans")

    def __init__(self) -> None:
        #: ``strawberry.cast`` marks made in this execution: (row, type) by row id.
        self.casts: dict[int, tuple[Any, Any]] = {}
        self.connection_states: dict[tuple, Any] = {}
        #: What each field's filters cost, as ``filter_budget`` counts it.
        self.filter_costs: dict[tuple, tuple[int, int]] = {}
        self.mark = object()
        #: What the optimizer has planned in this execution, and may still plan.
        self.plans: Any = None


_EXECUTION: ContextVar[_Execution | None] = ContextVar(
    "strawberry_orm_execution", default=None
)
#: Query marks made with no execution open. A query is only marked once
#: scoping built it, so it is not shared the way a row can be.
_OUTSIDE_AN_EXECUTION = object()


@contextmanager
def execution_scope() -> Iterator[None]:
    """Open an execution for the marks and connection states made inside it."""
    token = _EXECUTION.set(_Execution())
    try:
        yield
    finally:
        _EXECUTION.reset(token)


def open_subscription_event(info: Any) -> None:
    """Open an execution for a subscription event, from its root field.

    ``on_execute`` covers only setting a subscription up; each event resolves
    after it, in the task delivering the events. The event's one root field
    opens a fresh execution there, so no mark carries over from the event
    before. Nothing closes it: the next event replaces it.
    """
    operation = getattr(info, "operation", None)
    if getattr(operation, "operation", None) is OperationType.SUBSCRIPTION:
        _EXECUTION.set(_Execution())


def current_execution() -> _Execution | None:
    return _EXECUTION.get()


def execution_mark() -> object | None:
    """What marks made in the open execution hold, or ``None`` outside one."""
    execution = _EXECUTION.get()
    return None if execution is None else execution.mark


#: On a resolver the library builds for a relation field: the relation it
#: reads, scoped. ``functools.wraps`` carries it onto wrappers around it.
_READS_RELATION = "_orm_reads_relation"


def reads_relation[F: Callable[..., Any]](resolver: F, relation: str) -> F:
    """Mark *resolver* as the library's own scoped read of *relation*."""
    setattr(resolver, _READS_RELATION, relation)
    return resolver


def relation_read_by(info: Any) -> str | None:
    """The relation the field *info* resolves reads, if the library resolves it.

    Judged by the resolver, not the field's name: an app's resolver called
    ``posts`` can return any rows at all.
    """
    definition = getattr(info, "_field", None)
    if definition is None:
        fields = getattr(getattr(info, "parent_type", None), "fields", None) or {}
        graphql_field = fields.get(getattr(info, "field_name", None))
        extensions = getattr(graphql_field, "extensions", None) or {}
        definition = extensions.get("strawberry-definition")
    resolver = getattr(getattr(definition, "base_resolver", None), "wrapped_func", None)
    return getattr(resolver, _READS_RELATION, None)


def mark_relations_scoped(
    instances: Iterable[Any],
    names: Iterable[str],
    *,
    scope: Any = None,
    held: Callable[[Any, str], Any] | None = None,
) -> None:
    """Record that *names* on each instance were loaded with row scoping.

    *scope* is the ``scope=`` of the field the load was for, if it had one.
    Two types can show one model and scope one relation differently, and a
    cache holds what one of them allowed.

    *held* reads the value a relation holds. A mark made with it vouches for
    that value only: a row shared through an identity map can have the
    relation replaced by another execution's load.
    """
    mark = execution_mark()
    adding = set(names)
    if mark is None or not adding:
        return
    for instance in instances:
        if instance is None:
            continue
        current = getattr(instance, _SCOPED_RELATIONS, None)
        if not isinstance(current, dict):
            current = {}
            setattr(instance, _SCOPED_RELATIONS, current)
        for name in adding:
            current[name] = (mark, scope, held(instance, name) if held else None)


def relation_cache_is_scoped(
    instance: Any,
    field_name: str,
    *,
    scope: Any = None,
    held: Callable[[Any, str], Any] | None = None,
) -> bool:
    """Whether *field_name*'s loaded value came from a scoped load in this execution.

    The load must have been for a field with the same ``scope=`` as *scope*
    and, given *held*, the relation must still hold the value it loaded.
    """
    mark = execution_mark()
    current = getattr(instance, _SCOPED_RELATIONS, None)
    if mark is None or not isinstance(current, dict):
        return False
    made = current.get(field_name)
    return (
        made is not None
        and made[0] is mark
        and made[1] is scope
        and (held is None or made[2] is held(instance, field_name))
    )


#: On a row: the loads onto it still in flight.
_LOADS_IN_FLIGHT = "_strawberry_orm_loads_in_flight"
_LOADS_LOCK = threading.Lock()


class _Load:
    __slots__ = ("crossed", "names", "thread")

    def __init__(self, names: frozenset[str]) -> None:
        self.crossed = False
        self.names = names
        self.thread = threading.get_ident()

    def crosses(self, other: _Load) -> bool:
        return self.thread != other.thread and bool(self.names & other.names)


@contextmanager
def load_alone(
    rows: Iterable[Any], names: Iterable[str]
) -> Iterator[Callable[[], bool]]:
    """Track a load of relations *names* onto *rows*; yields whether it has had them to itself so far.

    A row the app keeps between requests can be loaded onto by two at once,
    and one load's caches replaced by the other's between its check of what
    they hold and its mark. Its mark would then vouch for rows another scope
    allowed, so a load that crossed another one marks nothing, and the
    resolvers read again. The check and the mark run without awaiting, so
    only a load in another thread, of one of the same relations, can come
    between them: one on the same event loop never crosses it, and two
    resolvers reading one relation again onto a row that parents share would
    otherwise keep each other from ever marking it.
    """
    load = _Load(frozenset(names))
    unique = list({id(row): row for row in rows}.values())
    with _LOADS_LOCK:
        for row in unique:
            loads = getattr(row, _LOADS_IN_FLIGHT, None)
            if loads is None:
                loads = []
                setattr(row, _LOADS_IN_FLIGHT, loads)
            for other in loads:
                if load.crosses(other):
                    other.crossed = load.crossed = True
            loads.append(load)
    try:
        yield lambda: not load.crossed
    finally:
        with _LOADS_LOCK:
            for row in unique:
                loads = getattr(row, _LOADS_IN_FLIGHT)
                loads[:] = [other for other in loads if other is not load]


#: The scope a row mark or check leaves unnamed. A mark that names none vouches
#: for the scope of each row's own class alone; a check that names none takes
#: any mark. Otherwise each names a class or a set of classes, and the mark must
#: name every class the check does.
ANY_SCOPE: Any = object()


def row_mark(owner: Any = ANY_SCOPE) -> tuple[object, Any] | None:
    """A row mark for the open execution: rows that passed *owner*'s ``scope_rows``.

    ``None`` outside an execution, where a mark would hold for every request.
    """
    mark = execution_mark()
    return None if mark is None else (mark, owner)


def row_mark_holds(made: Any, owner: Any = ANY_SCOPE) -> bool:
    """Whether the row mark *made* is this execution's and vouches for *owner*.

    *owner* is the class that scopes the row's own class, or the set of every
    class whose scope the row is held to. A row of a subclass whose type
    defines its own ``scope_rows`` and keeps its rows in a table of its own
    is not scoped by its base's, and one read as its base's type is not
    scoped by its own class's alone, so a mark made by either read does not
    vouch for the other.
    """
    mark = execution_mark()
    if mark is None or not isinstance(made, tuple) or made[0] is not mark:
        return False
    if owner is ANY_SCOPE:
        return True
    owed = _scopes(owner)
    return len(owed) <= 1 if made[1] is ANY_SCOPE else owed <= _scopes(made[1])


def _scopes(owner: Any) -> frozenset[Any]:
    return owner if isinstance(owner, frozenset) else frozenset((owner,))


def mark_rows_scoped(instances: Iterable[Any], owner: Any = ANY_SCOPE) -> None:
    """Record that these instances passed *owner*'s ``scope_rows``.

    Left out, *owner* is the class scoping each row's own class.
    """
    made = row_mark(owner)
    if made is None:
        return
    for instance in instances:
        with suppress(Exception):
            setattr(instance, _ROW_SCOPED, made)


def row_is_scoped(instance: Any, owner: Any = ANY_SCOPE) -> bool:
    """Whether *instance* passed *owner*'s ``scope_rows`` in this execution."""
    return row_mark_holds(getattr(instance, _ROW_SCOPED, None), owner)


def _query_mark() -> object:
    return execution_mark() or _OUTSIDE_AN_EXECUTION


def mark_query_scoped(query: Any, scopes: frozenset[Any] = frozenset()) -> None:
    """Remember that ``scope_rows`` has already been applied to *query*.

    *scopes* are the classes whose ``scope_rows`` were applied.
    """
    made = (_query_mark(), scopes)
    with suppress(Exception):
        setattr(query, _QUERY_SCOPED, made)
    with suppress(TypeError):
        _SCOPED_QUERIES[query] = made


def query_scopes(query: Any) -> frozenset[Any] | None:
    """The classes whose ``scope_rows`` *query* passed, or ``None`` if unscoped."""
    mark = _query_mark()
    made = getattr(query, _QUERY_SCOPED, None)
    if not isinstance(made, tuple) or made[0] is not mark:
        # WeakKeyDictionary treats an unhashable query as absent.
        try:
            made = _SCOPED_QUERIES.get(query)
        except TypeError:
            return None
    return made[1] if isinstance(made, tuple) and made[0] is mark else None


def query_is_scoped(query: Any) -> bool:
    """Return whether *query* was already passed through ``scope_rows``."""
    return query_scopes(query) is not None


def narrowed_from(query: Any, narrowed: Any) -> Any:
    """*narrowed*, a filter, ordering or page of *query*, scoped if *query* was.

    A clone, as Django's querysets make, forgets the mark.
    """
    scopes = query_scopes(query) if narrowed is not query else None
    if scopes is not None:
        mark_query_scoped(narrowed, scopes)
    return narrowed


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
    """The field names under *selection_set*, as far as a report shows them.

    It is recorded for every row that loads the relation, and a selection can
    repeat a field any number of times that runs once, so it stops once a
    report has as much as it would print.
    """
    if selection_set is None:
        return ""

    parts: list[str] = []
    size = 0

    def visit(node: Any) -> None:
        nonlocal size
        for selection in node.selections:
            if size > _MOST_ECHOED:
                return
            name_node = getattr(selection, "name", None)
            if name_node is None:
                continue
            nested = getattr(selection, "selection_set", None)
            if nested is not None and nested.selections:
                parts.append(f"{name_node.value} {{")
                size += len(parts[-1]) + 1
                visit(nested)
                parts.append("}")
            else:
                parts.append(name_node.value)
            size += len(parts[-1]) + 1

    visit(selection_set)
    return " ".join(parts)


def _query_selection_path(info: Any) -> str:
    """Return the GraphQL selection path that requested this relation."""
    operation = getattr(info, "operation", None)
    path_fields = _path_field_names(info)
    if operation is None or not path_fields:
        return f"{{ {_graphql_path_from_info(info)} }}"

    field_nodes = getattr(info, "field_nodes", None) or (None,)
    child = _format_selection_set(getattr(field_nodes[0], "selection_set", None))
    *outer, last = path_fields
    opened = [*outer, f"{last} {{ {child} }}" if child else last]
    inner = " { ".join(opened) + " }" * len(outer)

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


def field_label(info: Any) -> str:
    """``Type.field`` for the field *info* resolves, as the schema names it."""
    return f"{_parent_graphql_type(info)}.{_graphql_field_name(info)}"


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

    field_cls = type(field).__name__

    # A forward relation reads straight from this cache, so a cached ``None``
    # - a null key, or a row the scope hid - is as loaded as a cached row.
    if field_cls in ("ForeignKeyFieldInstance", "OneToOneFieldInstance") and (
        f"_{field_name}" in getattr(instance, "__dict__", {})
    ):
        return True

    fetched = getattr(instance, "_fetched", None)
    if isinstance(fetched, (set, frozenset)) and field_name in fetched:
        return True
    if isinstance(fetched, dict) and field_name in fetched:
        return True

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


#: How much of a request's own text - a path through its response keys, its
#: operation name, the selection under a relation - a report repeats, and how
#: many findings it lists: it is logged for every request.
_MOST_ECHOED = 160
_MOST_LISTED = 8


def _clipped(text: str) -> str:
    return text if len(text) <= _MOST_ECHOED else f"{text[: _MOST_ECHOED - 1]}…"


def _format_unoptimized_loads(
    loads: list[_UnoptimizedLoad],
    resolver_queries: list[_ResolverQueries] | None = None,
    causes: dict[str, list[_UnoptimizedLoad]] | None = None,
    bails: dict[str, tuple[str, str]] | None = None,
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
            f"  - path: {_clipped(load.query_path)}\n"
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

    for path in sorted(bails or {}):
        lines.append(
            f"  - path: {_clipped(path)}\n"
            f"    fell back to one query per parent: {(bails or {})[path][1]}\n"
            f"    fix: narrow it with a scope on orm.field.eager, which cannot "
            f"vary per parent and so never needs rewriting"
        )

    if len(lines) > _MOST_LISTED:
        lines[_MOST_LISTED:] = [f"  … and {len(lines) - _MOST_LISTED} more"]

    total = (
        len(loads)
        + sum(max(entry.count, entry.rows) for entry in merged_resolvers.values())
        + len(bails or {})
    )
    unique = len(grouped) + len(merged_resolvers) + len(bails or {})
    header = (
        f"Unoptimized relation loads detected ({total} total, {unique} unique) "
        f"— may waterfall (N+1):"
    )
    return f"{header}\n" + "\n".join(lines)


def _findings(
    loads: list[_UnoptimizedLoad],
    resolver_queries: list[_ResolverQueries],
    causes: dict[str, list[_UnoptimizedLoad]],
    bails: dict[str, tuple[str, str]],
) -> list[str]:
    """One warning per finding, naming only the schema's types and fields.

    Python keeps every distinct warning text it shows for the life of the
    process, so nothing a request chooses - its response keys, operation
    name, or how many rows it reads - may make a new one.
    """
    found = {
        f"{label} returned rows instead of a query object, so the relations "
        f"below it could not be eager-loaded"
        for label in causes
    }
    found.update(
        f"{load.resolver_type}.{load.graphql_field} loads "
        f"{load.model_name}.{load.orm_field} once per parent row; "
        f"fix: {load.hint}"
        for load in loads
    )
    found.update(
        f"{entry.resolver_type}.{entry.graphql_field} lazy-loaded "
        f"{', '.join(f'{entry.model_name}.{rel}' for rel in entry.relations)}; "
        f"fix: @orm.field.computed(using=[{', '.join(map(repr, entry.relations))}])"
        for entry in resolver_queries
    )
    found.update(
        f"{label} fell back to one query per parent: {reason}"
        for label, reason in bails.values()
    )
    return [
        f"Unoptimized relation loads detected: {finding}" for finding in sorted(found)
    ]


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

        before = unloaded_relations(self._backend, root)
        with self._backend.query_probe(info) as probe:
            result = _next(root, info, *args, **kwargs)

        # GraphQL drops a pending field when a sibling fails, and a coroutine
        # never started never runs its ``finally``: the await is probed
        # afresh, from inside the coroutine, so no probe outlives its frame.
        if isawaitable(result):
            return self._await_probed_result(probe.count, result, root, info, before)

        self._record_resolver_queries(root, info, probe, before)
        self._record_return_shape(info, result)
        return result

    async def _await_result(self, awaitable: Any, info: Any) -> Any:
        result = await awaitable
        self._record_return_shape(info, result)
        return result

    async def _await_probed_result(
        self,
        counted: int,
        awaitable: Any,
        root: Any,
        info: Any,
        before: set[str],
    ) -> Any:
        with self._backend.query_probe(info) as probe:
            result = await awaitable
        probe.count += counted
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
        self._shape_labels[path] = field_label(info)

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
        from strawberry_orm.batching import recorded_bails

        bails = recorded_bails(getattr(self, "execution_context", None))
        if (
            not self._loads and not self._resolver_queries and not bails
        ) or self._mode == "off":
            return

        causes: dict[str, list[_UnoptimizedLoad]] = {}
        for load in self._loads:
            ancestor = self._materializing_ancestor(load.path)
            if ancestor is not None:
                causes.setdefault(self._shape_labels[ancestor], []).append(load)

        message = _format_unoptimized_loads(
            self._loads, self._resolver_queries, causes, bails
        )
        logger.warning(message)
        if self._mode == "error":
            raise RuntimeError(message)
        if self._mode == "warn":
            for finding in _findings(
                self._loads, self._resolver_queries, causes, bails
            ):
                warnings.warn(finding, UserWarning, stacklevel=2)
