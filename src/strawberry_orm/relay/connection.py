"""ORM-agnostic connection types built on strawberry.relay."""

from __future__ import annotations

from typing import Any, ClassVar, Generic, Iterable, TypeVar

import strawberry
from strawberry import relay
from strawberry.relay import Connection, Edge, ListConnection, PageInfo

NodeType = TypeVar("NodeType", bound=relay.Node)


class ORMListConnection(ListConnection[NodeType]):
    """A ListConnection that works with any ORM backend.

    Subclass this to specialize cursor-based pagination for a particular
    backend (e.g. build WHERE clauses for efficient cursor seek).  The
    default implementation delegates to strawberry's built-in
    ``ListConnection`` which handles offset-based slicing of any iterable.
    """

    pass


__all__ = [
    "Connection",
    "Edge",
    "ListConnection",
    "NodeType",
    "ORMListConnection",
    "PageInfo",
]
