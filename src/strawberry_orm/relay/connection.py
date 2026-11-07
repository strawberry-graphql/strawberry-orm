"""ORM-agnostic connection types built on strawberry.relay."""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable, Iterable
from typing import Any, TypeVar, cast

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
    should_resolve_list_connection_edges,
    to_base64,
)
from strawberry.types import Info
from strawberry.types.base import StrawberryContainer, get_object_definition

from strawberry_orm._async import AwaitableOrValue, await_maybe
from strawberry_orm.backends._base import (
    _selection_requests,
)
from strawberry_orm.optimizer.extension import (
    get_configured_optimizer,
    optimize_query_nodes,
)

NodeType = TypeVar("NodeType", bound=relay.Node)
T = TypeVar("T")

#: Databases number rows with signed 64-bit integers, and a cursor can carry
#: any number at all; nothing past this is sent as an offset or a limit.
_LAST_POSITION = 2**63 - 1


def _then(value: AwaitableOrValue[T], step: Callable[[T], Any]) -> Any:
    """Apply *step* to *value*, awaiting either one where it is awaitable."""
    if not inspect.isawaitable(value):
        return step(value)

    async def _chained() -> Any:
        return await await_maybe(step(await value))

    return _chained()


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
    ctx = info.context
    if isinstance(ctx, dict):
        backend = ctx.get("_orm_backend")
    else:
        backend = getattr(ctx, "_orm_backend", None)
    if backend is not None:
        return backend
    from strawberry_orm.optimizer.extension import get_configured_optimizer

    backend, _store = get_configured_optimizer(info)
    return backend


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
    """Connection field extension that applies optimizer hints before pagination."""

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


def _use_orm_connection_extension(field: Any) -> None:
    """Replace Strawberry's ``ConnectionExtension`` with ``ORMConnectionExtension``."""
    field.extensions = [
        ORMConnectionExtension(max_results=ext.max_results)
        if isinstance(ext, ConnectionExtension)
        else ext
        for ext in field.extensions
    ]


def _node_matches_group_key(node: Any, key: Any, key_fields: list[str]) -> bool:
    """Return ``True`` if *node*'s attributes match the group *key*."""
    for fname in key_fields:
        key_val = getattr(key, fname, None)
        if key_val is None:
            continue
        node_val = getattr(node, fname, None)
        if str(node_val) != str(key_val):
            return False
    return True


def _assign_edge_indices(
    groups: list[Any], edges: list[Any], key_fields: list[str]
) -> None:
    """Compute ``edge_indices`` for each group by scanning the page edges."""
    for group in groups:
        indices: list[int] = []
        for i, edge in enumerate(edges):
            if _node_matches_group_key(edge.node, group.key, key_fields):
                indices.append(i)
        group.edge_indices = indices


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

    *node_type* may still be a forward reference, which is the normal case for
    a module that declares its connections above the types they name. The
    subclass is built around the reference and Strawberry resolves it when the
    schema is assembled, so a deferred node type keeps ``totalCount`` rather
    than falling back to a bare connection.
    """
    import types as _types_mod

    name = getattr(node_type, "__name__", None) or getattr(
        node_type, "__forward_arg__", None
    )
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
        backend, store = get_configured_optimizer(info)
        if backend is not None and store is not None and backend.is_query_object(nodes):
            paged = cls._resolve_query_page(backend, nodes, info=info, **kwargs)
            if paged is not None:
                return paged
        return cls._resolve_rows(nodes, info=info, **kwargs)

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
        # Cursors are the client's to forge. A negative position slices a
        # Python list from its end, which only reading the whole list repeats.
        if window.start < 0 or window.end < 0:
            return None
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

        if not should_resolve_list_connection_edges(info):
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
            return _then(optimize_query_nodes(page, info), finish)

        # ``last`` without ``before`` counts back from the end, so the total
        # locates the window. ``last: 0`` is Strawberry's ``[-0:]``: every row
        # after the cursor, which is a full read either way.
        if not last:
            return None
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
        return _then(optimize_query_nodes(page, info), finish)

    @classmethod
    def _slice(cls, nodes: Any, **kwargs: Any) -> AwaitableOrValue[Any]:
        return super().resolve_connection(nodes, **kwargs)

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
        rows = optimize_query_nodes(nodes, info)
        if _should_await_nodes(rows, info):
            return _then(rows, lambda read: cls._slice_rows(read, info=info, **kwargs))
        return cls._slice_rows(rows, info=info, **kwargs)

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

        connection = super().resolve_connection(nodes, info=info, **kwargs)

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

        backend = (
            info.context.get("_orm_backend")
            if isinstance(info.context, dict)
            else getattr(info.context, "_orm_backend", None)
        )
        base_query = (
            info.context.get("_orm_base_query")
            if isinstance(info.context, dict)
            else getattr(info.context, "_orm_base_query", None)
        )

        if backend is None or base_query is None:
            return connection

        if _selection_requests(info, "aggregates"):
            agg = backend.apply_aggregation(base_query, info, meta)
            connection.aggregates = agg

        def finish(grouping: Any) -> Any:
            if grouping is not None:
                cls._attach_groups(connection, *grouping, backend, base_query, meta)
            cls._attach_page_aggregates(connection, info, meta)
            return connection

        return _then(cls._collect_groups(backend, base_query, info, meta), finish)

    @classmethod
    def _collect_groups(cls, backend, base_query, info, meta):
        """Fetch the groups and their items.

        Returns ``(groups, items_by_key, items_offset)``, ``None`` when groups
        were not asked for, or a coroutine yielding them on async backends.
        """
        if not _selection_requests(info, "groups"):
            return None

        ctx = info.context
        group_by = (
            ctx.get("_orm_group_by")
            if isinstance(ctx, dict)
            else getattr(ctx, "_orm_group_by", None)
        )
        if group_by is None:
            return None
        order_input = (
            ctx.get("_orm_order")
            if isinstance(ctx, dict)
            else getattr(ctx, "_orm_order", None)
        )

        groups = backend.apply_grouping(
            base_query,
            group_by,
            info,
            meta,
            order_input=order_input,
        )

        def with_items(resolved: list[Any]) -> Any:
            window = (
                _items_window(info)
                if _selection_requests(info, "groups", "items")
                else None
            )
            if window is None:
                return resolved, None, 0
            start, limit = window
            # Only now are the group key fields known: an async apply_grouping
            # records them on *meta* when it runs, not when it is called.
            items_by_key = backend.batch_group_items(
                base_query,
                meta.group_key_fields,
                info,
                meta.model,
                per_group_limit=limit,
                per_group_offset=start,
                order_input=_extract_items_order(info),
            )
            return _then(items_by_key, lambda by_key: (resolved, by_key, start))

        return _then(groups, with_items)

    @classmethod
    def _attach_groups(
        cls, connection, groups, items_by_key, items_offset, backend, base_query, meta
    ):
        _assign_edge_indices(groups, connection.edges, meta.group_key_fields)

        for grp in groups:
            if items_by_key is not None:
                key_tuple = tuple(getattr(grp.key, k) for k in meta.group_key_fields)
                grp._items_nodes = items_by_key.get(key_tuple, [])
                grp._items_offset = items_offset
            grp._orm_base_query = base_query
            grp._orm_backend = backend
            grp._orm_model = meta.model

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


def _get_items_arg(info: Any, arg_name: str) -> Any:
    """Extract a named argument from the ``groups { items(...) }`` selection."""
    from strawberry_orm.backends._base import _find_selection

    groups_sel = _find_selection(info, "groups")
    if groups_sel is None:
        return None
    for sel in groups_sel.selections:
        if sel.name == "items":
            arguments = getattr(sel, "arguments", {})
            if isinstance(arguments, dict):
                return arguments.get(arg_name)
            for arg in arguments:
                name = getattr(arg, "name", None) or arg
                if name == arg_name:
                    return getattr(arg, "value", arg)
    return None


def _extract_items_first(info: Any) -> int | None:
    """Extract the ``first`` argument from ``groups.items`` in the selection set."""
    val = _get_items_arg(info, "first")
    if val is not None:
        return int(val)
    return None


def _extract_items_after(info: Any) -> str | None:
    """Extract the ``after`` cursor argument from ``groups.items``."""
    val = _get_items_arg(info, "after")
    if val is not None:
        return str(val)
    return None


def _extract_items_order(info: Any) -> Any:
    """Extract the ``order`` argument from ``groups.items``."""
    return _get_items_arg(info, "order")


def _items_window(info: Any) -> tuple[int, int] | None:
    """Where each group's page of items starts, and how many rows it needs.

    Strawberry's own arithmetic for the ``items`` arguments. ``None`` when it
    would refuse them, or for a cursor before the first row: the field then
    reports the arguments itself, and nothing is read for it.
    """
    try:
        window = SliceMetadata.from_arguments(
            info,
            first=_extract_items_first(info),
            after=_extract_items_after(info),
            last=_get_items_arg(info, "last"),
            before=_get_items_arg(info, "before"),
        )
    except (TypeError, ValueError):
        return None
    if window.start < 0 or window.end < 0:
        return None
    start = min(window.start, _LAST_POSITION)
    if window.end == sys.maxsize:
        # ``last`` without ``before`` counts back from each group's end, which
        # the window cannot see; it reads the default page after the cursor.
        return start, 10 + 1
    return start, min(window.overfetch, _LAST_POSITION) - start


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
