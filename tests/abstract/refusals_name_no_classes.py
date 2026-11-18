"""A query the library refuses is told why in the client's own terms.

The client knows the schema: its fields, relations and inputs. The app's model
classes, its other types and its settings are not part of it, so a refusal
names the relation or input the client wrote, never a Python class. What the
server's developer needs is kept as the error's cause, for the server log.

A backend's binding supplies:

``orm`` / ``models``      fixtures, as the other abstract suites take them
``run(schema, query)``    awaitable; the result of executing *query*
``hide_bob(query)``       *query* without the user named Bob
"""

import re

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import group_field
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

#: The test models' class names, as a refusal would spell them.
CLASS_NAMES = re.compile(r"\b(Comment|Post|Tag|User)\b")

GROUPS = " { groups { aggregates { count } } } }"
NAMES = " { edges { node { name } } } }"

#: Each refusal: the query, and how it begins, in the client's terms.
REFUSALS = {
    "a to-many relation grouped through a relation": (
        "{ commentConn(groupBy: [{object: {post: {object: {tags: "
        "{field: {name: true}}}}}}])" + GROUPS,
        "Cannot group by tags through post:",
    ),
    "objects: grouped through a relation": (
        "{ commentConn(groupBy: [{object: {post: {objects: {tags: "
        "{field: {name: true}}}}}}])" + GROUPS,
        "Cannot group by objects: {tags}:",
    ),
    "objects: grouped by a relation of the related rows": (
        "{ userConn(groupBy: [{objects: {posts: {object: {tags: "
        "{field: {name: true}}}}}}])" + GROUPS,
        "Cannot group by objects: {posts} through object:",
    ),
    "a group field reached through a relation": (
        "{ commentConn(groupBy: [{object: {post: {byTitle: true}}}])" + GROUPS,
        "Cannot group by by_title through post:",
    ),
    "objects: ordered both ways": (
        "{ posts(order: [{objects: {tags: {field: {name: ASC}}}}, "
        "{objects: {tags: {field: {name: DESC}}}}]) { title } }",
        "Cannot order by objects: {tags} both ascending and descending",
    ),
    "objects: ordered by a relation of the related rows": (
        "{ userConn(order: [{objects: {posts: {object: {tags: "
        "{field: {name: ASC}}}}}}])" + NAMES,
        "Cannot order by objects: {posts} through object:",
    ),
    "a scoped relation ordered by": (
        "{ comments(order: [{object: {author: {field: {name: ASC}}}}]) { body } }",
        "Cannot order by author: its rows are scoped",
    ),
}


class AbstractTestRefusalsNameNoClasses:
    @pytest.fixture
    def schema(self, orm, models):
        Tag, User, Post, Comment = models.Tag, models.User, models.Post, models.Comment
        hide_bob = self.hide_bob
        orm.group(Tag)
        orm.order(Tag)

        @orm.group_type(Post)
        class PostGroupBy:
            title: auto
            tags: auto

            @group_field
            def by_title(self, value: bool, query):
                return "title"  # pragma: no cover - refused before it groups

        post_order = orm.order(Post)

        @orm.type(User, order=orm.order(User), group=orm.group(User))
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

            @classmethod
            def scope_rows(cls, query, info):
                return hide_bob(query)

        @orm.type(Tag)
        class TagType:
            name: auto

        @orm.type(Post, order=post_order, group=PostGroupBy)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto
            tags: list[TagType]

        @orm.type(Comment, order=orm.order(Comment), group=orm.group(Comment))
        class CommentNode(relay.Node):
            id: relay.NodeID[int]
            body: auto
            post: PostNode
            author: UserNode | None

        @strawberry.type
        class Query:
            posts: list[PostNode] = orm.field.eager()
            comments: list[CommentNode] = orm.field.eager()
            comment_conn: ORMListConnection[CommentNode] = orm.connection.eager()
            user_conn: ORMListConnection[UserNode] = orm.connection.eager()

        # Built as ``strawberry.Schema`` would be: ``orm.schema()`` refuses the
        # scoped order input before any client could use it.
        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    @pytest.mark.parametrize("refusal", list(REFUSALS))
    async def test_a_refusal_names_what_the_client_wrote(self, schema, refusal):
        query, says = REFUSALS[refusal]
        result = await self.run(schema, query)
        assert result.errors, result.data
        message = result.errors[0].message
        assert message.startswith(says), message
        assert not CLASS_NAMES.search(message), message

    async def test_a_refused_order_keeps_what_scopes_it_for_the_log(self, schema):
        result = await self.run(schema, REFUSALS["a scoped relation ordered by"][0])
        cause = result.errors[0].original_error.__cause__
        assert "allow_scoped_ordering=['author']" in str(cause), cause
