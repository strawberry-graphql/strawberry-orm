"""Helpers for walking GraphQL selection sets used by the query optimizer."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
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


class _AttributeOverride:
    """Read-through proxy that replaces a single attribute on *wrapped*."""

    def __init__(self, wrapped: Any, **overrides: Any) -> None:
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_wrapped"), name)


def _fields_in(
    selection_sets: Iterable[Any],
    fragments: dict[str, Any],
    *,
    through_wrappers: bool,
) -> Iterator[FieldNode]:
    """Yield the fields *selection_sets* select, their fragments' fields in place.

    A fragment is expanded once however often it is spread, as graphql-core's
    ``collect_fields`` does: its fields are the same nodes each time, so the
    next spread adds nothing, and expanding every spread again at every level
    costs exponentially more in the depth of a query that repeats them. With
    *through_wrappers*, Relay structural fields (``edges``, ``node``, …) are
    looked through rather than yielded.
    """
    visited: set[str] = set()

    def walk(selection_set: Any) -> Iterator[FieldNode]:
        if selection_set is None:
            return
        for node in selection_set.selections:
            if isinstance(node, FieldNode):
                if through_wrappers and _relay_passthrough_field(node.name.value):
                    yield from walk(node.selection_set)
                else:
                    yield node
            elif isinstance(node, InlineFragmentNode):
                yield from walk(node.selection_set)
            elif isinstance(node, FragmentSpreadNode):
                fragment = fragments.get(node.name.value)
                if fragment is not None and node.name.value not in visited:
                    visited.add(node.name.value)
                    yield from walk(fragment.selection_set)

    for selection_set in selection_sets:
        yield from walk(selection_set)


def _named_children(
    nodes: list[FieldNode], name: str, fragments: dict[str, Any]
) -> list[FieldNode]:
    """Return the selections named *name* directly under *nodes*."""
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()
    return [
        node
        for node in _fields_in(
            [node.selection_set for node in nodes], fragments, through_wrappers=False
        )
        if node.name.value == name
        or re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", node.name.value).lower() == snake
    ]


def narrow_info(info: Any, path: str | Sequence[str]) -> Any:
    """Return an ``info`` whose selection set is re-rooted at *path*.

    The optimizer reads the selection from the field currently being resolved.
    When the rows live further down - under a payload's ``data``, say - it has
    to be pointed at that node instead, or it will look for relations among
    ``data`` and ``errors`` and find nothing.
    """
    names = [path] if isinstance(path, str) else list(path)
    raw_info = graphql_resolve_info(info)
    fragments = fragments_from_info(info) or {}

    nodes = field_nodes_from_info(info)
    for name in names:
        nodes = _named_children(nodes, name, fragments)
        if not nodes:
            break

    return _AttributeOverride(
        info, _raw_info=_AttributeOverride(raw_info, field_nodes=nodes)
    )


def _unwrap_named_type(graphql_type: Any) -> Any:
    """Strip NonNull/List wrappers from *graphql_type* down to the named type."""
    while (
        graphql_type is not None and getattr(graphql_type, "of_type", None) is not None
    ):
        graphql_type = graphql_type.of_type
    return graphql_type


def reads_rows_of(registered: Any, model: Any) -> bool:
    """Whether a type over *registered* reads rows of *model*.

    A row of a subclass - an inherited mapping, a proxy, a multi-table child -
    is a row of its base too, and the base's type reads it as one.
    """
    return registered is model or (
        isinstance(registered, type)
        and isinstance(model, type)
        and issubclass(model, registered)
    )


def registered_type_name(
    graphql_type: Any, model: type, registry: dict[str, type]
) -> str | None:
    """Name under which *model* is registered, at or under *graphql_type*.

    One model can be registered as several GraphQL types, each with its own
    field hints, so hints must be resolved from the type the selection actually
    names - a model-keyed lookup can only ever find one of them.

    The rows often sit inside a wrapper - a connection's ``edges { node }``, a
    payload's ``data`` - so the wrapper's fields are searched breadth-first.
    Finding two different registered names for *model* at the same depth means
    the wrapper alone cannot say which one applies; that returns ``None`` and
    the caller falls back to the registration-order lookup.
    """
    current = _unwrap_named_type(graphql_type)
    if current is None:
        return None
    frontier = [current]
    seen = {current}
    for _ in range(4):
        names = [n.name for n in frontier if getattr(n, "name", None) is not None]
        matches = {name for name in names if registry.get(name) is model} or {
            name for name in names if reads_rows_of(registry.get(name), model)
        }
        if matches:
            return matches.pop() if len(matches) == 1 else None
        next_frontier: list[Any] = []
        for named in frontier:
            for field in (getattr(named, "fields", None) or {}).values():
                child = _unwrap_named_type(field.type)
                if child is not None and child not in seen:
                    seen.add(child)
                    next_frontier.append(child)
        if not next_frontier:
            return None
        frontier = next_frontier
    return None


def child_field_type(schema: Any, parent_type_name: str | None, field_name: str) -> Any:
    """Schema type that *field_name* on the type named *parent_type_name* yields.

    ``field_name`` is the name as it appears in the query AST, which is the
    schema spelling. ``None`` when any hop is unknown - a test double without a
    schema, a fragment field belonging to a sibling type - and the caller falls
    back.
    """
    type_map = getattr(schema, "type_map", None)
    if type_map is None or parent_type_name is None:
        return None
    fields = getattr(type_map.get(parent_type_name), "fields", None) or {}
    field = fields.get(field_name)
    return field.type if field is not None else None


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
    A fragment spread more than once is expanded once.
    """
    return _fields_in([selection_set], fragments or {}, through_wrappers=True)


def merged_field_nodes(
    selection_sets: Iterable[Any],
    fragments: dict[str, Any] | None = None,
) -> dict[str, list[FieldNode]]:
    """The fields :func:`iter_field_nodes` yields under *selection_sets*, by name.

    The optimizer plans a field from its name alone, so the fields of one name
    at one level - repeated, aliased, or from sibling fragments - are planned
    once, their selections walked together. Walked one field at a time, a
    fragment each of them spreads is walked again for every one, and so is
    everything below it, at every level down.
    """
    merged: dict[str, list[FieldNode]] = {}
    for node in _fields_in(selection_sets, fragments or {}, through_wrappers=True):
        merged.setdefault(node.name.value, []).append(node)
    return merged


def selects_anywhere(info: Any, names: frozenset[str]) -> bool:
    """Whether a field or fragment spread named in *names* is anywhere below *info*'s field.

    Strawberry's ``should_resolve_list_connection_edges`` asks this of
    ``info.selected_fields``, which converts the whole selection, each spread
    anew. Read off the query, a fragment is searched once: a second search
    of it finds what the first did.
    """
    raw_info = graphql_resolve_info(info)
    fragments = getattr(raw_info, "fragments", None) or {}
    visited: set[str] = set()

    def search(selection_set: Any) -> bool:
        if selection_set is None:
            return False
        for node in selection_set.selections:
            if isinstance(node, InlineFragmentNode):
                if search(node.selection_set):
                    return True
                continue
            if node.name.value in names:
                return True
            if isinstance(node, FieldNode):
                if search(node.selection_set):
                    return True
            elif node.name.value not in visited:
                visited.add(node.name.value)
                fragment = fragments.get(node.name.value)
                if fragment is not None and search(fragment.selection_set):
                    return True
        return False

    return any(search(node.selection_set) for node in field_nodes_from_info(info))


class SelectedNode:
    """A field the query selects, read from the query only as it is asked about.

    Stands in for Strawberry's ``SelectedField`` where the library reads what
    a field selects. ``Info.selected_fields`` converts everything below the
    field up front, each fragment spread anew, which costs exponentially more
    than the query's text when it repeats its spreads at every level. Here
    the fields of ``selections`` have their fragments' fields in place, each
    fragment's once.
    """

    __slots__ = ("_info", "_node")

    def __init__(self, info: Any, node: FieldNode) -> None:
        self._info = info
        self._node = node

    @property
    def name(self) -> str:
        return self._node.name.value

    @property
    def arguments(self) -> dict[str, Any]:
        from strawberry.types.nodes import convert_arguments

        return convert_arguments(self._info, self._node.arguments)

    @property
    def selections(self) -> list[SelectedNode]:
        fragments = getattr(self._info, "fragments", None) or {}
        return [
            SelectedNode(self._info, node)
            for node in _fields_in(
                [self._node.selection_set], fragments, through_wrappers=False
            )
        ]


def selected_fields(info: Any) -> list[Any]:
    """What *info*'s field selects, as ``info.selected_fields`` would say.

    Read from the query as it is asked about - see :class:`SelectedNode`. A
    stand-in for ``info`` that carries none of the query's nodes gives its
    own ``selected_fields``, if any.
    """
    raw_info = graphql_resolve_info(info)
    field_nodes = getattr(raw_info, "field_nodes", None)
    if not field_nodes:
        return getattr(info, "selected_fields", [])
    return [SelectedNode(raw_info, node) for node in field_nodes]
