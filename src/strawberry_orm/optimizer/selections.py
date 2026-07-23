"""Helpers for walking GraphQL selection sets used by the query optimizer."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from graphql.language.ast import FieldNode, FragmentSpreadNode, InlineFragmentNode

# Relay connection / grouping wrappers — not ORM fields; recurse into their selections.
_RELAY_PASSTHROUGH_FIELDS = frozenset(
    {
        "edges",
        "node",
        "cursor",
        "pageInfo",
        "page_info",
        "items",
        "groups",
        "aggregates",
        "edgeIndices",
        "edge_indices",
    }
)


def graphql_resolve_info(info: Any) -> Any:
    """Return graphql-core ``GraphQLResolveInfo`` from Strawberry ``Info`` or pass-through."""
    return getattr(info, "_raw_info", info)


def fragments_from_info(info: Any) -> dict[str, Any] | None:
    """Return the GraphQL fragment map from *info* (Strawberry or raw resolve info)."""
    return getattr(graphql_resolve_info(info), "fragments", None)


def field_nodes_from_info(info: Any) -> list[FieldNode]:
    """Return the current field's AST nodes from *info* (Strawberry or raw resolve info).

    Reads them through the raw graphql-core resolve info (``info._raw_info``) so the
    optimizer keeps working after strawberry-graphql 0.321 removed the deprecated
    ``Info.field_nodes`` shortcut (and avoids its ``DeprecationWarning`` on
    0.316-0.320). Falls back to ``info.field_nodes`` for a bare ``GraphQLResolveInfo``
    or test doubles that set the attribute directly.
    """
    raw_info = getattr(info, "_raw_info", None)
    if raw_info is not None:
        return list(raw_info.field_nodes)
    return list(getattr(info, "field_nodes", ()) or ())


def _relay_passthrough_field(name: str) -> bool:
    if name in _RELAY_PASSTHROUGH_FIELDS:
        return True
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()
    return snake in _RELAY_PASSTHROUGH_FIELDS


def iter_field_nodes(
    selection_set: Any,
    fragments: dict[str, Any] | None = None,
) -> Iterator[FieldNode]:
    """Yield ORM-relevant ``FieldNode`` selections for the optimizer.

    Recurses into inline fragments, fragment spreads, and Relay structural
    fields (``edges``, ``node``, ``items``, …) without yielding those wrappers.
    """
    if selection_set is None:
        return

    fragment_map = fragments or {}
    for node in selection_set.selections:
        if isinstance(node, FieldNode):
            if _relay_passthrough_field(node.name.value):
                yield from iter_field_nodes(node.selection_set, fragment_map)
            else:
                yield node
        elif isinstance(node, InlineFragmentNode):
            yield from iter_field_nodes(node.selection_set, fragment_map)
        elif isinstance(node, FragmentSpreadNode):
            fragment = fragment_map.get(node.name.value)
            if fragment is not None:
                yield from iter_field_nodes(fragment.selection_set, fragment_map)
