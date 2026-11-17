"""What an operation a client writes must pass before it runs."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from typing import Any

from graphql import (
    DirectiveNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLField,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLOutputType,
    GraphQLSchema,
    GraphQLUnionType,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    get_named_type,
    get_variable_values,
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

#: How many levels deep a call's variables may nest.
_MAX_VARIABLE_DEPTH = 100

_END = object()


def exposed_schema(
    schema: GraphQLSchema,
    *,
    allow_mutations: bool,
    root_fields: Collection[str] | None = None,
) -> GraphQLSchema:
    """The schema as the ``graphql`` tool shows it and lets it be used.

    No mutations unless they are allowed, and with ``root_fields`` only those
    root fields and the types they reach. A field that returns a root type
    returns it as shown here, and a field that returns a root type left out
    is left out itself. A field that returns an interface reaches its
    implementations, since a fragment on one is valid there, but a type is
    not reached by sharing an interface with one that is.
    """
    assert schema.query_type is not None
    kept = {schema.query_type.name: _kept(schema.query_type, root_fields)}
    if schema.mutation_type is not None:
        kept[schema.mutation_type.name] = (
            _kept(schema.mutation_type, root_fields) if allow_mutations else []
        )
    if schema.subscription_type is not None:
        kept[schema.subscription_type.name] = []
    shown = _shown(kept)
    query = shown(schema.query_type)
    mutation = shown(schema.mutation_type) if schema.mutation_type else None
    exposed = GraphQLSchema(
        query=query, mutation=mutation, directives=schema.directives
    )
    # ``types`` print first, so only implementations nothing else reaches go
    # there.
    missing = [
        named
        for named in _implementations(schema, (query, mutation), shown)
        if named.name not in exposed.type_map
    ]
    if not missing:
        return exposed
    return GraphQLSchema(
        query=query, mutation=mutation, types=missing, directives=schema.directives
    )


def refuses_introspection(schema: Any) -> bool:
    """Whether the Strawberry *schema* refuses ``__schema`` and ``__type``.

    Strawberry's ``DisableIntrospection`` is ``AddValidationRules`` with
    graphql-core's ``NoSchemaIntrospectionCustomRule``.
    """
    from graphql.validation import NoSchemaIntrospectionCustomRule
    from strawberry.extensions import AddValidationRules

    get_extensions = getattr(schema, "get_extensions", None)
    if get_extensions is None:
        return False
    return any(
        isinstance(extension, AddValidationRules)
        and any(
            issubclass(rule, NoSchemaIntrospectionCustomRule)
            for rule in extension.validation_rules
        )
        for extension in get_extensions()
    )


def exposed_sdl(schema: GraphQLSchema, *, descriptions: bool = True) -> str:
    """*schema* printed as SDL, without its descriptions and deprecation
    reasons unless asked."""
    sdl = print_schema(schema)
    if descriptions:
        return sdl
    return print_ast(visit(parse(sdl), _StripReasons()))


def strip_descriptions(node: Node) -> Node:
    """*node* without the descriptions on it or anything inside it."""
    return visit(node, _StripDescriptions())


def _implementations(
    schema: GraphQLSchema,
    roots: Iterable[GraphQLNamedType | None],
    shown: Callable[[GraphQLNamedType], GraphQLNamedType | None],
) -> list[GraphQLNamedType]:
    """The implementations, as *shown*, of each interface that a field
    reached from *roots* returns, in *schema*'s order.

    Only object types' fields are followed. A value has the type its own
    parent's field gives, so an interface's field, which may name a wider
    type, and an interface a type implements reach nothing more.
    """
    found: dict[str, GraphQLNamedType] = {}
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
            for possible in schema.get_possible_types(named):
                copy = shown(possible)
                if copy is not None:
                    found[copy.name] = copy
                    pending.append(copy)
        elif isinstance(named, GraphQLUnionType):
            pending.extend(named.types)
    return [found[name] for name in schema.type_map if name in found]


def _kept(root: GraphQLObjectType, names: Collection[str] | None) -> list[str]:
    return [name for name in root.fields if names is None or name in names]


def _shown(
    kept: Mapping[str, Collection[str]],
) -> Callable[[GraphQLNamedType], GraphQLNamedType | None]:
    """A copy of each type that may reach a root type, through which a root
    type has only the fields *kept* names, so the root types as they are
    never reach the schema the copies make up.

    A root type *kept* names no field of is ``None``, and a field or union
    member that would return it is left out. Scalars, enums and input types
    reach no root type and are left as they are.
    """
    copies: dict[str, GraphQLNamedType | None] = {}

    def named(type_: GraphQLNamedType) -> GraphQLNamedType | None:
        if type_.name in copies:
            return copies[type_.name]
        names = kept.get(type_.name)
        copy: GraphQLNamedType | None
        if names is not None and not names:
            copy = None
        elif isinstance(type_, GraphQLObjectType | GraphQLInterfaceType):
            copy = type(type_)(
                **{
                    **type_.to_kwargs(),
                    "fields": lambda: fields(type_, names),
                    "interfaces": lambda: [named(i) for i in type_.interfaces],
                }
            )
        elif isinstance(type_, GraphQLUnionType):
            copy = GraphQLUnionType(
                **{
                    **type_.to_kwargs(),
                    "types": lambda: [
                        t for t in map(named, type_.types) if t is not None
                    ],
                }
            )
        else:
            copy = type_
        copies[type_.name] = copy
        return copy

    def output(type_: GraphQLOutputType) -> GraphQLOutputType | None:
        if isinstance(type_, GraphQLNonNull | GraphQLList):
            inner = output(type_.of_type)
            return None if inner is None else type(type_)(inner)
        return named(type_)

    def fields(
        type_: GraphQLObjectType | GraphQLInterfaceType, names: Collection[str] | None
    ) -> dict[str, GraphQLField]:
        copied = {}
        for name, field in type_.fields.items():
            if names is not None and name not in names:
                continue
            returned = output(field.type)
            if returned is not None:
                copied[name] = GraphQLField(**{**field.to_kwargs(), "type_": returned})
        return copied

    return named


class _StripDescriptions(Visitor):
    def enter(self, node: Any, *_: Any) -> Any:
        if getattr(node, "description", None) is not None:
            return dataclasses.replace(node, description=None)
        return None


class _StripReasons(_StripDescriptions):
    """Strips deprecation reasons too, keeping ``@deprecated`` itself."""

    def enter_directive(self, node: DirectiveNode, *_: Any) -> Any:
        if node.name.value == "deprecated":
            return dataclasses.replace(node, arguments=())
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
    max_fields: int,
    schema_tool: bool = True,
    hide_suggestions: bool = False,
) -> tuple[list[str], OperationType | None]:
    """Errors that stop ``query`` from running, and the kind of operation it is.

    The operation runs on the whole schema, so ``__schema`` and ``__type``
    are refused: they would describe what *schema* leaves out. A document
    past ``max_tokens`` is refused while it is parsed, before anything else
    reads it. ``hide_suggestions`` leaves the names validation would
    suggest out of its errors.
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
    fields = _fields(operations[0].selection_set, fragments, {}, set())
    if fields > max_fields:
        return [
            f"The query selects {fields} fields, fragments included; "
            f"the limit is {max_fields}."
        ], kind
    finder = _FindIntrospection()
    visit(document, finder)
    if finder.found:
        if not schema_tool:
            return ["Introspection is not available here."], kind
        return [
            "Introspection is not available here; call graphql_schema for the schema."
        ], kind
    errors = validate(schema, document, hide_suggestions=hide_suggestions)
    return [e.message for e in errors], kind


def check_variables(variables: Mapping[str, Any], *, max_values: int) -> list[str]:
    """Errors that stop *variables* from reaching an operation.

    Each object, list and scalar in them is one value. Counting stops past
    ``max_values``, so it costs no more than that however much was sent.
    """
    pending: list[Iterator[Any]] = [iter(variables.values())]
    count = 0
    while pending:
        value = next(pending[-1], _END)
        if value is _END:
            pending.pop()
            continue
        count += 1
        if count > max_values:
            return [
                f"The variables hold over {max_values} values; "
                f"the limit is {max_values}."
            ]
        if len(pending) > _MAX_VARIABLE_DEPTH:
            return [
                f"The variables are nested over {_MAX_VARIABLE_DEPTH} levels deep; "
                f"the limit is {_MAX_VARIABLE_DEPTH}."
            ]
        if isinstance(value, dict):
            pending.append(iter(value.values()))
        elif isinstance(value, list | tuple):
            pending.append(iter(value))
    return []


def check_coercion(
    schema: GraphQLSchema, query: str, variables: dict[str, Any]
) -> list[str]:
    """Errors coercing *variables* to the types *query* declares, without
    suggestions.

    Execution coerces them too, but its errors suggest the names a value was
    near, such as the enum value one a letter off was meant to be. *query*
    must already have passed :func:`check_operation`.
    """
    (operation,) = (
        d for d in parse(query).definitions if isinstance(d, OperationDefinitionNode)
    )
    coerced = get_variable_values(
        schema, operation.variable_definitions or (), variables, hide_suggestions=True
    )
    return [error.message for error in coerced] if isinstance(coerced, list) else []


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


def _fields(
    selection_set: SelectionSetNode | None,
    fragments: dict[str, FragmentDefinitionNode],
    counts: dict[str, int],
    entered: set[str],
) -> int:
    """How many fields *selection_set* selects with its fragments expanded,
    each alias counted.

    Each fragment is counted once, in *counts*, and a fragment spread twice
    in one selection set once, as GraphQL does. A spread of a fragment being
    counted, a cycle validation refuses, adds nothing.
    """
    if selection_set is None:
        return 0
    total = 0
    spread: set[str] = set()
    pending = list(selection_set.selections)
    while pending:
        selection = pending.pop()
        if isinstance(selection, FieldNode):
            total += 1 + _fields(selection.selection_set, fragments, counts, entered)
        elif isinstance(selection, InlineFragmentNode):
            pending.extend(selection.selection_set.selections)
        else:
            assert isinstance(selection, FragmentSpreadNode)
            name = selection.name.value
            fragment = fragments.get(name)
            if fragment is None or name in entered or name in spread:
                continue
            spread.add(name)
            if name not in counts:
                entered.add(name)
                counts[name] = _fields(
                    fragment.selection_set, fragments, counts, entered
                )
                entered.discard(name)
            total += counts[name]
    return total


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
