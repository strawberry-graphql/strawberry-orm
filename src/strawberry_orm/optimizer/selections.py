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


def fragments_from_info(info: Any) -> dict[str, Any] | None:
    """Return the GraphQL fragment map from *info* (Strawberry or raw resolve info)."""
    fragments = getattr(info, "fragments", None)
    if fragments is not None:
        return fragments
    raw_info = getattr(info, "_raw_info", None)
    if raw_info is not None:
        return getattr(raw_info, "fragments", None)
    return None


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
