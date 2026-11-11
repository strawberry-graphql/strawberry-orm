"""JSON Schemas for a GraphQL operation: its variables in, its data out."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLEnumType,
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLOutputType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLString,
    InlineFragmentNode,
    OperationDefinitionNode,
    SchemaMetaFieldDef,
    SelectionSetNode,
    TypeMetaFieldDef,
    Undefined,
    ast_from_value,
    type_from_ast,
    value_from_ast_untyped,
)
from strawberry.schema_directives import OneOf

_SCALARS: dict[str, dict[str, Any]] = {
    "Int": {"type": "integer"},
    "Float": {"type": "number"},
    "String": {"type": "string"},
    "Boolean": {"type": "boolean"},
}

_INTROSPECTION = {"__schema": SchemaMetaFieldDef, "__type": TypeMetaFieldDef}

_ERROR = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "path": {"type": "array", "items": {"type": ["string", "integer"]}},
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "column": {"type": "integer"},
                },
            },
        },
        "extensions": {"type": "object"},
    },
    "required": ["message"],
}


def input_schema(
    schema: GraphQLSchema, operation: OperationDefinitionNode
) -> dict[str, Any]:
    """The operation's variables as a tool's arguments."""
    defs: dict[str, dict[str, Any]] = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for definition in operation.variable_definitions or ():
        name = definition.variable.name.value
        type_ = type_from_ast(schema, definition.type)
        assert type_ is not None, "the document was validated"
        prop = _input(type_, defs)
        if definition.description is not None:
            prop = {**prop, "description": definition.description.value}
        if definition.default_value is not None:
            prop = {**prop, "default": value_from_ast_untyped(definition.default_value)}
        elif isinstance(type_, GraphQLNonNull):
            required.append(name)
        properties[name] = prop
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    if defs:
        result["$defs"] = defs
    return result


def output_schema(
    schema: GraphQLSchema,
    operation: OperationDefinitionNode,
    fragments: dict[str, FragmentDefinitionNode],
) -> dict[str, Any]:
    """The ``{"data": ..., "errors": [...]}`` the operation returns."""
    root = (
        schema.mutation_type
        if operation.operation.value == "mutation"
        else schema.query_type
    )
    assert root is not None, "the document was validated"
    data = _Selections(schema, fragments).object(root, [operation.selection_set])
    return {
        "type": "object",
        "properties": {
            "data": _nullable(data),
            "errors": {"type": "array", "items": _ERROR},
        },
        "required": ["data"],
    }


def _input(type_: GraphQLInputType, defs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if isinstance(type_, GraphQLNonNull):
        return _input_non_null(type_.of_type, defs)
    return _nullable(_input_non_null(type_, defs))


def _input_non_null(
    type_: GraphQLInputType, defs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if isinstance(type_, GraphQLList):
        return {"type": "array", "items": _input(type_.of_type, defs)}
    if isinstance(type_, GraphQLInputObjectType):
        if type_.name not in defs:
            defs[type_.name] = {}
            defs[type_.name] = _input_object(type_, defs)
        return {"$ref": f"#/$defs/{type_.name}"}
    if type_.name == "ID":
        return {"type": ["string", "integer"]}
    return _leaf(type_)


def _input_object(
    type_: GraphQLInputObjectType, defs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, field in type_.fields.items():
        prop = _input(field.type, defs)
        if field.description:
            prop = {**prop, "description": field.description}
        default = _default(field.default_value, field.type)
        if default is not Undefined:
            prop = {**prop, "default": default}
        elif isinstance(field.type, GraphQLNonNull):
            required.append(name)
        properties[name] = prop
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    if _is_one_of(type_):
        result["minProperties"] = result["maxProperties"] = 1
    if type_.description:
        result["description"] = type_.description
    return result


def _is_one_of(type_: GraphQLInputObjectType) -> bool:
    """Strawberry marks ``one_of=True`` with its own directive, not graphql-core's."""
    definition = (type_.extensions or {}).get("strawberry-definition")
    directives = getattr(definition, "directives", None) or ()
    return type_.is_one_of or any(isinstance(d, OneOf) for d in directives)


def _default(value: Any, type_: GraphQLInputType) -> Any:
    if value is Undefined:
        return Undefined
    return value_from_ast_untyped(ast_from_value(value, type_))


def _leaf(type_: GraphQLNamedType) -> dict[str, Any]:
    if isinstance(type_, GraphQLEnumType):
        result: dict[str, Any] = {"type": "string", "enum": list(type_.values)}
    else:
        assert isinstance(type_, GraphQLScalarType)
        result = dict(_SCALARS.get(type_.name, {}))
    if type_.description and type_.name not in _SCALARS and type_.name != "ID":
        result["description"] = type_.description
    return result


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        return {"anyOf": [schema, {"type": "null"}]}
    if "type" not in schema:
        return schema
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    result = {**schema, "type": [*types, "null"]}
    if "enum" in schema:
        result["enum"] = [*schema["enum"], None]
    return result


class _Selections:
    """Output schemas for selection sets, following their fragments."""

    def __init__(
        self, schema: GraphQLSchema, fragments: dict[str, FragmentDefinitionNode]
    ) -> None:
        self.schema = schema
        self.fragments = fragments

    def object(
        self, parent: GraphQLNamedType, selection_sets: Iterable[SelectionSetNode]
    ) -> dict[str, Any]:
        fields: dict[str, _Field] = {}
        for selection_set in selection_sets:
            self._collect(parent, parent, selection_set, False, fields)
        properties = {key: self._field(field) for key, field in fields.items()}
        result: dict[str, Any] = {"type": "object", "properties": properties}
        required = [key for key, field in fields.items() if field.always]
        if required:
            result["required"] = required
        return result

    def _collect(
        self,
        parent: GraphQLNamedType,
        runtime: GraphQLNamedType,
        selection_set: SelectionSetNode,
        conditional: bool,
        fields: dict[str, _Field],
    ) -> None:
        for selection in selection_set.selections:
            sometimes = conditional or bool(selection.directives)
            if isinstance(selection, FieldNode):
                key = (selection.alias or selection.name).value
                type_, description = self._field_type(runtime, selection.name.value)
                field = fields.setdefault(key, _Field(description))
                field.add(type_, selection, always=not sometimes)
                continue
            if isinstance(selection, InlineFragmentNode):
                fragment_set = selection.selection_set
                condition = selection.type_condition
            else:
                assert isinstance(selection, FragmentSpreadNode)
                fragment = self.fragments[selection.name.value]
                fragment_set = fragment.selection_set
                condition = fragment.type_condition
            narrowed = runtime
            if condition is not None:
                named = self.schema.get_type(condition.name.value)
                assert named is not None, "the document was validated"
                narrowed = named
            # On an object type every valid fragment applies; on an abstract
            # one, a fragment on another type applies only to some objects.
            narrows = (
                not isinstance(parent, GraphQLObjectType)
                and narrowed.name != parent.name
            )
            self._collect(parent, narrowed, fragment_set, sometimes or narrows, fields)

    def _field_type(
        self, runtime: GraphQLNamedType, name: str
    ) -> tuple[GraphQLOutputType, str | None]:
        if name == "__typename":
            return GraphQLNonNull(GraphQLString), None
        definition = _INTROSPECTION.get(name) or runtime.fields[name]  # type: ignore[union-attr]
        return definition.type, definition.description

    def _field(self, field: _Field) -> dict[str, Any]:
        # Validation lets one key hold fields of different object types when
        # their fragments are on different members; each keeps its own shape.
        shapes = [
            self._output(type_, [n.selection_set for n in nodes if n.selection_set])
            for type_, nodes in field.nodes.values()
        ]
        result = shapes[0] if len(shapes) == 1 else {"anyOf": shapes}
        if field.description:
            result = {**result, "description": field.description}
        return result

    def _output(
        self, type_: GraphQLOutputType, selection_sets: list[SelectionSetNode]
    ) -> dict[str, Any]:
        if isinstance(type_, GraphQLNonNull):
            return self._output_non_null(type_.of_type, selection_sets)
        return _nullable(self._output_non_null(type_, selection_sets))

    def _output_non_null(
        self, type_: GraphQLOutputType, selection_sets: list[SelectionSetNode]
    ) -> dict[str, Any]:
        if isinstance(type_, GraphQLList):
            return {
                "type": "array",
                "items": self._output(type_.of_type, selection_sets),
            }
        if isinstance(type_, GraphQLScalarType | GraphQLEnumType):
            if type_.name == "ID":
                return {"type": "string"}
            return _leaf(type_)
        return self.object(type_, selection_sets)


class _Field:
    """One response key and the field nodes that fill it, by their type."""

    def __init__(self, description: str | None) -> None:
        self.description = description
        self.nodes: dict[str, tuple[GraphQLOutputType, list[FieldNode]]] = {}
        self.always = False

    def add(self, type_: GraphQLOutputType, node: FieldNode, *, always: bool) -> None:
        self.nodes.setdefault(str(type_), (type_, []))[1].append(node)
        self.always = self.always or always
