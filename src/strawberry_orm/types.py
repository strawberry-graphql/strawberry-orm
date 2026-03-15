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
    filter_input: type | None = None
    order_by: type | None = None
    pagination: bool = False
    permission_classes: list[type] | None = None
    description: str | None = None


UNSET = strawberry.UNSET
