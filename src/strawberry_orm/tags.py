"""Tag fields with the schemas they belong to, and build a schema for some tags.

A field, argument or input field without tags is in every schema. A tagged one
is in a schema built for any of its tags::

    email: auto = orm.field.eager(tags=["internal"])

    @strawberry.field(metadata=tags("internal", "partner"))
    def audit(self) -> str: ...

    internal = orm.schema(query=Query, tags="internal")
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from strawberry import UNSET
from strawberry.extensions import FieldExtension
from strawberry.types.base import StrawberryOptional

from strawberry_orm.cover import copy_field, named_type

#: The field-metadata key tags are stored under.
TAGS_KEY = "strawberry_orm.tags"


def tags(*names: str) -> dict[str, frozenset[str]]:
    """Field metadata placing a field in the schemas built for *names*.

    For fields declared with Strawberry itself: ``strawberry.field``,
    ``strawberry.mutation``, ``strawberry.argument`` and input fields.
    """
    return {TAGS_KEY: _tag_set(names, "tags()")}


def tags_of(item: Any) -> frozenset[str] | None:
    """The tags on a field or argument, or ``None`` when it has none."""
    return (getattr(item, "metadata", None) or {}).get(TAGS_KEY)


def field_metadata(
    metadata: Mapping[Any, Any] | None, tag_names: str | Iterable[str] | None
) -> dict[Any, Any] | None:
    """Merge an ORM helper's ``metadata=`` and ``tags=`` into one mapping."""
    if tag_names is None:
        return dict(metadata) if metadata else None
    merged = dict(metadata or {})
    merged[TAGS_KEY] = merged.get(TAGS_KEY, frozenset()) | _tag_set(tag_names, "tags=")
    return merged


def _tag_set(names: str | Iterable[str], where: str) -> frozenset[str]:
    names = (names,) if isinstance(names, str) else tuple(names)
    if not names or not all(isinstance(name, str) and name for name in names):
        raise ValueError(f"{where} needs one or more tag names, got {names!r}")
    return frozenset(names)


def _listed(names: frozenset[str]) -> str:
    return ", ".join(sorted(names))


def tagged_schema_class(base: type, tag_names: str | Iterable[str]) -> type:
    """A subclass of *base* that keeps only what is untagged or tagged *tag_names*."""
    selected = _tag_set(tag_names, "orm.schema(tags=)")
    return type(base.__name__, (_TaggedSchema, base), {"_orm_tags": selected})


class _TaggedSchema:
    """Mixed in ahead of a ``strawberry.Schema`` by :func:`tagged_schema_class`."""

    _orm_tags: frozenset[str]

    def get_fields(self, type_definition: Any) -> list[Any]:
        fields = super().get_fields(type_definition)  # type: ignore[misc]
        built = self.__dict__.get("_orm_built_inputs", {})
        dropped = built.get(type_definition.origin, ())
        kept = []
        for field in fields:
            if field.python_name in dropped:
                continue
            if self._orm_includes(field):
                kept.append(self._orm_without_hidden_arguments(type_definition, field))
            elif type_definition.is_input and _required_input_field(field):
                raise ValueError(
                    f"{type_definition.name}.{field.python_name} is tagged "
                    f"{_listed(tags_of(field))} and has no default, so the schema "
                    f"for {_listed(self._orm_tags)} could never build "
                    f"{type_definition.name}. Give the field a default, or tag the "
                    f"field that takes {type_definition.name} instead."
                )
        if fields and not kept:
            raise ValueError(
                f"Every field of {type_definition.name} is tagged out of the "
                f"schema for {_listed(self._orm_tags)}. Tag the fields that "
                f"return {type_definition.name} instead, so the type is left "
                f"out with them."
            )
        return kept

    def _orm_includes(self, item: Any) -> bool:
        return _selects(self._orm_tags, item)

    def _orm_without_hidden_arguments(self, type_definition: Any, field: Any) -> Any:
        """*field*, or a copy of it without the arguments this schema hides.

        One copy per field, so every lookup in this schema sees the same field.
        Extensions set a field's arguments as the schema converts it, and may
        add tagged ones or rebuild the resolver's without their metadata, so
        a field with extensions is tried on a throwaway copy first, and a copy
        converted for this schema ends with an extension that drops whatever
        hidden argument the others left it with.
        """
        copies = self.__dict__.setdefault("_orm_field_copies", {})
        if id(field) in copies:
            return copies[id(field)][1]
        arguments = _arguments(field)
        declared = _declared_arguments(field, arguments)
        hidden = list(
            {
                arg.python_name: arg
                for arg in [*declared, *arguments]
                if not self._orm_includes(arg)
            }.values()
        )
        if not hidden and field.extensions:
            hidden = [
                arg for arg in _extended_arguments(field) if not self._orm_includes(arg)
            ]
        if not hidden:
            copies[id(field)] = (field, field)
            return field
        for argument in hidden:
            if _required_argument(field, argument):
                raise ValueError(
                    f"{type_definition.name}.{field.python_name}"
                    f"({argument.python_name}:) is tagged "
                    f"{_listed(tags_of(argument))} and has no default, so the "
                    f"schema for {_listed(self._orm_tags)} could never call it. "
                    f"Give the argument a default, or tag the field instead."
                )
        clone = copy_field(field)
        clone.arguments = [arg for arg in arguments if self._orm_includes(arg)]
        if field.extensions:
            names = {arg.python_name for arg in hidden}
            if field.base_resolver is not None:
                clone.base_resolver = _resolver_without(field.base_resolver, names)
            clone.extensions = [
                *_own_extensions(field),
                _DropHiddenArguments(
                    self._orm_tags,
                    names,
                    {named_type(arg.type) for arg in declared},
                    self.__dict__.setdefault("_orm_built_inputs", {}),
                ),
            ]
        copies[id(field)] = (field, clone)
        return clone


class _DropHiddenArguments(FieldExtension):
    """The last extension of a field copy a tagged schema converts.

    Whatever arguments the field's own extensions leave it with, it drops
    those the schema hides and those named after a hidden argument, and
    keeps them out of what the resolver is called with. An input an
    extension built from the resolver's parameters loses the hidden ones.
    """

    def __init__(
        self,
        selected: frozenset[str],
        names: set[str],
        declared: set[Any],
        built_inputs: dict[Any, set[str]],
    ) -> None:
        self._selected = selected
        self._names = names
        self._declared = declared
        self._built_inputs = built_inputs

    def apply(self, field: Any) -> None:
        self._names.update(
            arg.python_name
            for arg in field.arguments
            if not _selects(self._selected, arg)
        )
        field.arguments = [
            arg for arg in field.arguments if arg.python_name not in self._names
        ]
        resolver = field.base_resolver
        parameters = set(resolver.signature.parameters) if resolver else set()
        for argument in field.arguments:
            built = named_type(argument.type)
            definition = getattr(built, "__strawberry_definition__", None)
            if (
                definition is None
                or not definition.is_input
                or built in self._declared
                or not {f.python_name for f in definition.fields} <= parameters
            ):
                continue
            self._built_inputs.setdefault(built, set()).update(self._names)

    def map_arguments(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        for name in self._names:
            kwargs.pop(name, None)
        return kwargs


def served(schema: Any) -> Callable[[Any], Any]:
    """The field *schema* serves for a declared one: its copy, if it made one."""
    copies = schema.__dict__.get("_orm_field_copies", {})
    return lambda field: copies.get(id(field), (field, field))[1]


def _selects(selected: frozenset[str], item: Any) -> bool:
    item_tags = tags_of(item)
    return item_tags is None or bool(item_tags & selected)


def _extended_arguments(field: Any) -> list[Any]:
    """The arguments *field*'s extensions would leave it with, worked out on a copy."""
    probe = copy_field(field)
    if probe.base_resolver is not None:
        # Strawberry hands out the resolver's own list, which extensions append to.
        probe.arguments = list(probe.arguments)
    probe.extensions = _own_extensions(field)
    for extension in probe.extensions:
        extension.apply(probe)
    return probe.arguments


def _own_extensions(field: Any) -> list[Any]:
    """Copies of *field*'s extensions, for a copy of the field.

    An extension may keep what it built for a field on itself and hand it out
    again, so one that a copy for one schema shares would give the field the
    arguments it was cut to there in every schema built after.
    """
    return [copy.copy(extension) for extension in field.extensions]


def _resolver_without(resolver: Any, names: set[str]) -> Any:
    """A copy of *resolver* that does not declare *names*.

    ``InputMutationExtension`` and others build from ``resolver.arguments``.
    """
    clone = copy.copy(resolver)
    clone.__dict__["arguments"] = [
        arg for arg in resolver.arguments if arg.python_name not in names
    ]
    return clone


def _arguments(field: Any) -> list[Any]:
    """*field*'s arguments, read off a copy if an extension may yet set them.

    Strawberry keeps the list it reads first, and Relay's ``node()`` gives its
    field the resolver that declares ``id`` only as the schema converts it.
    """
    return copy.copy(field).arguments if field.extensions else field.arguments


def _declared_arguments(field: Any, arguments: list[Any]) -> list[Any]:
    """The arguments *field*'s resolver declares, or else its *arguments*.

    Every schema converting a field applies its extensions to the field itself,
    and they may replace its arguments with an input built from them or with
    copies that lost their tags, so once one schema is built a field's own
    arguments no longer say what its resolver declared.
    """
    resolver = field.base_resolver
    return list(resolver.arguments) if resolver is not None else arguments


def _required_input_field(field: Any) -> bool:
    return (
        field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING
    )


def _required_argument(field: Any, argument: Any) -> bool:
    resolver = field.base_resolver
    parameter = (
        resolver.signature.parameters.get(argument.python_name) if resolver else None
    )
    if parameter is None:
        # An argument an extension added: the extension reads it, if sent.
        return argument.default is UNSET and not isinstance(
            argument.type, StrawberryOptional
        )
    return parameter.default is inspect.Parameter.empty
