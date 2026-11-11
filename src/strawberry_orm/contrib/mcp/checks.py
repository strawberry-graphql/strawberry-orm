"""What an operation a client writes must pass before it runs."""

from __future__ import annotations

import dataclasses
from collections.abc import Collection
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
    print_ast,
    print_schema,
    validate,
)
from graphql.language import Node, Visitor, visit
from strawberry.types.graphql import OperationType


def exposed_schema(
    schema: GraphQLSchema,
    *,
    allow_mutations: bool,
    root_fields: Collection[str] | None = None,
) -> GraphQLSchema:
    """The schema as the ``graphql`` tool shows it and lets it be used.

    No mutations unless they are allowed, and with ``root_fields`` only those
    root fields and the types they reach. Implementations of a reached
    interface count as reached, since a fragment on one is valid there.
    """
    query = _narrow(schema.query_type, root_fields)
    mutation = schema.mutation_type if allow_mutations else None
    if mutation is not None:
        mutation = _narrow(mutation, root_fields)
    implementations = [
        named
        for named in schema.type_map.values()
        if isinstance(named, GraphQLObjectType) and named.interfaces
    ]
    types: list[GraphQLObjectType] = []
    while True:
        exposed = GraphQLSchema(
            query=query,
            mutation=mutation,
            types=types,
            directives=schema.directives,
        )
        added = [
            named
            for named in implementations
            if named.name not in exposed.type_map
            and any(i.name in exposed.type_map for i in named.interfaces)
        ]
        if not added:
            return exposed
        types += added


def exposed_sdl(schema: GraphQLSchema, *, descriptions: bool = True) -> str:
    """*schema* printed as SDL, without its descriptions unless asked."""
    sdl = print_schema(schema)
    if descriptions:
        return sdl
    return print_ast(strip_descriptions(parse(sdl)))


def strip_descriptions(node: Node) -> Node:
    """*node* without the descriptions on it or anything inside it."""
    return visit(node, _StripDescriptions())


def _narrow(
    root: GraphQLObjectType | None, names: Collection[str] | None
) -> GraphQLObjectType | None:
    if root is None or names is None:
        return root
    fields = {name: f for name, f in root.fields.items() if name in names}
    if not fields:
        return None
    return GraphQLObjectType(root.name, fields, description=root.description)


class _StripDescriptions(Visitor):
    def enter(self, node: Any, *_: Any) -> Any:
        if getattr(node, "description", None) is not None:
            return dataclasses.replace(node, description=None)
        return None


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
