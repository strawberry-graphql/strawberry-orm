"""Helpers for walking GraphQL selection sets used by the query optimizer."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from graphql.language.ast import FieldNode, FragmentSpreadNode, InlineFragmentNode


def iter_field_nodes(
    selection_set: Any,
    fragments: dict[str, Any] | None = None,
) -> Iterator[FieldNode]:
    """Yield ``FieldNode`` selections, recursing into inline fragments and spreads."""
    if selection_set is None:
        return

    fragment_map = fragments or {}
    for node in selection_set.selections:
        if isinstance(node, FieldNode):
            yield node
        elif isinstance(node, InlineFragmentNode):
            yield from iter_field_nodes(node.selection_set, fragment_map)
        elif isinstance(node, FragmentSpreadNode):
            fragment = fragment_map.get(node.name.value)
            if fragment is not None:
                yield from iter_field_nodes(fragment.selection_set, fragment_map)
