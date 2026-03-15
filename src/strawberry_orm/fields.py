"""Field factory helpers for strawberry-orm."""

from __future__ import annotations

from typing import Any, Callable

from strawberry_orm.types import FieldDefinition


def make_field(
    *,
    load: list[Any] | Callable[..., Any] | None = None,
    only: list[str] | None = None,
    compute: dict[str, Any] | None = None,
    disable_optimization: bool = False,
    permission_classes: list[type] | None = None,
    description: str | None = None,
) -> FieldDefinition:
    """Create a :class:`FieldDefinition` that backends translate into a
    Strawberry field descriptor with optimizer hints attached."""
    return FieldDefinition(
        load=load,
        only=only,
        compute=compute,
        disable_optimization=disable_optimization,
        permission_classes=permission_classes,
        description=description,
    )
