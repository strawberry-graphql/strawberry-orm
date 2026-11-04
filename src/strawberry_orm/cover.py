"""Cut generated inputs to what a schema's types show, one schema at a time.

Filters, ordering, group-by and aggregates are generated from the model, which
knows every column, while each GraphQL type chooses the columns a caller may
read. Which types an input serves depends on the schema: the types it is
attached to there, and the types its relations lead to through ``object:``.
So the cut is worked out when a schema is built and applied to that schema
alone, the way tags are, leaving the shared input classes as they were built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class InputCutError(ValueError):
    """A schema reaches a generated type the cut leaves with nothing in it."""


@dataclass(frozen=True)
class InputCut:
    """The fields a schema keeps of one generated type, and why none may be left."""

    keep: frozenset[str]
    #: Raised if the schema reaches the type and none of its fields are kept.
    if_empty: str | None = None


def named_type(type_: Any) -> Any:
    """The class a field's type names, through lists, optionals, lazy types and
    connections."""
    from strawberry.types.base import StrawberryContainer
    from strawberry.types.lazy_type import LazyType

    while True:
        if isinstance(type_, LazyType):
            type_ = type_.resolve_type()
        elif isinstance(type_, StrawberryContainer):
            type_ = type_.of_type
        elif isinstance(type_, type) and getattr(type_, "_node_type", None):
            type_ = type_._node_type
        else:
            return type_


def reached_types(schema: Any) -> set[Any]:
    """The classes behind every type *schema* reaches."""
    origins = (
        getattr(concrete.definition, "origin", None)
        for concrete in schema.schema_converter.type_map.values()
    )
    return {origin for origin in origins if origin is not None}


def covered_schema_class(base: type, cuts: dict[Any, InputCut]) -> type:
    """A subclass of *base* that keeps only the fields *cuts* allows."""
    return type(base.__name__, (_CoveredSchema, base), {"_orm_cuts": cuts})


class _CoveredSchema:
    """Mixed in ahead of a ``strawberry.Schema`` by :func:`covered_schema_class`."""

    _orm_cuts: dict[Any, InputCut]

    def get_fields(self, type_definition: Any) -> list[Any]:
        fields = super().get_fields(type_definition)  # type: ignore[misc]
        cut = self._orm_cuts.get(type_definition.origin)
        if cut is None:
            return fields
        kept = [field for field in fields if field.python_name in cut.keep]
        if not kept and cut.if_empty is not None:
            raise InputCutError(cut.if_empty)
        return kept


def build_covered(schema_class: type, cuts: dict[Any, InputCut], **kwargs: Any) -> Any:
    """Build *schema_class* with *cuts* applied.

    graphql-core reads a type's fields lazily and reports a failure as a
    ``TypeError`` about the type, so the reason is taken back out of it.
    """
    try:
        return covered_schema_class(schema_class, cuts)(**kwargs)
    except TypeError as error:
        if isinstance(error.__cause__, InputCutError):
            raise error.__cause__ from None
        raise
