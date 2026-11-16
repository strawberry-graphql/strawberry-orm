"""What an operation a client writes must pass before it runs."""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Iterable
from typing import Any

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLInterfaceType,
    GraphQLNamedType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    get_named_type,
    parse,
    print_ast,
    print_schema,
    validate,
)
from graphql.language import BREAK, Node, Visitor, visit
from strawberry.types.graphql import OperationType

#: The meta fields that describe the schema an operation runs on.
_INTROSPECTION = frozenset({"__schema", "__type"})

#: How many fields an operation may merge into one place in its result.
_MAX_REPEATS = 20


def exposed_schema(
    schema: GraphQLSchema,
    *,
    allow_mutations: bool,
    root_fields: Collection[str] | None = None,
) -> GraphQLSchema:
    """The schema as the ``graphql`` tool shows it and lets it be used.

    No mutations unless they are allowed, and with ``root_fields`` only those
    root fields and the types they reach. A field that returns an interface
    reaches its implementations, since a fragment on one is valid there, but
    a type is not reached by sharing an interface with one that is.
    """
    query = _narrow(schema.query_type, root_fields)
    mutation = schema.mutation_type if allow_mutations else None
    if mutation is not None:
        mutation = _narrow(mutation, root_fields)
    exposed = GraphQLSchema(
        query=query, mutation=mutation, directives=schema.directives
    )
    # ``types`` print first, so only implementations nothing else reaches go
    # there.
    missing = [
        named
        for named in _implementations(schema, (query, mutation))
        if named.name not in exposed.type_map
    ]
    if not missing:
        return exposed
    return GraphQLSchema(
        query=query, mutation=mutation, types=missing, directives=schema.directives
    )


def exposed_sdl(schema: GraphQLSchema, *, descriptions: bool = True) -> str:
    """*schema* printed as SDL, without its descriptions unless asked."""
    sdl = print_schema(schema)
    if descriptions:
        return sdl
    return print_ast(strip_descriptions(parse(sdl)))


def strip_descriptions(node: Node) -> Node:
    """*node* without the descriptions on it or anything inside it."""
    return visit(node, _StripDescriptions())


def _implementations(
    schema: GraphQLSchema, roots: Iterable[GraphQLObjectType | None]
) -> list[GraphQLObjectType]:
    """The implementations of each interface that a field reached from
    *roots* returns, in *schema*'s order.

    Only object types' fields are followed. A value has the type its own
    parent's field gives, so an interface's field, which may name a wider
    type, and an interface a type implements reach nothing more.
    """
    found: set[str] = set()
    seen: set[str] = set()
    pending: list[GraphQLNamedType] = [root for root in roots if root is not None]
    while pending:
        named = pending.pop()
        if named.name in seen:
            continue
        seen.add(named.name)
        if isinstance(named, GraphQLObjectType):
            pending.extend(get_named_type(f.type) for f in named.fields.values())
        elif isinstance(named, GraphQLInterfaceType):
            possible = schema.get_possible_types(named)
            found.update(t.name for t in possible)
            pending.extend(possible)
        elif isinstance(named, GraphQLUnionType):
            pending.extend(named.types)
    return [
        named
        for named in schema.type_map.values()
        if isinstance(named, GraphQLObjectType) and named.name in found
    ]


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


class _FindIntrospection(Visitor):
    found = False

    def enter_field(self, node: FieldNode, *_: Any) -> Any:
        if node.name.value in _INTROSPECTION:
            self.found = True
            return BREAK
        return None


def check_operation(
    schema: GraphQLSchema,
    query: str,
    *,
    allowed: set[OperationType],
    max_depth: int,
    max_tokens: int,
) -> tuple[list[str], OperationType | None]:
    """Errors that stop ``query`` from running, and the kind of operation it is.

    The operation runs on the whole schema, so ``__schema`` and ``__type``
    are refused: they would describe what *schema* leaves out. A document
    past ``max_tokens`` is refused while it is parsed, before anything else
    reads it.
    """
    try:
        document = parse(query, max_tokens=max_tokens)
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
    depth = _depth(operations[0].selection_set, fragments, {}, set())
    if depth > max_depth:
        return [f"The query is {depth} levels deep; the limit is {max_depth}."], kind
    repeats = _repeats(operations[0].selection_set, fragments, {}, set())
    if repeats > _MAX_REPEATS:
        return [
            f"The query selects a field up to {repeats} times in one place; "
            f"the limit is {_MAX_REPEATS}."
        ], kind
    finder = _FindIntrospection()
    visit(document, finder)
    if finder.found:
        return [
            "Introspection is not available here; call graphql_schema for the schema."
        ], kind
    return [e.message for e in validate(schema, document)], kind


def _depth(
    selection_set: SelectionSetNode | None,
    fragments: dict[str, FragmentDefinitionNode],
    depths: dict[str, int],
    entered: set[str],
) -> int:
    """How many fields deep *selection_set* goes, fragments included.

    Each fragment is measured once, in *depths*: one spread twice by each of
    a chain of fragments would otherwise be walked once per path, which
    doubles with every link. A spread of a fragment being measured, a cycle
    validation refuses, adds nothing.
    """
    if selection_set is None:
        return 0
    deepest = 0
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode):
            depth = 1 + _depth(selection.selection_set, fragments, depths, entered)
        elif isinstance(selection, InlineFragmentNode):
            depth = _depth(selection.selection_set, fragments, depths, entered)
        else:
            assert isinstance(selection, FragmentSpreadNode)
            name = selection.name.value
            fragment = fragments.get(name)
            if fragment is None or name in entered:
                continue
            if name not in depths:
                entered.add(name)
                depths[name] = _depth(
                    fragment.selection_set, fragments, depths, entered
                )
                entered.discard(name)
            depth = depths[name]
        deepest = max(deepest, depth)
    return deepest


def _repeats(
    selection_set: SelectionSetNode | None,
    fragments: dict[str, FragmentDefinitionNode],
    tables: dict[str, dict[str, tuple[int, int]]],
    entered: set[str],
) -> int:
    """At most how many fields *selection_set* merges into one place in its
    result, fragments included.

    Fields that share a response name merge, and validation compares every
    pair of them and of the fields below them, so its cost grows with the
    square of this. Counts multiply down the tree, which can overstate a
    merge but never understates one. Each fragment is counted once, in
    *tables*, and a fragment spread twice in one selection set once, as
    GraphQL does.
    """
    if selection_set is None:
        return 1
    table = _merged(selection_set, fragments, tables, entered)
    return max((count * below for count, below in table.values()), default=1)


def _merged(
    selection_set: SelectionSetNode,
    fragments: dict[str, FragmentDefinitionNode],
    tables: dict[str, dict[str, tuple[int, int]]],
    entered: set[str],
) -> dict[str, tuple[int, int]]:
    """Each response name in *selection_set*, with how many fields have it
    and the most :func:`_repeats` below one of them."""
    table: dict[str, tuple[int, int]] = {}
    spread: set[str] = set()

    def add(name: str, count: int, below: int) -> None:
        seen, deepest = table.get(name, (0, 1))
        table[name] = (seen + count, max(deepest, below))

    pending = list(selection_set.selections)
    while pending:
        selection = pending.pop()
        if isinstance(selection, FieldNode):
            below = _repeats(selection.selection_set, fragments, tables, entered)
            add((selection.alias or selection.name).value, 1, below)
        elif isinstance(selection, InlineFragmentNode):
            pending.extend(selection.selection_set.selections)
        else:
            assert isinstance(selection, FragmentSpreadNode)
            name = selection.name.value
            fragment = fragments.get(name)
            if fragment is None or name in entered or name in spread:
                continue
            spread.add(name)
            if name not in tables:
                entered.add(name)
                tables[name] = _merged(
                    fragment.selection_set, fragments, tables, entered
                )
                entered.discard(name)
            for field, (count, below) in tables[name].items():
                add(field, count, below)
    return table
