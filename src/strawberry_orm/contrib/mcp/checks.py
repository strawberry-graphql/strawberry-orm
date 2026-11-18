"""What an operation a client writes must pass before it runs."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from typing import Any

from graphql import (
    DirectiveNode,
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLDirective,
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
    TypeInfo,
    TypeInfoVisitor,
    get_named_type,
    parse,
    print_ast,
    print_schema,
    specified_rules,
    validate,
)
from graphql.execution.values import VariableValues, get_argument_values
from graphql.language import BREAK, Node, Visitor, visit
from strawberry.schema.validation_rules.maybe_null import MaybeNullValidationRule
from strawberry.schema.validation_rules.one_of import OneOfInputValidationRule
from strawberry.types.graphql import OperationType

#: The rules Strawberry validates an operation with: graphql-core's, and its own.
VALIDATION_RULES = (*specified_rules, MaybeNullValidationRule, OneOfInputValidationRule)

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
    """The schema as the ``graphql`` tool shows it and runs operations on.

    No mutations unless they are allowed, and with ``root_fields`` only those
    root fields and the types they reach. A field that returns a root type
    returns it as shown here, and a field that returns a type left out is
    left out itself: a root type no field of is kept, a type left with no
    fields, or a union with no members. A type keeps an interface only while
    it has every field the interface shows, so a root type narrowed out of
    an interface is not reached through it. A field that returns an
    interface reaches its implementations, since a fragment on one is valid
    there, but a type is not reached by sharing an interface with one that
    is.

    ``root_fields`` names fields of the Query and Mutation types, bare or
    qualified as ``Query.posts`` and ``Mutation.publish`` (or by the root
    types' own names). With mutations allowed, a bare name both types have
    is refused, as is a name of neither, and at least one query field must
    be named.
    """
    assert schema.query_type is not None
    names = (
        None
        if root_fields is None
        else _root_names(schema, root_fields, allow_mutations=allow_mutations)
    )

    def kept_of(root: GraphQLObjectType) -> list[str]:
        return [name for name in root.fields if names is None or name in names[root]]

    kept = {schema.query_type.name: kept_of(schema.query_type)}
    if schema.mutation_type is not None:
        kept[schema.mutation_type.name] = (
            kept_of(schema.mutation_type) if allow_mutations else []
        )
    if schema.subscription_type is not None:
        kept[schema.subscription_type.name] = []
    shown = _shown(schema, kept)
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


def _root_names(
    schema: GraphQLSchema, names: Collection[str], *, allow_mutations: bool
) -> dict[GraphQLObjectType, set[str]]:
    """The fields *names* keeps of each root type that may run."""
    query = schema.query_type
    assert query is not None
    mutation = schema.mutation_type if allow_mutations else None
    roots = [root for root in (query, mutation) if root is not None]
    qualifiers = {"Query": query, query.name: query}
    if mutation is not None:
        qualifiers.update({"Mutation": mutation, mutation.name: mutation})
    kept: dict[GraphQLObjectType, set[str]] = {root: set() for root in roots}
    unknown = []
    for name in names:
        qualifier, dot, field = name.rpartition(".")
        if dot:
            owners = [qualifiers[qualifier]] if qualifier in qualifiers else []
            owners = [root for root in owners if field in root.fields]
        else:
            field = name
            owners = [root for root in roots if name in root.fields]
        if len(owners) > 1:
            raise ValueError(
                f"root_fields names {name}, a field of both {query.name} and "
                f"{owners[1].name}: write {query.name}.{name} or "
                f"{owners[1].name}.{name}."
            )
        if not owners:
            unknown.append(name)
            continue
        kept[owners[0]].add(field)
    if unknown:
        hint = "" if allow_mutations else " (mutations need allow_mutations)"
        raise ValueError(
            f"root_fields names no such root field: {sorted(unknown)}{hint}."
        )
    if not kept[query]:
        raise ValueError("root_fields must include at least one query field.")
    return kept


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


def escaping_types(schema: GraphQLSchema, exposed: GraphQLSchema) -> set[str]:
    """The names of the interfaces and unions of *exposed* that *schema*
    resolves to an object type they do not have there: one *exposed* leaves
    out, or a root type it narrows out of an interface."""
    names = set()
    for name, type_ in exposed.type_map.items():
        if isinstance(type_, GraphQLInterfaceType | GraphQLUnionType):
            shown = {t.name for t in exposed.get_possible_types(type_)}
            original = schema.get_type(name)
            assert isinstance(original, GraphQLInterfaceType | GraphQLUnionType)
            if any(t.name not in shown for t in schema.get_possible_types(original)):
                names.add(name)
    return names


def reaching(
    schema: GraphQLSchema, document: DocumentNode, names: Collection[str]
) -> str | None:
    """The first field of *document*, which passed validation against
    *schema*, that returns a type *names* names, as ``Type.field``."""
    type_info = TypeInfo(schema)
    finder = _FindReaching(type_info, names)
    visit(document, TypeInfoVisitor(type_info, finder))
    return finder.found


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
                if isinstance(copy, GraphQLObjectType) and named in copy.interfaces:
                    found[copy.name] = copy
                    pending.append(copy)
        elif isinstance(named, GraphQLUnionType):
            pending.extend(named.types)
    return [found[name] for name in schema.type_map if name in found]


def _shown(
    schema: GraphQLSchema, kept: Mapping[str, Collection[str]]
) -> Callable[[GraphQLNamedType], GraphQLNamedType | None]:
    """A copy of each type that may reach a root type, through which a root
    type has only the fields *kept* names, so the root types as they are
    never reach the schema the copies make up.

    A type :func:`_hidden` leaves out is ``None``, and a field or union
    member that would return it is left out. An interface or union resolves
    a value's type as the original does. Scalars, enums and input types
    reach no root type and are left as they are.
    """
    hidden = _hidden(schema, kept)
    copies: dict[str, GraphQLNamedType | None] = {}

    def named(type_: GraphQLNamedType) -> GraphQLNamedType | None:
        if type_.name in copies:
            return copies[type_.name]
        names = kept.get(type_.name)
        copy: GraphQLNamedType | None
        if type_.name in hidden:
            copy = None
        elif isinstance(type_, GraphQLObjectType | GraphQLInterfaceType):
            copy = type(type_)(
                **{
                    **type_.to_kwargs(),
                    **_resolving(type_),
                    "fields": lambda: fields(type_, names),
                    "interfaces": lambda: interfaces(type_, names),
                }
            )
        elif isinstance(type_, GraphQLUnionType):
            copy = GraphQLUnionType(
                **{
                    **type_.to_kwargs(),
                    **_resolving(type_),
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
        for name in _field_names(type_, names, hidden):
            field = type_.fields[name]
            returned = output(field.type)
            copied[name] = GraphQLField(**{**field.to_kwargs(), "type_": returned})
        return copied

    def interfaces(
        type_: GraphQLObjectType | GraphQLInterfaceType, names: Collection[str] | None
    ) -> list[GraphQLInterfaceType]:
        own = set(_field_names(type_, names, hidden))
        return [
            copy
            for interface in type_.interfaces
            if (copy := named(interface)) is not None
            and own.issuperset(_field_names(interface, None, hidden))
        ]

    return named


def _hidden(schema: GraphQLSchema, kept: Mapping[str, Collection[str]]) -> set[str]:
    """The names of the types left with nothing to show: a root type *kept*
    names no field of, a type whose every field returns a type left out,
    and a union whose every member is."""
    hidden = {name for name, names in kept.items() if not names}
    changed = True
    while changed:
        changed = False
        for type_ in schema.type_map.values():
            if type_.name in hidden:
                continue
            if isinstance(type_, GraphQLObjectType | GraphQLInterfaceType):
                empty = not _field_names(type_, kept.get(type_.name), hidden)
            elif isinstance(type_, GraphQLUnionType):
                empty = all(member.name in hidden for member in type_.types)
            else:
                continue
            if empty:
                hidden.add(type_.name)
                changed = True
    return hidden


def _field_names(
    type_: GraphQLObjectType | GraphQLInterfaceType,
    names: Collection[str] | None,
    hidden: set[str],
) -> list[str]:
    return [
        name
        for name, field in type_.fields.items()
        if (names is None or name in names)
        and get_named_type(field.type).name not in hidden
    ]


def _resolving(type_: GraphQLNamedType) -> dict[str, Any]:
    """``resolve_type`` for a copy of *type_*, if it is an abstract type
    that resolves values itself: as *type_* does, given *type_*."""
    resolve = getattr(type_, "resolve_type", None)
    if resolve is None:
        return {}
    return {"resolve_type": lambda value, info, _copy: resolve(value, info, type_)}


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


class _FindReaching(Visitor):
    def __init__(self, type_info: TypeInfo, names: Collection[str]) -> None:
        super().__init__()
        self.type_info = type_info
        self.names = names
        self.found: str | None = None

    def enter_field(self, node: FieldNode, *_: Any) -> Any:
        returned = get_named_type(self.type_info.get_type())
        if returned is not None and returned.name in self.names:
            parent = self.type_info.get_parent_type()
            assert parent is not None
            self.found = f"{parent.name}.{node.name.value}"
            return BREAK
        return None


@dataclasses.dataclass(frozen=True)
class Parsed:
    """A document of one operation and the fragments it uses, parsed once."""

    document: DocumentNode
    operation: OperationDefinitionNode
    fragments: Mapping[str, FragmentDefinitionNode]

    @property
    def kind(self) -> OperationType:
        return OperationType(self.operation.operation.value)

    @classmethod
    def of(cls, document: DocumentNode) -> Parsed:
        """*document*, which holds one operation and only the fragments it
        uses, each defined once."""
        (operation,) = (
            d for d in document.definitions if isinstance(d, OperationDefinitionNode)
        )
        fragments = {
            d.name.value: d
            for d in document.definitions
            if isinstance(d, FragmentDefinitionNode)
        }
        return cls(document, operation, fragments)


def read_operation(
    schema: GraphQLSchema,
    query: str,
    *,
    allowed: set[OperationType],
    max_length: int = 100_000,
    max_depth: int,
    max_tokens: int,
    max_fields: int,
    schema_tool: bool = True,
    hide_suggestions: bool = False,
) -> tuple[list[str], Parsed | None]:
    """Errors that stop ``query`` from running, and its operation, parsed
    once, if it holds exactly one.

    A document past ``max_length`` characters is refused before anything
    reads it, and one past ``max_tokens`` tokens while it is parsed. Each
    fragment must be defined once and used, so the depth, merge and field
    limits measure everything validation reads. ``__schema`` and ``__type``
    are refused: ``graphql_schema`` describes what *schema* shows. Then it
    is validated against *schema*, with the rules Strawberry validates with;
    ``hide_suggestions`` leaves the names validation would suggest out of
    its errors.
    """
    if len(query) > max_length:
        return [
            f"The query is over {max_length} characters long; "
            f"the limit is {max_length}."
        ], None
    try:
        document = parse(query, max_tokens=max_tokens)
    except GraphQLError as error:
        return [error.message], None
    operations = [
        d for d in document.definitions if isinstance(d, OperationDefinitionNode)
    ]
    if len(operations) != 1:
        return ["Send exactly one operation."], None
    fragments: dict[str, FragmentDefinitionNode] = {}
    twice = []
    for definition in document.definitions:
        if isinstance(definition, FragmentDefinitionNode):
            name = definition.name.value
            if name in fragments:
                twice.append(f"There can be only one fragment named '{name}'.")
            fragments[name] = definition
    parsed = Parsed(document, operations[0], fragments)
    kind = parsed.kind
    if kind not in allowed:
        return [f"{kind.value.capitalize()} operations are not allowed."], parsed
    if twice:
        return twice, parsed
    used = _used(parsed.operation, fragments)
    unused = [
        f"Fragment '{name}' is never used." for name in fragments if name not in used
    ]
    if unused:
        return unused, parsed
    selection_set = parsed.operation.selection_set
    depth = _depth(selection_set, fragments, {}, set())
    if depth > max_depth:
        return [f"The query is {depth} levels deep; the limit is {max_depth}."], parsed
    repeats = _repeats(selection_set, fragments, {}, set())
    if repeats > _MAX_REPEATS:
        return [
            f"The query selects a field up to {repeats} times in one place; "
            f"the limit is {_MAX_REPEATS}."
        ], parsed
    fields = _fields(selection_set, fragments, {}, set())
    if fields > max_fields:
        return [
            f"The query selects {fields} fields, fragments included; "
            f"the limit is {max_fields}."
        ], parsed
    finder = _FindIntrospection()
    visit(document, finder)
    if finder.found:
        if not schema_tool:
            return ["Introspection is not available here."], parsed
        return [
            "Introspection is not available here; call graphql_schema for the schema."
        ], parsed
    errors = validate(
        schema, document, VALIDATION_RULES, hide_suggestions=hide_suggestions
    )
    return [e.message for e in errors], parsed


def check_operation(
    schema: GraphQLSchema,
    query: str,
    *,
    allowed: set[OperationType],
    max_depth: int,
    max_tokens: int,
    max_fields: int,
    max_length: int = 100_000,
    schema_tool: bool = True,
    hide_suggestions: bool = False,
) -> tuple[list[str], OperationType | None]:
    """Errors that stop ``query`` from running, and the kind of operation it
    is: :func:`read_operation` without the operation."""
    errors, parsed = read_operation(
        schema,
        query,
        allowed=allowed,
        max_length=max_length,
        max_depth=max_depth,
        max_tokens=max_tokens,
        max_fields=max_fields,
        schema_tool=schema_tool,
        hide_suggestions=hide_suggestions,
    )
    return errors, None if parsed is None else parsed.kind


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


class _OverBudget(Exception):
    pass


class Costs:
    """How many values each field's arguments hold, as Strawberry converts
    them each time the field runs: each object, list and scalar inside each
    of its arguments and its directives' arguments, with variables
    substituted and defaults filled in, as GraphQL coerces them.

    A field is counted once, however often it runs, and counting it stops
    past ``max_values``.
    """

    def __init__(
        self, schema: GraphQLSchema, variables: VariableValues, max_values: int
    ) -> None:
        self.schema = schema
        self.variables = variables
        self.max_values = max_values
        #: Whether a field :meth:`check` passed may be a Mutation type's.
        self.writes = False
        self._counted: dict[Any, int] = {}
        self._writable: dict[str, bool] = {}

    def of(self, field: GraphQLField, node: FieldNode) -> int:
        """What *field*'s arguments, as *node* gives them, cost each time it runs."""
        key = (node.loc.start, node.loc.end) if node.loc else id(node)
        cost = self._counted.get(key)
        if cost is None:
            cost = self._counted[key] = self._count(field, node)
        return cost

    def check(self, parsed: Parsed) -> list[str]:
        """Errors if *parsed*'s fields, which passed validation, cost more
        than ``max_values`` between them, each counted wherever an alias or
        fragment repeats it, as if every list held one item."""
        root = self.schema.get_root_type(parsed.operation.operation)
        assert root is not None
        try:
            self._total(parsed.operation.selection_set, root, parsed.fragments, {})
        except _OverBudget:
            return [
                f"The arguments hold over {self.max_values} values, each counted "
                "wherever an alias or fragment repeats it; "
                f"the limit is {self.max_values}."
            ]
        return []

    def _total(
        self,
        selection_set: SelectionSetNode,
        parent: GraphQLNamedType | None,
        fragments: Mapping[str, FragmentDefinitionNode],
        totals: dict[str, int],
    ) -> int:
        total = 0
        spread: set[str] = set()
        for selection in selection_set.selections:
            if isinstance(selection, FieldNode):
                total += self._field(selection, parent, fragments, totals)
            elif isinstance(selection, InlineFragmentNode):
                condition = selection.type_condition
                inner = (
                    parent if condition is None else self._type(condition.name.value)
                )
                total += self._total(selection.selection_set, inner, fragments, totals)
            else:
                assert isinstance(selection, FragmentSpreadNode)
                name = selection.name.value
                if name in spread:
                    continue
                spread.add(name)
                if name not in totals:
                    fragment = fragments[name]
                    totals[name] = self._total(
                        fragment.selection_set,
                        self._type(fragment.type_condition.name.value),
                        fragments,
                        totals,
                    )
                total += totals[name]
            if total > self.max_values:
                raise _OverBudget
        return total

    def _field(
        self,
        node: FieldNode,
        parent: GraphQLNamedType | None,
        fragments: Mapping[str, FragmentDefinitionNode],
        totals: dict[str, int],
    ) -> int:
        if not isinstance(parent, GraphQLObjectType | GraphQLInterfaceType):
            return 0
        field = parent.fields.get(node.name.value)
        if field is None:
            return 0
        self.writes = self.writes or self._may_write(parent)
        total = self.of(field, node)
        if node.selection_set is not None:
            total += self._total(
                node.selection_set, get_named_type(field.type), fragments, totals
            )
        return total

    def _type(self, name: str) -> GraphQLNamedType | None:
        return self.schema.get_type(name)

    def _may_write(self, parent: GraphQLObjectType | GraphQLInterfaceType) -> bool:
        mutation = self.schema.mutation_type
        if parent.name not in self._writable:
            self._writable[parent.name] = mutation is not None and (
                parent is mutation
                or (
                    isinstance(parent, GraphQLInterfaceType)
                    and self.schema.is_sub_type(parent, mutation)
                )
            )
        return self._writable[parent.name]

    def _count(self, field: GraphQLField, node: FieldNode) -> int:
        count = self._inside(field, node) if field.args else 0
        for directive in node.directives or ():
            definition = self.schema.get_directive(directive.name.value)
            if definition is not None and definition.args:
                count += self._inside(definition, directive)
        return count

    def _inside(
        self,
        definition: GraphQLField | GraphQLDirective,
        node: FieldNode | DirectiveNode,
    ) -> int:
        try:
            values = get_argument_values(definition, node, self.variables)
        except GraphQLError:
            # Execution raises the same error before the field runs.
            return 0
        most = self.max_values + 2
        return sum(_size(value, most) - 1 for value in values.values())


def _size(value: Any, most: int) -> int:
    """How many objects, lists and scalars *value* holds, counting no further
    than *most*."""
    pending = [value]
    count = 0
    while pending and count < most:
        item = pending.pop()
        count += 1
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list | tuple):
            pending.extend(item)
    return count


def _used(
    operation: OperationDefinitionNode, fragments: Mapping[str, FragmentDefinitionNode]
) -> set[str]:
    """The names of the fragments *operation* spreads, directly or not."""
    used: set[str] = set()
    pending = [operation.selection_set]
    while pending:
        for selection in pending.pop().selections:
            if isinstance(selection, FragmentSpreadNode):
                name = selection.name.value
                if name in fragments and name not in used:
                    used.add(name)
                    pending.append(fragments[name].selection_set)
            elif selection.selection_set is not None:
                pending.append(selection.selection_set)
    return used


def _depth(
    selection_set: SelectionSetNode | None,
    fragments: Mapping[str, FragmentDefinitionNode],
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
    fragments: Mapping[str, FragmentDefinitionNode],
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
    fragments: Mapping[str, FragmentDefinitionNode],
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
    fragments: Mapping[str, FragmentDefinitionNode],
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
