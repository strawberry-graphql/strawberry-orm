from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Callable

import strawberry


@strawberry.enum
class Ordering(enum.Enum):
    ASC = "ASC"
    ASC_NULLS_FIRST = "ASC_NULLS_FIRST"
    ASC_NULLS_LAST = "ASC_NULLS_LAST"
    DESC = "DESC"
    DESC_NULLS_FIRST = "DESC_NULLS_FIRST"
    DESC_NULLS_LAST = "DESC_NULLS_LAST"


@strawberry.type
class OperationMessage:
    kind: str
    field: str | None = None
    message: str = ""


@strawberry.type
class OperationInfo:
    messages: list[OperationMessage]


auto = strawberry.auto


@dataclass
class FieldDefinition:
    """Metadata attached to fields created via orm.field()."""

    load: list[Any] | Callable[..., Any] | None = None
    only: list[str] | None = None
    compute: dict[str, Any] | None = None
    disable_optimization: bool = False
    permission_classes: list[type] | None = None
    description: str | None = None

    def to_hints(self) -> FieldHints:
        """Return the optimizer-relevant subset as a :class:`FieldHints`."""
        return FieldHints(
            load=self.load,
            only=self.only,
            compute=self.compute,
            disable_optimization=self.disable_optimization,
        )


@dataclass
class FieldHints:
    """Optimization hints for a single field (subset of FieldDefinition)."""

    load: list[Any] | Callable[..., Any] | None = None
    only: list[str] | None = None
    compute: dict[str, Any] | None = None
    disable_optimization: bool = False


UNSET = strawberry.UNSET
