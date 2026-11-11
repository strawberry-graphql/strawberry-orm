"""The operations a server exposes as tools, read from a GraphQL document."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from graphql import (
    DocumentNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLSchema,
    OperationDefinitionNode,
    parse,
    print_ast,
    validate,
)
from graphql.language import Visitor, visit
from strawberry.types.graphql import OperationType

from .checks import strip_descriptions
from .json_schema import input_schema, output_schema


@dataclass(frozen=True)
class Operation:
    """One named operation, ready to run as a tool."""

    name: str
    kind: OperationType
    description: str
    #: The operation with only the fragments it uses, and no descriptions.
    document: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


def read_operations(schema: GraphQLSchema, source: str) -> list[Operation]:
    """Every operation in *source*, checked against *schema*.

    Raises :class:`ValueError` for a document that does not parse or
    validate, an anonymous operation, or a subscription, so a schema change
    that breaks a tool fails when the server is built.
    """
    try:
        document = parse(source)
    except GraphQLError as error:
        raise ValueError(f"The operations do not parse: {error.message}") from error
    errors = validate(schema, document)
    if errors:
        listed = "\n".join(f"- {e.message}" for e in errors)
        raise ValueError(f"The operations do not validate:\n{listed}")
    fragments = {
        d.name.value: d
        for d in document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }
    operations = []
    for definition in document.definitions:
        if not isinstance(definition, OperationDefinitionNode):
            continue
        if definition.name is None:
            raise ValueError(
                "Every operation needs a name, which becomes its tool's name."
            )
        name = definition.name.value
        kind = OperationType(definition.operation.value)
        if kind is OperationType.SUBSCRIPTION:
            raise ValueError(f"{name} is a subscription; tools cannot stream.")
        description = (
            definition.description.value
            if definition.description is not None
            else f"Runs the GraphQL {kind.value} {name}."
        )
        used = _fragments_used(definition, fragments)
        own = DocumentNode(
            definitions=(definition, *(fragments[n] for n in sorted(used)))
        )
        operations.append(
            Operation(
                name=name,
                kind=kind,
                description=description,
                document=print_ast(strip_descriptions(own)),
                input_schema=input_schema(schema, definition),
                output_schema=output_schema(schema, definition, fragments),
            )
        )
    return operations


def _fragments_used(
    node: OperationDefinitionNode | FragmentDefinitionNode,
    fragments: dict[str, FragmentDefinitionNode],
) -> set[str]:
    used: set[str] = set()
    pending = [node]
    while pending:
        spreads = _Spreads()
        visit(pending.pop(), spreads)
        for name in spreads.names - used:
            used.add(name)
            pending.append(fragments[name])
    return used


class _Spreads(Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def enter_fragment_spread(self, node: FragmentSpreadNode, *_: Any) -> None:
        self.names.add(node.name.value)
