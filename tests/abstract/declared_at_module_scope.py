"""Types declared at module scope, named in annotations by strings.

``from __future__ import annotations`` leaves every annotation a string, and
a connection may name its node type as one. Strawberry looks such a name up in
the module that declared the class, so each function here declares its types
as this module's globals, the way an app's schema module does, and writes the
annotations naming them as strings.
"""

from typing import Any

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection


def _forget(*names: str) -> None:
    """Drop what an earlier call declared, as a fresh import would not have it."""
    for name in names:
        globals().pop(name, None)


def declare_node_types(orm: Any, models: Any, narrow: Any, allow: type) -> Any:
    """``PostNode`` and ``CommentNode``, then ``UserNode`` naming them, and a ``Query``.

    ``UserNode.posts`` is published posts with an ``order`` argument and a
    permission; ``UserNode.comments`` names its node type as a string.
    """
    global PostNode, CommentNode, UserNode, Query
    _forget("PostNode", "CommentNode", "UserNode")
    Post, Comment, User = models.Post, models.Comment, models.User

    @orm.type(Post)
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: str

        @classmethod
        def scope_rows(cls, query, info):
            return narrow.exclude(query, Post, "title", "Rust Adventures")

    @orm.type(Comment)
    class CommentNode(relay.Node):
        id: relay.NodeID[int]
        body: str

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: str
        posts: "list[PostNode]" = orm.field.eager(
            scope=lambda query, info: narrow.only(query, Post, "is_published", True),
            order=orm.order(Post),
            permission_classes=[allow],
        )
        comments: ORMListConnection["CommentNode"] = orm.connection.eager()

    @strawberry.type
    class Query:
        users: "list[UserNode]" = orm.field.eager()
        posts: "list[PostNode]" = orm.field.eager(filters=orm.filter(Post))
        page: "ORMListConnection[PostNode]" = orm.connection.eager()

    return Query


def declare_query_before_its_types(orm: Any, models: Any, field: str) -> Any:
    """A ``Query`` whose *field* names ``LaterNode``, declared after it."""
    global LaterNode, Query
    _forget("LaterNode")

    @strawberry.type
    class Query:
        if field == "list":
            items: "list[LaterNode]" = orm.field.eager()
        else:
            items: ORMListConnection["LaterNode"] = orm.connection.eager()

    @orm.type(models.Post)
    class LaterNode(relay.Node):
        id: relay.NodeID[int]
        title: str

    return Query


def declare_a_connection_before_its_node(orm: Any, models: Any) -> None:
    """``EarlyNode`` with a connection over ``LateNode``, declared after it."""
    global EarlyNode, LateNode
    _forget("LateNode")

    @orm.type(models.User)
    class EarlyNode(relay.Node):
        id: relay.NodeID[int]
        posts: ORMListConnection["LateNode"] = orm.connection.eager()

    @orm.type(models.Post)
    class LateNode(relay.Node):
        id: relay.NodeID[int]
