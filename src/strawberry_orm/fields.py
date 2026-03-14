"""Field factory helpers for strawberry-orm."""

from __future__ import annotations

from typing import Any

from strawberry_orm.types import FieldDefinition


def make_field(
    *,
    load: list[Any] | None = None,
    only: list[str] | None = None,
    compute: dict[str, Any] | None = None,
    disable_optimization: bool = False,
    filter_input: type | None = None,
    order_by: type | None = None,
    pagination: bool = False,
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
        filter_input=filter_input,
        order_by=order_by,
        pagination=pagination,
        permission_classes=permission_classes,
        description=description,
    )
