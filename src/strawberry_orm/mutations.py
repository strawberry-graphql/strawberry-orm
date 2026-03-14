"""Mutation helpers and the orm.ref() related-list factory."""

from __future__ import annotations

from typing import Any

import strawberry


def make_ref_type(
    model: type,
    *,
    create: type | None = None,
    update: type | None = None,
    delete: bool = False,
    name: str | None = None,
) -> type:
    """Generate a ``@oneOf`` input type for managing a related list.

    The returned Strawberry input has up to four ``@oneOf`` variants:
    - ``id`` (always): link an existing object by ID.
    - ``create`` (opt-in): create a new object inline.
    - ``update`` (opt-in): update an existing object in-place (input must have ``id``).
    - ``delete`` (opt-in): unlink AND delete an existing object.
    """
    type_name = name or f"{model.__name__}Ref"
    annotations: dict[str, Any] = {
        "id": strawberry.ID | None,
    }
    defaults: dict[str, Any] = {
        "id": strawberry.UNSET,
    }

    if create is not None:
        annotations["create"] = create | None
        defaults["create"] = strawberry.UNSET

    if update is not None:
        annotations["update"] = update | None
        defaults["update"] = strawberry.UNSET

    if delete:
        delete_type_name = f"Delete{model.__name__}Input"
        delete_type = _make_delete_input(delete_type_name)
        annotations["delete"] = delete_type | None
        defaults["delete"] = strawberry.UNSET

    ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
    cls = type(type_name, (), ns)
    return strawberry.input(cls, one_of=True)


def _make_delete_input(name: str) -> type:
    ns: dict[str, Any] = {
        "__annotations__": {"id": strawberry.ID},
    }
    cls = type(name, (), ns)
    return strawberry.input(cls)
