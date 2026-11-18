"""ORM-agnostic connection types built on strawberry.relay."""

from __future__ import annotations

import copy
import inspect
import statistics
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, cast, get_args, get_origin

import strawberry
from strawberry import field, relay
from strawberry.extensions.field_extension import (
    AsyncExtensionResolver,
    SyncExtensionResolver,
)
from strawberry.relay import Connection, Edge, ListConnection, PageInfo
from strawberry.relay.fields import ConnectionExtension
from strawberry.relay.types import Node
from strawberry.relay.utils import (
    SliceMetadata,
    from_base64,
    to_base64,
)
from strawberry.types import Info
from strawberry.types.base import StrawberryContainer, get_object_definition
from strawberry.types.lazy_type import LazyType
from strawberry.types.nodes import SelectedField
from strawberry.types.union import StrawberryUnion

from strawberry_orm._async import AwaitableOrValue, await_maybe
from strawberry_orm.backends._base import (
    _selection_requests,
)
from strawberry_orm.cover import copy_field
from strawberry_orm.optimizer.extension import (
    get_configured_optimizer,
    optimize_query_nodes,
    settled,
)
from strawberry_orm.optimizer.selections import selected_fields, selects_anywhere

NodeType = TypeVar("NodeType", bound=relay.Node)

#: Databases number rows with signed 64-bit integers, and a cursor can carry
#: any number at all; nothing past this is sent as an offset or a limit.
_LAST_POSITION = 2**63 - 1

#: What Strawberry's ``ListConnection`` looks for below a connection before
#: it builds edges.
_EDGE_FIELDS = frozenset({"edges", "pageInfo"})


def _edges_answered(info: Any) -> Any:
    """A copy of *info* answering what ``ListConnection`` asks before building edges.

    Strawberry asks by walking ``info.selected_fields`` for an ``edges`` or a
    ``pageInfo``, which converts everything the connection selects, each
    fragment spread anew: exponential in the depth of a query that repeats
    its spreads. The copy's ``selected_fields`` holds only the answer, read
    off the query.
    """
    if not isinstance(info, Info):
        return info
    edges = SelectedField(name="edges", directives={}, arguments={}, selections=[])
    answered = copy.copy(info)
    answered.selected_fields = [
        SelectedField(
            name=info.field_name,
            directives={},
            arguments={},
            selections=[edges] if selects_anywhere(info, _EDGE_FIELDS) else [],
        )
    ]
    return answered


def _then[T](value: AwaitableOrValue[T], step: Callable[[T], Any]) -> Any:
    """Apply *step* to *value*, awaiting either one where it is awaitable."""
    if not inspect.isawaitable(value):
        return step(value)

    async def _chained() -> Any:
        return await await_maybe(step(await value))

    return _chained()


def is_empty_page_at_the_end(first: Any, last: Any, before: Any) -> bool:
    """Whether the arguments ask for Relay's empty page at the end: ``last: 0``.

    Strawberry slices ``[-0:]`` for it, every row after the cursor. The rows
    the end follows are the rows a page of none at the cursor has after it,
    so it is read as ``first: 0`` and moved to the end with ``_at_the_end``.
    """
    return last == 0 and first is None and not before


def _at_the_end(connection: Any) -> Any:
    """*connection*, a page of none, moved from its cursor to the end."""
    page_info = connection.page_info
    page_info.has_previous_page = page_info.has_next_page
    page_info.has_next_page = False
    return connection


def _should_await_nodes(nodes: Any, info: Info) -> bool:
    """Return whether *nodes* should be awaited before pagination.

    ORM query objects (e.g. Tortoise ``QuerySet``) are awaitable but must not be
    awaited here; pagination and ``count()`` handle them in the active context.
    """
    if not inspect.isawaitable(nodes):
        return False
    if inspect.iscoroutine(nodes):
        return True
    backend = _orm_backend_from_info(info)
    return backend is None or not backend.is_query_object(nodes)


async def _await_nodes_if_needed(nodes: Any, info: Info) -> Any:
    if _should_await_nodes(nodes, info):
        return await nodes
    return nodes


def _orm_backend_from_info(info: Any) -> Any | None:
    """Return the active ORM backend from resolver context or schema extensions."""
    ctx = getattr(info, "context", None)
    if isinstance(ctx, dict):
        backend = ctx.get("_orm_backend")
    else:
        backend = getattr(ctx, "_orm_backend", None)
    if backend is not None:
        return backend
    state = connection_state(info)
    if state is not None:
        return state.backend
    from strawberry_orm.optimizer.extension import get_configured_optimizer

    backend, _store = get_configured_optimizer(info)
    if backend is not None:
        return backend
    backend = getattr(getattr(info, "schema", None), "_orm_backend", None)
    return backend if backend is not None else _field_backend(info)


def _field_backend(info: Any) -> Any:
    """The backend an extension the library put on *info*'s field carries.

    It is how an ``orm.connection`` holds its rows to scope in a schema that
    ``strawberry.Schema`` built without any of the library's extensions.
    """
    from strawberry_orm.backends._base import BaseBackend
    from strawberry_orm.batching import _strawberry_field
    from strawberry_orm.optimizer.selections import graphql_resolve_info

    field = _strawberry_field(graphql_resolve_info(info))
    for extension in getattr(field, "extensions", ()):
        backend = getattr(extension, "_backend", None)
        if isinstance(backend, BaseBackend):
            return backend
    return None


def _read_query(backend: Any, query: Any, info: Any) -> AwaitableOrValue[Any]:
    """The rows of *query*, held to ``scope_rows``.

    The optimizer reads it, with its loads, when the schema has one. Without
    it the scope goes on here and the rows it lets through are marked, so no
    ``node`` checks its row again.
    """
    from strawberry_orm.lazy_resolution import mark_rows_scoped

    if get_configured_optimizer(info)[1] is not None:
        return optimize_query_nodes(query, info)
    model = backend.query_model(query)
    query = backend.apply_type_scope(query, model, info)
    owners = frozenset() if model is None else backend.scope_owners(model)

    def marked(rows: list[Any]) -> list[Any]:
        if owners:
            mark_rows_scoped(rows, owners)
        return rows

    return _then(backend.materialize_query(query, info), marked)


def _page_size(info: Any, max_results: int | None) -> tuple[int, int]:
    """The ``max_results`` a page is refused above, and the size it is cut to.

    The size is ``max_results``, or ``default_query_limit`` when that is
    smaller, and is the page's size when neither ``first`` nor ``last`` is given.
    """
    if max_results is None:
        max_results = info.schema.config.relay_max_results
    limit = getattr(_orm_backend_from_info(info), "_default_query_limit", None)
    return max_results, max_results if limit is None else min(max_results, limit)


def _refuse_unreadable_cursor(argument: str, cursor: str | None) -> None:
    """Refuse a cursor that does not decode to a position, as one naming
    another type is refused.

    Strawberry's own error quotes what the cursor decoded to, which would
    repeat any text a client sends in the response and the server's log.
    """
    if not cursor:
        return
    try:
        int(from_base64(cursor)[1])
    except ValueError:
        raise TypeError(
            f"Argument '{argument}' contains a non-existing value."
        ) from None


def _held_to_one_page(
    info: Any, prefix: str | None, limits: tuple[int, int], **kwargs: Any
) -> dict[str, Any]:
    """*kwargs*, with the page held to *limits*, as ``_page_size`` gives them.

    Strawberry holds only ``first`` and ``last`` to ``max_results``, so
    ``before`` alone would page to its cursor, however far that is; it
    reads one page from the start instead, as no arguments do. A cursor
    before the first row is refused: Strawberry slices a Python list from
    its end there, which only reading every row repeats.
    """
    max_results, size = limits
    for argument in ("after", "before"):
        _refuse_unreadable_cursor(argument, kwargs.get(argument))
    window = SliceMetadata.from_arguments(
        info,
        before=kwargs.get("before"),
        after=kwargs.get("after"),
        first=kwargs.get("first"),
        last=kwargs.get("last"),
        max_results=max_results,
        prefix=prefix,
    )
    for argument, position in (("after", window.start), ("before", window.end)):
        if position < 0:
            raise ValueError(
                f"Argument '{argument}' cannot point before the first item."
            )
    first, last = kwargs.get("first"), kwargs.get("last")
    if first is None and last is None:
        first = size
    return {
        **kwargs,
        "max_results": max_results,
        "first": None if first is None else min(first, size),
        "last": None if last is None else min(last, size),
    }


class _PageRows:
    """A page the database cut at *offset*, indexed as the whole result.

    Strawberry slices what it is given by absolute position and numbers the
    cursors from there, so handing it the page at its original positions keeps
    every cursor and ``pageInfo`` flag exactly what slicing the full list gave.
    """

    __slots__ = ("offset", "rows")

    def __init__(self, rows: list[Any], offset: int) -> None:
        self.rows = rows
        self.offset = offset

    def __getitem__(self, window: slice) -> list[Any]:
        return self.rows[window.start - self.offset : window.stop - self.offset]


class PreslicedRows(list):
    """One parent's page, already cut to size by a windowed query.

    ``totalCount`` cannot be read off these rows: the window kept only the
    page, so counting them would report the page size as the total. The real
    figure is carried alongside, from a grouped count over the same query.
    """

    __slots__ = ("orm_offset", "orm_total_count")

    def __init__(self, rows: Any, total_count: int, offset: int = 0) -> None:
        super().__init__(rows)
        self.orm_total_count = total_count
        #: Where the first row sits in the parent's whole result.
        self.orm_offset = offset


def _visible_rows(nodes: Any, info: Info) -> Any:
    """*nodes*, a list, without the rows ``scope_rows`` hides.

    A query is scoped where it is read. Rows a resolver already read reach the
    connection unchecked, and each ``node`` is checked only as it resolves -
    after ``totalCount`` and the cursors have counted the hidden rows too. So
    they are checked here, before anything counts or slices them. ``settled``
    read any other iterable into a list, so what is not one holds no rows and
    is left as it is. Returns a coroutine on async backends.
    """
    from strawberry_orm.optimizer.extension import _is_prepared, _without_rejected

    backend = _orm_backend_from_info(info)
    if backend is None or not isinstance(nodes, list):
        return nodes
    rows = [
        row
        for row in nodes
        if backend.is_model_instance(row)
        and not _is_prepared(row, backend.row_scopes(type(row), info, row))
    ]
    if not rows:
        return nodes
    return _then(
        backend.scope_instances(rows, info),
        lambda kept: _without_rejected(nodes, rows, kept)[0],
    )


def _held_to_scope(nodes: Any, info: Info) -> Any:
    """*nodes* with nothing ``scope_rows`` hides, before anything pages them.

    A query gets the type's scope, which a scoped query carries only once;
    rows lose the ones the scope hides, as ``_visible_rows`` drops them, once
    ``settled`` has read them into a list.
    """
    backend = _orm_backend_from_info(info)
    if backend is None:
        return nodes
    read = settled(nodes, 1, backend)
    if inspect.iscoroutine(read):
        return _then(read, lambda nodes: _held_to_scope(nodes, info))
    if backend.is_query_object(read):
        return backend.apply_type_scope(read, backend.query_model(read), info)
    return _visible_rows(read, info)


def _connection_total_count(nodes: Any, info: Info) -> AwaitableOrValue[int]:
    """Count connection nodes before Relay pagination is applied."""
    carried = getattr(nodes, "orm_total_count", None)
    if carried is not None:
        return carried
    backend = _orm_backend_from_info(info)
    if backend is not None and backend.is_query_object(nodes):
        return backend.count_query(nodes, info)
    if inspect.isawaitable(nodes):

        async def _count_awaitable() -> int:
            resolved = await nodes
            return len(resolved)

        return _count_awaitable()
    try:
        return len(nodes)
    except TypeError:
        return len(list(nodes))


class ORMConnectionExtension(ConnectionExtension):
    """Connection field extension that holds rows to scope before pagination."""

    def apply(self, field: Any) -> None:
        """Make sure the field's connection type cuts its own page.

        A forward or lazy reference (``ListConnection["PostNode"]``) is only
        resolved here, as the schema is built, so the connection type
        ``orm.connection`` builds for a named node is built now. Any other type
        would read every row and slice the page in Python, past ``max_results``
        and ``default_query_limit``, so it is refused. A connection whose nodes
        cannot be rows of a model keeps its type, and so does a connection
        over a union, whose rows are held to scope before Strawberry pages
        them.
        """
        super().apply(field)
        node = _node_of(self.connection_type)
        if _cuts_its_own_page(self.connection_type) or not _may_be_rows(node):
            return
        definition = get_object_definition(self.connection_type)
        generic = definition.concrete_of if definition is not None else None
        if generic is None or generic.origin not in (
            Connection,
            ListConnection,
            ORMConnection,
        ):
            name = getattr(self.connection_type, "__name__", self.connection_type)
            raise TypeError(
                f"{field.python_name}: {name} slices the rows it is given in "
                f"Python, so its page would not be cut in the database or held "
                f"to max_results and default_query_limit. Derive it from "
                f"ORMListConnection."
            )
        if isinstance(node, StrawberryUnion):
            return
        self.connection_type = connection_type_for_node(node)
        field.type = self.connection_type

    def _paginate_nodes(
        self,
        nodes: Any,
        info: Info,
        *,
        before: str | None = None,
        after: str | None = None,
        first: int | None = None,
        last: int | None = None,
    ) -> Any:
        assert self.connection_type is not None
        return self.connection_type.resolve_connection(
            cast("Iterable[Node]", nodes),
            info=info,
            before=before,
            after=after,
            first=first,
            last=last,
            max_results=self.max_results,
        )

    def _resolve_nodes(
        self,
        nodes: Any,
        info: Info,
        *,
        before: str | None = None,
        after: str | None = None,
        first: int | None = None,
        last: int | None = None,
    ) -> Any:
        nodes = _held_to_scope(nodes, info)
        # An ORM connection cuts its page out of the query before reading it,
        # so it has to receive the query, not every row the query matches.
        if not _cuts_its_own_page(self.connection_type):
            nodes = optimize_query_nodes(nodes, info)
        if inspect.isawaitable(nodes):

            async def _await_optimized() -> Any:
                optimized = await _await_nodes_if_needed(nodes, info)
                connection = self._paginate_nodes(
                    optimized,
                    info,
                    before=before,
                    after=after,
                    first=first,
                    last=last,
                )
                if inspect.isawaitable(connection):
                    connection = await connection
                return connection

            return _await_optimized()

        connection = self._paginate_nodes(
            nodes,
            info,
            before=before,
            after=after,
            first=first,
            last=last,
        )
        if inspect.isawaitable(connection):

            async def _await_connection() -> Any:
                return await connection

            return _await_connection()
        return connection

    def resolve(
        self,
        next_: SyncExtensionResolver,
        source: Any,
        info: Info,
        *,
        before: str | None = None,
        after: str | None = None,
        first: int | None = None,
        last: int | None = None,
        **kwargs: Any,
    ) -> Any:
        nodes = next_(source, info, **kwargs)
        if inspect.isawaitable(nodes):

            async def _await_nodes() -> Any:
                resolved_nodes = await _await_nodes_if_needed(nodes, info)
                connection = self._resolve_nodes(
                    resolved_nodes,
                    info,
                    before=before,
                    after=after,
                    first=first,
                    last=last,
                )
                if inspect.isawaitable(connection):
                    connection = await connection
                return connection

            return _await_nodes()

        return self._resolve_nodes(
            nodes,
            info,
            before=before,
            after=after,
            first=first,
            last=last,
        )

    async def resolve_async(
        self,
        next_: AsyncExtensionResolver,
        source: Any,
        info: Info,
        *,
        before: str | None = None,
        after: str | None = None,
        first: int | None = None,
        last: int | None = None,
        **kwargs: Any,
    ) -> Any:
        nodes = await _await_nodes_if_needed(next_(source, info, **kwargs), info)
        connection = self._resolve_nodes(
            nodes,
            info,
            before=before,
            after=after,
            first=first,
            last=last,
        )
        if inspect.isawaitable(connection):
            connection = await connection
        return connection


def _cuts_its_own_page(connection_type: Any) -> bool:
    return isinstance(connection_type, type) and issubclass(
        connection_type, ORMListConnection
    )


def _unwrapped(type_: Any) -> Any:
    """*type_* without the lists, optionals and lazy references around it."""
    while True:
        if isinstance(type_, LazyType):
            type_ = type_.resolve_type()
        elif isinstance(type_, StrawberryContainer):
            type_ = type_.of_type
        else:
            return type_


def _node_of(connection_type: type) -> Any:
    """The type the edges of *connection_type* carry."""
    definition = get_object_definition(connection_type, strict=True)
    edges = definition.get_field("edges")
    assert edges is not None
    edge = _unwrapped(edges.resolve_type(type_definition=definition))
    edge_definition = get_object_definition(edge, strict=True)
    node = edge_definition.get_field("node")
    assert node is not None
    return _unwrapped(node.resolve_type(type_definition=edge_definition))


def _connection_nodes(type_: Any) -> list[Any]:
    """The node types of the connections a field of *type_* returns.

    A union can hold several connections; any other type holds one at most.
    """
    type_ = _unwrapped(type_)
    members = type_.types if isinstance(type_, StrawberryUnion) else [type_]
    return [
        _node_of(member)
        for member in map(_unwrapped, members)
        if isinstance(member, type) and issubclass(member, Connection)
    ]


def _may_be_rows(node_type: Any) -> bool:
    """Whether a connection over *node_type* can hand out rows of a model.

    It can over an ``@orm.type``, and over an interface or a union one
    belongs to: the rows are checked against scope by their model, not by
    the type the connection declares.
    """
    if isinstance(node_type, StrawberryUnion):
        return any(_may_be_rows(_unwrapped(member)) for member in node_type.types)
    pending = [node_type] if isinstance(node_type, type) else []
    while pending:
        cls = pending.pop()
        if getattr(cls, "__orm_model__", None) is not None:
            return True
        pending.extend(cls.__subclasses__())
    return False


def _holds_to_scope(extension: Any) -> bool:
    """Whether *extension* pages only rows held to ``scope_rows``.

    The library's own does, and so does Strawberry's when it pages an
    ``ORMListConnection`` that reads its rows as the library declared.
    """
    if isinstance(extension, ORMConnectionExtension):
        return True
    connection_type = getattr(extension, "connection_type", None)
    return (
        type(extension) is ConnectionExtension
        and _cuts_its_own_page(connection_type)
        and connection_type.resolve_connection.__func__
        is ORMListConnection.resolve_connection.__func__
    )


def _pages_before_scoping(field: Any) -> bool:
    """Whether *field* pages rows of a model before they are held to scope.

    Paging counts every row it is given into the cursors, ``pageInfo`` and
    ``totalCount``. A connection the library declared holds them to scope
    first; one a resolver builds itself, or an extension other than the
    library's pages, does not. An ``orm.connection`` left unbuilt pages
    nothing, and ``orm.schema()`` refuses it on its own.
    """
    if getattr(field, "_orm_connection", False) or getattr(
        field.default, "_orm_auto_field", False
    ):
        return False
    paging = [ext for ext in field.extensions if isinstance(ext, ConnectionExtension)]
    if paging and all(map(_holds_to_scope, paging)):
        return False
    return any(map(_may_be_rows, _connection_nodes(field.type)))


def _pages_before_scoping_message(name: str) -> str:
    return (
        f"{name}: its connection is paged before the rows in it are held to "
        f"scope_rows, so its edges, cursors and pageInfo would count the rows "
        f"the scope hides. Declare it with orm.connection or relay.connection "
        f"and return the rows, not a connection."
    )


def refuse_unscoped_page(info: Any) -> None:
    """Refuse the connection field *info* resolves if it paged rows unscoped.

    ``orm.schema()`` refuses such a field at build. This is the backstop for
    a schema built with ``strawberry.Schema`` directly, where Strawberry's
    ``relay.connection`` pages what the resolver returns as it is. The client
    is told only the field it asked for; what to do instead names the app's
    declarations, so that is kept as the cause, for the server log.
    """
    from strawberry_orm.batching import _strawberry_field

    field = _strawberry_field(info)
    if not _pages_before_scoping(field):
        return
    raise TypeError(
        f"Cannot page {info.field_name}: its pages would count rows the caller "
        f"cannot read."
    ) from TypeError(_pages_before_scoping_message(field.python_name))


def refuse_page_held_by_nothing(info: Any) -> None:
    """Refuse a page of rows that nothing in this execution holds to scope.

    An ``ORMListConnection`` holds its rows to ``scope_rows`` through the
    backend the schema's extensions name, or an ``orm.connection``'s own. A
    ``relay.connection`` in a schema ``strawberry.Schema`` built without
    ``orm.optimizer_extension()`` has neither: its rows would be paged,
    counted and served as the resolver returned them. A field whose type
    cannot be read may hold rows too.
    """
    from strawberry_orm.batching import _strawberry_field
    from strawberry_orm.optimizer.selections import graphql_resolve_info

    field = _strawberry_field(graphql_resolve_info(info))
    if field is not None and not any(map(_may_be_rows, _connection_nodes(field.type))):
        return
    raise TypeError(
        f"Cannot page {info.field_name}: its pages would count rows the caller "
        f"cannot read."
    ) from TypeError(
        f"{info.field_name}: no strawberry_orm backend holds its rows to "
        f"scope_rows. Build the schema with orm.schema(), or give "
        f"strawberry.Schema orm.optimizer_extension()."
    )


def paged_in_scope_schema_class(base: type) -> type:
    """A subclass of *base* that pages connections only over rows in scope."""
    return type(base.__name__, (_PagedInScopeSchema, base), {})


class _PagedInScopeSchema:
    """Mixed in ahead of a ``strawberry.Schema`` by ``paged_in_scope_schema_class``.

    Strawberry's ``relay.connection`` pages what the resolver returns with a
    ``ConnectionExtension`` of its own, rows the scopes hide included, and the
    ``node`` checked as each edge resolves then reports where they were. This
    schema converts a copy of such a field with ``ORMConnectionExtension`` in
    its place, one copy per field, so every lookup sees the same field. A
    field that would page rows of a model another way is refused.
    """

    def get_fields(self, type_definition: Any) -> list[Any]:
        fields = super().get_fields(type_definition)  # type: ignore[misc]
        return [self._orm_paged_in_scope(field) for field in fields]

    def _orm_paged_in_scope(self, field: Any) -> Any:
        copies = self.__dict__.setdefault("_orm_paged_copies", {})
        if id(field) in copies:
            return copies[id(field)][1]
        paged = field
        if any(type(ext) is ConnectionExtension for ext in field.extensions):
            paged = copy_field(field)
            paged.extensions = [
                ORMConnectionExtension(max_results=ext.max_results)
                if type(ext) is ConnectionExtension
                else ext
                for ext in field.extensions
            ]
        elif _pages_before_scoping(field):
            raise TypeError(_pages_before_scoping_message(field.python_name))
        copies[id(field)] = (field, paged)
        return paged


def _use_orm_connection_extension(field: Any) -> None:
    """Replace Strawberry's ``ConnectionExtension`` with ``ORMConnectionExtension``."""
    field.extensions = [
        ORMConnectionExtension(max_results=ext.max_results)
        if isinstance(ext, ConnectionExtension)
        else ext
        for ext in field.extensions
    ]


@dataclass(frozen=True)
class ConnectionState:
    """What a connection field's aggregates and groups are computed from."""

    backend: Any
    base_query: Any
    group_by: Any = None
    order: Any = None


_STATE_KEY = "_orm_connections"


def connection_states(info: Any) -> dict[tuple, ConnectionState] | None:
    """Where the execution *info* belongs to keeps its connection states.

    Under the library's schema extensions that is the execution itself: a
    context can be missing, or shared by several executions (one module-level
    object, or every operation on a websocket), and a path stashed by one would
    answer another's aggregates and groups.
    """
    from strawberry_orm.lazy_resolution import current_execution

    execution = current_execution()
    if execution is not None:
        return execution.connection_states
    context = info.context
    if context is None:
        return None
    if isinstance(context, dict):
        return context.setdefault(_STATE_KEY, {})
    states = getattr(context, _STATE_KEY, None)
    if states is None:
        states = {}
        setattr(context, _STATE_KEY, states)
    return states


def stash_state(info: Any, state: ConnectionState) -> None:
    """Keep *state* for the connection field *info* resolves.

    Keyed by the field's path: under async execution the same field resolves
    for every parent row at once, and a single slot would hand each of them
    whichever parent stashed last.
    """
    path = getattr(info, "path", None)
    states = connection_states(info) if path is not None else None
    if states is not None:
        states[tuple(path.as_list())] = state


def connection_state(info: Any) -> ConnectionState | None:
    path = getattr(info, "path", None)
    states = connection_states(info) if path is not None else None
    if states is None:
        return None
    return states.get(tuple(path.as_list()))


def _edge_memberships(
    connection: Any, state: ConnectionState, paths: list[Any], info: Any
) -> Any:
    """Each edge's group key, or the set of them, for ``edgeIndices``.

    A column of the row itself is read off the row. Anything the database
    works out, a related column, a truncated date or a ``@group_field``, is
    asked of it for the page's rows.
    """
    from strawberry_orm.grouping import needs_database_membership, own_key_values

    if not _selection_requests(info, "groups", "edgeIndices"):
        return None
    nodes = [edge.node for edge in connection.edges]
    if not needs_database_membership(paths):
        return [own_key_values(node, paths) for node in nodes]
    if not nodes:
        return []
    pks = [state.backend.instance_pk(node) for node in nodes]
    by_pk = state.backend.group_memberships(state.base_query, paths, pks, info)
    return _then(by_pk, lambda found: [found.get(pk, set()) for pk in pks])


def _compute_page_aggregates(edges: list[Any], meta: Any) -> Any:
    """Compute in-memory aggregates over just the page's edges."""
    nodes = [e.node for e in edges]
    count = len(nodes)

    kwargs: dict[str, Any] = {"count": count}

    def _coerce(value: Any) -> Any:
        """Try to coerce to float; fall back to the raw value."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return value

    for func_name, SubType, fields in [
        ("sum", meta.sum_type, meta.numeric_fields),
        ("avg", meta.avg_type, meta.numeric_fields),
        ("median", meta.median_type, meta.numeric_fields),
        ("min", meta.min_type, meta.comparable_fields),
        ("max", meta.max_type, meta.comparable_fields),
    ]:
        if SubType is None:
            continue
        sub_kwargs: dict[str, Any] = {}
        for fname, _ in fields:
            vals = [getattr(n, fname, None) for n in nodes]
            vals = [v for v in vals if v is not None]
            if not vals:
                sub_kwargs[fname] = None
                continue
            if func_name == "sum":
                sub_kwargs[fname] = _coerce(sum(vals))
            elif func_name == "avg":
                sub_kwargs[fname] = _coerce(sum(vals) / len(vals))
            elif func_name == "median":
                sub_kwargs[fname] = _coerce(statistics.median(vals))
            elif func_name == "min":
                sub_kwargs[fname] = _coerce(min(vals))
            elif func_name == "max":
                sub_kwargs[fname] = _coerce(max(vals))
        kwargs[func_name] = SubType(**sub_kwargs)

    return meta.aggregates_type(**kwargs)


@strawberry.type(description="A connection to a list of items.")
class ORMConnection(ListConnection[NodeType]):
    """Relay connection with ``totalCount`` for the full filtered result set."""

    total_count: int | None = field(
        default=None,
        description=(
            "Total number of items in this connection before pagination is applied."
        ),
    )


def connection_type_for_node(node_type: type) -> Any:
    """Return a concrete Strawberry connection type for *node_type* with ``totalCount``.

    A forward reference names the type, but the subclass's fields cannot
    resolve it: they are looked up where ``ORMListConnection`` is declared.
    ``orm.connection`` therefore builds it once the schema has resolved the
    name, in ``ORMConnectionExtension.apply``.
    """
    import types as _types_mod

    named = node_type
    if get_origin(named) is Annotated:
        # A lazy reference: named after the class it names.
        named = get_args(named)[0]
    name = getattr(named, "__name__", None) or getattr(named, "__forward_arg__", None)
    if name is None:
        name = str(node_type)
    type_name = f"{name}Connection"

    def _exec_body(ns: dict[str, Any]) -> None:
        ns["__annotations__"] = {"total_count": int | None}
        ns["total_count"] = None

    new_cls = _types_mod.new_class(
        type_name,
        (ORMListConnection[node_type],),  # type: ignore[misc]
        exec_body=_exec_body,
    )
    new_cls._node_type = node_type  # type: ignore[attr-defined]
    return strawberry.type(new_cls, name=type_name)


@strawberry.type(description="A connection to a list of items.")
class ORMListConnection(ORMConnection[NodeType]):
    """A ListConnection that works with any ORM backend.

    Given a query, it reads only the requested page plus one row, which is
    what answers ``hasNextPage``; ``totalCount`` is counted separately, and
    only when selected. Given rows, it slices them as Strawberry does.

    When the generated connection subclass has ``_orm_aggregate_meta`` set,
    this override computes aggregates, groups, and page-level aggregates
    based on the client's selection set.
    """

    @classmethod
    def resolve_connection(
        cls,
        nodes,
        *,
        info,
        **kwargs,
    ) -> AwaitableOrValue:
        kwargs = _held_to_one_page(
            info,
            cls._edge_type().CURSOR_PREFIX,
            _page_size(info, kwargs.get("max_results")),
            **kwargs,
        )
        if is_empty_page_at_the_end(
            kwargs.get("first"), kwargs.get("last"), kwargs.get("before")
        ):
            return _then(
                cls._resolve(nodes, info=info, **{**kwargs, "first": 0, "last": None}),
                _at_the_end,
            )
        return cls._resolve(nodes, info=info, **kwargs)

    @classmethod
    def _resolve(cls, nodes: Any, *, info: Any, **kwargs: Any) -> AwaitableOrValue:
        backend = _orm_backend_from_info(info)
        if backend is None:
            refuse_page_held_by_nothing(info)
            return cls._resolve_rows(nodes, info=info, **kwargs)
        read = settled(nodes, 1, backend)
        if inspect.iscoroutine(read):
            return _then(read, lambda nodes: cls._resolve(nodes, info=info, **kwargs))
        if backend.is_query_object(read):
            paged = cls._resolve_query_page(backend, read, info=info, **kwargs)
            if paged is not None:
                return paged
        return cls._resolve_rows(read, info=info, **kwargs)

    @classmethod
    def _resolve_query_page(
        cls,
        backend: Any,
        query: Any,
        *,
        info: Any,
        before: str | None = None,
        after: str | None = None,
        first: int | None = None,
        last: int | None = None,
        max_results: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """Read only the page *query* is asked for, or ``None`` to read it whole.

        ``scope_rows`` goes on before the cut, so the count and the page see
        the same rows, and Django can still filter a queryset it then slices.
        """
        prefix = cls._edge_type().CURSOR_PREFIX
        window = SliceMetadata.from_arguments(
            info,
            before=before,
            after=after,
            first=first,
            last=last,
            max_results=max_results,
            prefix=prefix,
        )
        query = backend.apply_type_scope(query, backend.query_model(query), info)
        want_total = _selection_requests(info, "totalCount")
        args = {
            "before": before,
            "after": after,
            "first": first,
            "last": last,
            "max_results": max_results,
            **kwargs,
        }

        def count() -> AwaitableOrValue[int | None]:
            return backend.count_query(query, info) if want_total else None

        if not selects_anywhere(info, _EDGE_FIELDS):
            # Strawberry builds no edges without edges or pageInfo selected.
            connection = cls._slice(_PageRows([], window.start), info=info, **args)
            return cls._finish_connection(connection, count(), info=info, **args)

        if window.end != sys.maxsize:
            limit = min(window.overfetch, _LAST_POSITION) - window.start

            def finish(rows: list[Any]) -> Any:
                connection = cls._slice(
                    _PageRows(rows, window.start), info=info, **args
                )
                # A short page is the end of the result, unless it started past
                # the end, so it gives the total. Otherwise count - only now,
                # after the page: an AsyncSession runs one statement at a time.
                ended = len(rows) < limit and (rows or not window.start)
                total = window.start + len(rows) if want_total and ended else count()
                return cls._finish_connection(connection, total, info=info, **args)

            # A window that ends where it starts, or past every addressable
            # row, holds nothing - as slicing the list would have found.
            if limit <= 0:
                return finish([])
            page = backend.apply_page(query, window.start, limit)
            if page is None:
                return None
            return _then(_read_query(backend, page, info), finish)

        # ``last`` without ``before`` counts back from the end, so the total
        # locates the window.
        return _then(
            backend.count_query(query, info),
            lambda total: cls._cut_tail(
                backend,
                query,
                total,
                start=window.start,
                prefix=prefix,
                info=info,
                want_total=want_total,
                **args,
            ),
        )

    @classmethod
    def _cut_tail(
        cls,
        backend: Any,
        query: Any,
        total: int,
        *,
        start: int,
        prefix: str,
        info: Any,
        want_total: bool,
        **args: Any,
    ) -> Any:
        """The last ``last`` rows after *start*, given the query's *total*."""
        last = args["last"]
        offset = max(start, total - last)
        limit = min(offset + last, _LAST_POSITION) - offset

        def finish(rows: list[Any]) -> Any:
            # Asked for as the plain page it now is, so Strawberry numbers the
            # cursors from *offset*. Relay measures hasPreviousPage from the
            # ``after`` cursor, not from the start of the list.
            connection = cls._slice(
                _PageRows(rows, offset),
                info=info,
                after=to_base64(prefix, offset - 1) if offset else None,
                first=len(rows),
                max_results=args["max_results"],
            )
            connection.page_info.has_previous_page = offset > start
            return cls._finish_connection(
                connection, total if want_total else None, info=info, **args
            )

        if limit <= 0:
            return finish([])
        page = backend.apply_page(query, offset, limit)
        if page is None:
            return cls._resolve_rows(query, info=info, **args)
        return _then(_read_query(backend, page, info), finish)

    @classmethod
    def _slice(cls, nodes: Any, *, info: Any, **kwargs: Any) -> AwaitableOrValue[Any]:
        return super().resolve_connection(nodes, info=_edges_answered(info), **kwargs)

    @classmethod
    def _edge_type(cls) -> Any:
        """The edge class, whose cursor prefix the page's cursors carry."""
        type_def = get_object_definition(cls, strict=True)
        edge = type_def.get_field("edges").resolve_type(type_definition=type_def)
        while isinstance(edge, StrawberryContainer):
            edge = edge.of_type
        return edge

    @classmethod
    def _resolve_rows(
        cls,
        nodes,
        *,
        info,
        **kwargs,
    ) -> AwaitableOrValue:
        """Read every row and slice the page out in Python."""

        def slice_visible(read: Any) -> AwaitableOrValue:
            visible = _visible_rows(read, info)
            if inspect.iscoroutine(visible):
                return _then(
                    visible, lambda rows: cls._slice_rows(rows, info=info, **kwargs)
                )
            return cls._slice_rows(visible, info=info, **kwargs)

        backend = _orm_backend_from_info(info)
        rows = nodes
        if backend is not None and backend.is_query_object(nodes):
            rows = _read_query(backend, nodes, info)
        if _should_await_nodes(rows, info):
            return _then(rows, slice_visible)
        return slice_visible(rows)

    @classmethod
    def _slice_rows(
        cls,
        nodes,
        *,
        info,
        **kwargs,
    ) -> AwaitableOrValue:
        want_total = _selection_requests(info, "totalCount")
        total_count: AwaitableOrValue[int | None] = (
            _connection_total_count(nodes, info) if want_total else None
        )
        if getattr(nodes, "orm_offset", 0):
            nodes = _PageRows(nodes, nodes.orm_offset)

        connection = super().resolve_connection(
            nodes, info=_edges_answered(info), **kwargs
        )

        if inspect.isawaitable(connection):

            async def _await_connection() -> Any:
                resolved = await connection
                finished = cls._finish_connection(
                    resolved,
                    total_count,
                    info=info,
                    **kwargs,
                )
                if inspect.isawaitable(finished):
                    return await finished
                return finished

            return _await_connection()

        return cls._finish_connection(connection, total_count, info=info, **kwargs)

    @classmethod
    def _attach_total_count(cls, connection: Any, total_count: int | None) -> Any:
        if total_count is not None:
            connection.total_count = total_count
        return connection

    @classmethod
    def _finish_connection(
        cls,
        connection: Any,
        total_count: AwaitableOrValue[int | None],
        *,
        info: Any,
        **kwargs: Any,
    ) -> AwaitableOrValue[Any]:
        # Groups and aggregates are awaitable on async backends whether or not
        # the total is, so the total waits for them either way.
        return _then(
            total_count,
            lambda count: _then(
                cls._post_process_connection(connection, info=info, **kwargs),
                lambda processed: cls._attach_total_count(processed, count),
            ),
        )

    @classmethod
    def _post_process_connection(cls, connection, *, info, **kwargs):
        meta = getattr(cls, "_orm_aggregate_meta", None)
        if meta is None:
            return connection
        state = connection_state(info)
        if state is None:
            return connection

        if _selection_requests(info, "aggregates"):
            connection.aggregates = state.backend.apply_aggregation(
                state.base_query, info, meta
            )

        def finish(grouping: Any) -> Any:
            if grouping is not None:
                cls._attach_groups(connection, *grouping)
            cls._attach_page_aggregates(connection, info, meta)
            return connection

        return _then(
            cls._collect_groups(
                connection, state, info, meta, kwargs.get("max_results")
            ),
            finish,
        )

    @classmethod
    def _collect_groups(cls, connection, state, info, meta, max_results):
        """Fetch the groups, their items, and the keys of the page's edges.

        Returns ``(groups, items_by_page, items_limits, memberships)``, ``None``
        when groups were not asked for, or a coroutine yielding them on async
        backends. Each group's ``items`` are held to the connection's
        *max_results* and ``default_query_limit``, and each distinct page of
        them selected is its own read. The reads run one after another: an
        async backend's connection serves one statement at a time.
        """
        from strawberry_orm.grouping import group_paths

        if not _selection_requests(info, "groups") or state.group_by is None:
            return None
        backend, base_query = state.backend, state.base_query
        paths = group_paths(backend, meta.model, state.group_by)
        if not paths:
            return [], {}, None, None

        limits = _page_size(info, max_results)
        pages = _items_pages(info, limits)

        def items(remaining: list[tuple[int, int]], read: dict) -> Any:
            if not remaining:
                return read
            start, end = page = remaining[0]
            by_key = backend.batch_group_items(
                base_query,
                paths,
                info,
                meta.model,
                per_group_limit=end - start,
                per_group_offset=start,
                order_input=state.order,
            )
            return _then(
                by_key, lambda found: items(remaining[1:], {**read, page: found})
            )

        groups = backend.apply_grouping(
            base_query, paths, info, meta, order_input=state.order
        )
        return _then(
            groups,
            lambda resolved: _then(
                items(pages, {}),
                lambda by_page: _then(
                    _edge_memberships(connection, state, paths, info),
                    lambda memberships: (resolved, by_page, limits, memberships),
                ),
            ),
        )

    @classmethod
    def _attach_groups(
        cls, connection, groups, items_by_page, items_limits, memberships
    ):
        from strawberry_orm.grouping import assign_edge_indices
        from strawberry_orm.lazy_resolution import mark_rows_scoped

        if memberships is not None:
            assign_edge_indices(groups, memberships)
        for grp in groups:
            grp._items_limits = items_limits
            grp._items_pages = {
                page: by_key.get(grp._orm_key_values, [])
                for page, by_key in items_by_page.items()
            }
            for rows in grp._items_pages.values():
                # Read from the scoped base query, so not re-checked per group.
                mark_rows_scoped(rows)

        connection.groups = groups

    @classmethod
    def _attach_page_aggregates(cls, connection, info, meta):
        page_info_type = getattr(cls, "_page_info_type", None)
        if page_info_type is None or not _selection_requests(
            info, "pageInfo", "aggregates"
        ):
            return

        pi = connection.page_info
        connection.page_info = page_info_type(
            start_cursor=pi.start_cursor,
            end_cursor=pi.end_cursor,
            has_previous_page=pi.has_previous_page,
            has_next_page=pi.has_next_page,
            aggregates=_compute_page_aggregates(connection.edges, meta),
        )


def _items_arguments(info: Any) -> list[dict[str, Any]]:
    """The arguments of every ``groups { items }`` the connection selects.

    Aliases and fragments can select ``items`` more than once, each with a
    page of its own, under more than one ``groups``.
    """
    from strawberry_orm.backends._base import _selected_fields

    return [
        items.arguments
        for connection in selected_fields(info)
        for groups in _selected_fields(connection)
        if groups.name == "groups"
        for items in _selected_fields(groups)
        if items.name == "items"
    ]


def _int_argument(arguments: dict[str, Any], name: str) -> int | None:
    """An ``Int`` argument as selected: a literal is its text."""
    value = arguments.get(name)
    return None if value is None else int(value)


def _items_page(
    info: Any, limits: tuple[int, int], **arguments: Any
) -> tuple[dict[str, Any], tuple[int, int]]:
    """A group's ``items`` page: its arguments held to *limits*, and its rows.

    The rows are where the page starts in each group and where it stops,
    counting the one that answers ``hasNextPage``. Arguments the connection
    would refuse are refused the same way.
    """
    page = _held_to_one_page(info, None, limits, **arguments)
    read = page
    if is_empty_page_at_the_end(page["first"], page["last"], page["before"]):
        read = {**page, "first": 0, "last": None}
    window = SliceMetadata.from_arguments(info, **read)
    start = min(window.start, _LAST_POSITION)
    if window.end == sys.maxsize:
        # ``last`` without ``before`` counts back from each group's end, which
        # the window cannot see; it reads one page after the cursor.
        return page, (start, min(start + limits[1] + 1, _LAST_POSITION))
    return page, (start, min(window.overfetch, _LAST_POSITION))


def _items_pages(info: Any, limits: tuple[int, int]) -> list[tuple[int, int]]:
    """The rows each distinct ``items`` page selected reads from a group.

    A page the field refuses is left out: the field reports it, and nothing
    is read for it.
    """
    pages = set()
    for arguments in _items_arguments(info):
        try:
            _page, rows = _items_page(
                info,
                limits,
                first=_int_argument(arguments, "first"),
                last=_int_argument(arguments, "last"),
                after=arguments.get("after"),
                before=arguments.get("before"),
            )
        except (TypeError, ValueError):
            continue
        pages.add(rows)
    return sorted(pages)


__all__ = [
    "Connection",
    "Edge",
    "ListConnection",
    "NodeType",
    "ORMConnection",
    "ORMConnectionExtension",
    "ORMListConnection",
    "connection_type_for_node",
    "PageInfo",
]
