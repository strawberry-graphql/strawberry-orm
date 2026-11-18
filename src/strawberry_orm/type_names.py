"""One type per name: a schema refuses to serve a field a type not its own.

Strawberry keeps one type per GraphQL name. Two classes with the same
qualified name it takes for one type, so that a module imported twice still
builds, and it publishes whichever it converts first for both. The library
builds its types with ``type()``, naming each after a model, a node type, a
resolver or a name it is given, so two it builds differently under one name
share a qualified name as well. A field typed with the one left out would be
served the other: its fields, its arguments, what it is cut to.
"""

from __future__ import annotations

from typing import Any

from strawberry.schema.schema_converter import GraphQLCoreConverter
from strawberry.types.base import (
    StrawberryContainer,
    StrawberryObjectDefinition,
    get_object_definition,
)
from strawberry.types.lazy_type import LazyType
from strawberry.types.union import StrawberryUnion

_BACKREF = GraphQLCoreConverter.DEFINITION_BACKREF


def refuse_merged_types(schema: Any) -> set[Any]:
    """Raise if *schema* serves a field a type other than its own of that name.

    Every field, argument and input field the schema converted, and every
    interface, is checked against the type the schema publishes under its
    name. The classes served in place of one they do not differ from are
    returned, so that they can be cut like the rest.
    """
    converter = schema.schema_converter
    compare = _Comparison(schema)
    merged: set[Any] = set()
    for where, type_ in _uses(converter.type_map):
        for used in _definitions(type_):
            name = compare.name(used)
            published = converter.type_map[name].definition
            if published.origin is used.origin:
                continue
            difference = compare.definitions(published, used)
            if difference is not None:
                raise TypeError(
                    f"{where} takes a {name} other than the one the schema "
                    f"publishes: {difference}. Strawberry keeps one type per "
                    f"name, so {where} would be served the published one. Build "
                    f"one {name} for both, or name them apart: with name= where "
                    f"their builder takes one, or for a grouped connection, "
                    f"with a connection type of its own to name its types after."
                )
            merged.add(used.origin)
    return merged


def _uses(type_map: dict[str, Any]) -> list[tuple[str, Any]]:
    """Each type a converted field, argument or interface names, and where."""
    uses = []
    for owner, concrete in type_map.items():
        for interface in getattr(concrete.definition, "interfaces", ()):
            uses.append((owner, interface.origin))
        for name, field in getattr(concrete.implementation, "fields", {}).items():
            declared = field.extensions[_BACKREF]
            uses.append(
                (
                    f"{owner}.{name}",
                    declared.resolve_type(type_definition=concrete.definition),
                )
            )
            for argument_name, argument in getattr(field, "args", {}).items():
                uses.append(
                    (
                        f"{owner}.{name}({argument_name}:)",
                        argument.extensions[_BACKREF].type,
                    )
                )
    return uses


def _definitions(type_: Any) -> list[StrawberryObjectDefinition]:
    """The object, interface and input definitions *type_* names."""
    type_ = _resolved(type_)
    if isinstance(type_, StrawberryContainer):
        return _definitions(type_.of_type)
    if isinstance(type_, StrawberryUnion):
        return [d for member in type_.types for d in _definitions(member)]
    definition = get_object_definition(type_)
    return [definition] if definition is not None else []


def _resolved(type_: Any) -> Any:
    return type_.resolve_type() if isinstance(type_, LazyType) else type_


class _Comparison:
    """Whether two definitions of one name publish the same type in *schema*."""

    def __init__(self, schema: Any) -> None:
        self._schema = schema
        self._names = schema.config.name_converter
        self._compared: set[tuple[int, int]] = set()

    def name(self, definition: StrawberryObjectDefinition) -> str:
        return self._names.from_type(definition)

    def definitions(
        self, published: StrawberryObjectDefinition, other: StrawberryObjectDefinition
    ) -> str | None:
        """How *other* differs from *published*, or ``None`` if it does not.

        Fields are compared as the schema serves them, cut and tagged.
        """
        pair = (id(published), id(other))
        if published.origin is other.origin or pair in self._compared:
            return None
        self._compared.add(pair)
        ours = self._fields(published)
        theirs = self._fields(other)
        for name in sorted(ours.keys() ^ theirs.keys()):
            return f"only {'it' if name in theirs else 'the published one'} has {name}"
        for name, (field, type_) in ours.items():
            difference = self._types(type_, theirs[name][1]) or self._field(
                field, theirs[name][0], published.is_input
            )
            if difference is not None:
                return f"its {name} {difference}"
        return None

    def _fields(self, definition: StrawberryObjectDefinition) -> dict[str, Any]:
        """Each field the schema serves of *definition*, with its type there."""
        return {
            self._names.from_field(field): (
                field,
                field.resolve_type(type_definition=definition),
            )
            for field in self._schema.get_fields(definition)
        }

    def _field(self, published: Any, other: Any, is_input: bool) -> str | None:
        if is_input and published.default != other.default:
            return "defaults to another value"
        ours = {self._names.from_argument(a): a for a in published.arguments}
        theirs = {self._names.from_argument(a): a for a in other.arguments}
        if ours.keys() != theirs.keys():
            return "takes other arguments"
        for name, argument in ours.items():
            if (
                self._types(argument.type, theirs[name].type) is not None
                or argument.default != theirs[name].default
            ):
                return f"takes another {name}"
        return None

    def _types(self, published: Any, other: Any) -> str | None:
        published, other = _resolved(published), _resolved(other)
        if isinstance(published, StrawberryContainer) or isinstance(
            other, StrawberryContainer
        ):
            if type(published) is not type(other):
                return "is a list or nullable on only one of them"
            return self._types(published.of_type, other.of_type)
        ours, theirs = get_object_definition(published), get_object_definition(other)
        if ours is None or theirs is None:
            return None if published == other else "holds another type"
        name = self.name(ours)
        if name != self.name(theirs):
            return f"holds {self.name(theirs)}, not {name}"
        difference = self.definitions(ours, theirs)
        return None if difference is None else f"holds another {name}: {difference}"
