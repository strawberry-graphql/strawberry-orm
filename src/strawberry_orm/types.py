from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import strawberry


@strawberry.enum
class Ordering(enum.Enum):
    ASC = "ASC"
    ASC_NULLS_FIRST = "ASC_NULLS_FIRST"
    ASC_NULLS_LAST = "ASC_NULLS_LAST"
    DESC = "DESC"
    DESC_NULLS_FIRST = "DESC_NULLS_FIRST"
    DESC_NULLS_LAST = "DESC_NULLS_LAST"


@strawberry.enum
class DateGroupByInterval(enum.Enum):
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    QUARTER = "QUARTER"
    YEAR = "YEAR"


@strawberry.input
class DateGroupByOption:
    interval: DateGroupByInterval


@strawberry.type
class OperationMessage:
    kind: str
    field: str | None = None
    message: str = ""


@strawberry.type
class OperationInfo:
    messages: list[OperationMessage]


auto = strawberry.auto


def _own_annotations(owner: type) -> dict[str, Any] | None:
    """The annotations declared on *owner* itself, or ``None`` if it has none.

    From 3.14 annotations are lazy (PEP 649), so a class that has them may
    carry ``__annotate__`` and no ``__annotations__`` entry yet. Reading
    ``__dict__`` alone reports nothing in that case, and writing a fresh dict
    back would drop every annotation the class declared.
    """
    existing = owner.__dict__.get("__annotations__")
    if existing is not None:
        return existing
    try:
        import annotationlib
    except ImportError:  # pragma: no cover - Python < 3.14
        return None

    # Mid-class-creation the annotations are reachable through neither
    # ``__annotations__`` nor ``__annotate__``, so ask for them directly.
    # FORWARDREF so a reference to a type defined later resolves to a
    # ForwardRef rather than raising, which is normal in these schemas.
    # Exercised on 3.14 runs; the coverage gate runs on 3.12, where the import
    # above has already returned.
    return (  # pragma: no cover
        annotationlib.get_annotations(owner, format=annotationlib.Format.FORWARDREF)
        or None
    )


@dataclass
class FieldDefinition:
    """Metadata attached to fields created via orm.field()."""

    using: list[str] | None = None
    scope: Callable[..., Any] | None = None
    compute: dict[str, Any] | None = None
    disable_optimization: bool = False
    on: str | None = None
    permission_classes: list[type] | None = None
    description: str | None = None
    declared_type: Any = None

    def __set_name__(self, owner: type, name: str) -> None:
        """Publish the field's type when it came from a decorated function.

        ``@orm.field.scoped`` decorates the scope, not the resolver, so the
        GraphQL type lives on the function's return annotation rather than on
        a class annotation. This runs while the class is being created, which
        is before ``@orm.type`` reads annotations.
        """
        annotations = _own_annotations(owner)
        if self.declared_type is not None:
            if annotations is None:
                annotations = {}
            annotations.setdefault(name, self.declared_type)
            # Assign rather than mutate: the dict may have been materialised
            # from a lazy annotation function, in which case nothing is
            # watching it.
            owner.__annotations__ = annotations
        elif self.scope is not None and name not in (annotations or {}):
            raise TypeError(
                f"{owner.__name__}.{name} has no type. Annotate the attribute "
                f"({name}: list[SomeType] = orm.field.scoped(...)), or give the "
                f"decorated function a return annotation."
            )

    def to_hints(self) -> FieldHints:
        """Return the optimizer-relevant subset as a :class:`FieldHints`."""
        return FieldHints(
            using=self.using,
            scope=self.scope,
            compute=self.compute,
            disable_optimization=self.disable_optimization,
            on=self.on,
        )


@dataclass
class FieldHints:
    """Optimization hints for a single field (subset of FieldDefinition).

    ``using`` names the relations this field is served with - they are
    eager-loaded alongside the parent query. ``scope`` narrows the rows loaded
    through this relation edge. ``on`` names the relation the field is served
    from, for when the GraphQL field is called something else.
    """

    using: list[str] | None = None
    scope: Callable[..., Any] | None = None
    compute: dict[str, Any] | None = None
    disable_optimization: bool = False
    on: str | None = None


UNSET = strawberry.UNSET
