"""What a model-written operation must pass before it runs."""

from __future__ import annotations

import json
from typing import Any

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLObjectType,
    GraphQLSchema,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    parse,
    print_schema,
    validate,
)
from strawberry.types.graphql import OperationType


def exposed_sdl(schema: GraphQLSchema, *, allow_mutations: bool) -> str:
    """The schema as the model sees it: no mutations unless they are allowed."""
    implementations = [
        named
        for named in schema.type_map.values()
        if isinstance(named, GraphQLObjectType) and named.interfaces
    ]
    exposed = GraphQLSchema(
        query=schema.query_type,
        mutation=schema.mutation_type if allow_mutations else None,
        types=implementations,
        directives=schema.directives,
    )
    return print_schema(exposed)


def check_operation(
    schema: GraphQLSchema,
    query: str,
    *,
    allowed: set[OperationType],
    max_depth: int,
) -> tuple[list[str], OperationType | None]:
    """Errors that stop ``query`` from running, and the kind of operation it is."""
    try:
        document = parse(query)
    except GraphQLError as error:
        return [error.message], None
    operations = [
        d for d in document.definitions if isinstance(d, OperationDefinitionNode)
    ]
    if len(operations) != 1:
        return ["Send exactly one operation."], None
    kind = OperationType(operations[0].operation.value)
    if kind not in allowed:
        return [f"{kind.value.capitalize()} operations are not allowed."], kind
    fragments = {
        d.name.value: d
        for d in document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }
    depth = _depth(operations[0].selection_set, fragments, frozenset())
    if depth > max_depth:
        return [f"The query is {depth} levels deep; the limit is {max_depth}."], kind
    return [e.message for e in validate(schema, document)], kind


def tool_arguments(raw: str) -> tuple[str, dict[str, Any], list[str]]:
    """The ``query`` and ``variables`` a model passed to a query tool."""
    try:
        arguments = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return "", {}, ["The tool arguments were not valid JSON."]
    query = arguments.get("query")
    variables = arguments.get("variables") or {}
    if not isinstance(query, str) or not isinstance(variables, dict):
        return "", {}, ["Pass query as a string and variables as an object."]
    return query, variables, []


def truncate(value: Any, limit: int) -> tuple[Any, bool]:
    """``value`` with every list cut to ``limit`` items, and whether any was cut."""
    if isinstance(value, list):
        items = [truncate(item, limit) for item in value[:limit]]
        cut = len(value) > limit or any(c for _, c in items)
        return [item for item, _ in items], cut
    if isinstance(value, dict):
        entries = {key: truncate(item, limit) for key, item in value.items()}
        cut = any(c for _, c in entries.values())
        return {key: item for key, (item, _) in entries.items()}, cut
    return value, False


def _depth(
    selection_set: SelectionSetNode | None,
    fragments: dict[str, FragmentDefinitionNode],
    seen: frozenset[str],
) -> int:
    if selection_set is None:
        return 0
    deepest = 0
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode):
            depth = 1 + _depth(selection.selection_set, fragments, seen)
        elif isinstance(selection, InlineFragmentNode):
            depth = _depth(selection.selection_set, fragments, seen)
        else:
            assert isinstance(selection, FragmentSpreadNode)
            name = selection.name.value
            fragment = fragments.get(name)
            if fragment is None or name in seen:
                continue
            depth = _depth(fragment.selection_set, fragments, seen | {name})
        deepest = max(deepest, depth)
    return deepest
