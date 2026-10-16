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
from collections.abc import Iterable, Mapping
from typing import Any

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
        kept = []
        for field in fields:
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
        item_tags = tags_of(item)
        return item_tags is None or bool(item_tags & self._orm_tags)

    def _orm_without_hidden_arguments(self, type_definition: Any, field: Any) -> Any:
        """*field*, or a copy of it without the arguments this schema hides.

        One copy per field, so every lookup in this schema sees the same field.
        """
        hidden = [arg for arg in field.arguments if not self._orm_includes(arg)]
        if not hidden:
            return field
        copies = self.__dict__.setdefault("_orm_field_copies", {})
        if id(field) in copies:
            return copies[id(field)][1]
        for argument in hidden:
            if _required_argument(field, argument):
                raise ValueError(
                    f"{type_definition.name}.{field.python_name}"
                    f"({argument.python_name}:) is tagged "
                    f"{_listed(tags_of(argument))} and has no default, so the "
                    f"schema for {_listed(self._orm_tags)} could never call it. "
                    f"Give the argument a default, or tag the field instead."
                )
        clone = copy.copy(field)
        # The optimizer and resolvers read the library's own markers off the
        # field, which Strawberry's copy does not carry.
        for name, value in vars(field).items():
            if name.startswith("_orm"):
                setattr(clone, name, value)
        clone.arguments = [arg for arg in field.arguments if self._orm_includes(arg)]
        copies[id(field)] = (field, clone)
        return clone


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
    return parameter is None or parameter.default is inspect.Parameter.empty
