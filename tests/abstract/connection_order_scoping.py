"""A relation connection's ``order`` cannot rank rows the caller cannot read.

Ordering by a related row the related type's ``scope_rows`` hides ranks the
parent rows by values the caller is not allowed to see. ``orm.schema()``
refuses such an order input on every type over a model, not only the one
registered last, and a relation connection read in one batch for many parents
refuses it at query time, as the other read paths already do.

Backends supply:

``orm``                             a fresh ORM
``models``                          ``User``, ``Post`` and ``Comment``
``run(schema, query, variables)``   awaitable result of running *query*
``narrow``                          as in ``tests.abstract.filter_oracles``

The seed: Bob wrote "Nice post!" and Alice "Thanks!" on post 1; Charlie
"Great guide" on post 2.
"""

import pytest
import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


def _types(orm, models, narrow):
    User, Post, Comment = models.User, models.Post, models.Comment
    orm.order(User)
    comment_order = orm.order(Comment)

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

        @classmethod
        def scope_rows(cls, query, info):
            return narrow.exclude(query, User, "name", "Bob")

    @orm.type(Comment, order=comment_order)
    class CommentNode(relay.Node):
        id: relay.NodeID[int]
        body: auto
        author: UserNode | None

    @orm.type(Post)
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        comments: ORMListConnection[CommentNode] = orm.connection.eager()

    return CommentNode, PostNode


def _unchecked_schema(orm, models, narrow):
    """Built with ``strawberry.Schema``, which skips the build-time check."""
    _, PostNode = _types(orm, models, narrow)

    @strawberry.type
    class Query:
        posts: list[PostNode] = orm.field.auto()

    return strawberry.Schema(
        query=Query,
        extensions=[
            orm.relation_connection_extension(),
            orm.batching_extension(),
            orm.optimizer_extension(),
        ],
    )


def comment_page(order):
    return (
        f"{{ posts {{ title comments(first: 10, order: [{order}]) "
        "{ edges { node { body } } } } }"
    )


class AbstractTestConnectionOrderScoping:
    def test_schema_build_checks_the_order_of_every_type_over_a_model(
        self, orm, models, narrow
    ):
        CommentNode, PostNode = _types(orm, models, narrow)

        @orm.type(models.Comment, name="CommentAdminNode")
        class CommentAdminNode(relay.Node):
            id: relay.NodeID[int]
            body: auto

        @strawberry.type
        class Query:
            posts: list[PostNode] = orm.field.auto()
            admin_comments: ORMListConnection[CommentAdminNode] = orm.connection.eager()

        with pytest.raises(ValueError, match="Cannot order by Comment.author"):
            orm.schema(query=Query)

    def test_schema_build_checks_an_order_passed_to_a_field(self, orm, models, narrow):
        """An order handed to a field with ``order=``, and to no type, sorts the
        rows that field returns all the same."""
        User, Comment = models.User, models.Comment
        orm.order(User)
        comment_order = orm.order(Comment)

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

            @classmethod
            def scope_rows(cls, query, info):
                return narrow.exclude(
                    query, User, "name", "Bob"
                )  # pragma: no cover - never queried

        @orm.type(Comment)
        class CommentNode(relay.Node):
            id: relay.NodeID[int]
            body: auto
            author: UserNode | None

        @strawberry.type
        class Query:
            comments: list[CommentNode] = orm.field.eager(order=comment_order)

        with pytest.raises(ValueError, match="Cannot order by Comment.author"):
            orm.schema(query=Query)

    async def test_a_relation_connection_refuses_to_order_by_a_hidden_row(
        self, orm, models, narrow, run
    ):
        schema = _unchecked_schema(orm, models, narrow)
        result = await run(
            schema, comment_page("{ object: { author: { field: { name: ASC } } } }")
        )
        assert result.errors is not None
        assert "Cannot order by Comment.author" in str(result.errors[0].message)

    async def test_a_relation_connection_still_orders_by_its_own_columns(
        self, orm, models, narrow, run
    ):
        schema = _unchecked_schema(orm, models, narrow)
        result = await run(schema, comment_page("{ field: { body: DESC } }"))
        assert result.errors is None, result.errors
        pages = {
            post["title"]: [edge["node"]["body"] for edge in post["comments"]["edges"]]
            for post in result.data["posts"]
        }
        assert pages["Hello World"] == ["Thanks!", "Nice post!"]
        assert pages["GraphQL Guide"] == ["Great guide"]
