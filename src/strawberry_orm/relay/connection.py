"""ORM-agnostic connection types built on strawberry.relay."""

from __future__ import annotations

from typing import Any, TypeVar

from strawberry import relay
from strawberry.relay import Connection, Edge, ListConnection, PageInfo

from strawberry_orm._async import AwaitableOrValue
from strawberry_orm.backends._base import (
    _selection_requests,
)

NodeType = TypeVar("NodeType", bound=relay.Node)


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


class ORMListConnection(ListConnection[NodeType]):
    """A ListConnection that works with any ORM backend.

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
        connection = super().resolve_connection(nodes, info=info, **kwargs)

        # Handle async case
        if not isinstance(connection, cls.__mro__[0]) and not isinstance(
            connection, ORMListConnection
        ):
            # It's likely an awaitable - return as-is for now
            # Async post-processing is handled by the caller
            return connection

        return cls._post_process_connection(connection, info=info, **kwargs)

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

        if _selection_requests(info, "groups"):
            ctx = info.context
            group_by = (
                ctx.get("_orm_group_by")
                if isinstance(ctx, dict)
                else getattr(ctx, "_orm_group_by", None)
            )
            order_input = (
                ctx.get("_orm_order")
                if isinstance(ctx, dict)
                else getattr(ctx, "_orm_order", None)
            )
            if group_by is not None:
                groups = backend.apply_grouping(
                    base_query,
                    group_by,
                    info,
                    meta,
                    order_input=order_input,
                )
                _assign_edge_indices(groups, connection.edges, meta.group_key_fields)

                if _selection_requests(info, "groups", "items"):
                    first = _extract_items_first(info)
                    after_cursor = _extract_items_after(info)
                    items_order = _extract_items_order(info)
                    offset = _decode_cursor_offset(after_cursor) if after_cursor else 0
                    limit = (first or 10) + offset + 1
                    items_by_key = backend.batch_group_items(
                        base_query,
                        meta.group_key_fields,
                        info,
                        meta.model,
                        per_group_limit=limit,
                        order_input=items_order,
                    )
                    for grp in groups:
                        key_tuple = tuple(
                            getattr(grp.key, k) for k in meta.group_key_fields
                        )
                        grp._items_nodes = items_by_key.get(key_tuple, [])

                for grp in groups:
                    grp._orm_base_query = base_query
                    grp._orm_backend = backend
                    grp._orm_model = meta.model

                connection.groups = groups

        page_info_type = getattr(cls, "_page_info_type", None)
        if page_info_type is not None and _selection_requests(
            info, "pageInfo", "aggregates"
        ):
            page_agg = _compute_page_aggregates(connection.edges, meta)
            pi = connection.page_info
            connection.page_info = page_info_type(
                start_cursor=pi.start_cursor,
                end_cursor=pi.end_cursor,
                has_previous_page=pi.has_previous_page,
                has_next_page=pi.has_next_page,
                aggregates=page_agg,
            )

        return connection


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


def _decode_cursor_offset(cursor: str) -> int:
    """Decode a relay ``arrayconnection`` cursor to a 0-based offset."""
    import base64

    try:
        decoded = base64.b64decode(cursor).decode("utf-8")
        prefix, _, offset_str = decoded.rpartition(":")
        return int(offset_str) + 1
    except Exception:
        return 0


__all__ = [
    "Connection",
    "Edge",
    "ListConnection",
    "NodeType",
    "ORMListConnection",
    "PageInfo",
]
