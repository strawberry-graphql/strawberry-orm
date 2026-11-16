"""A parent is handed only the rows read for that very parent.

Batched rows and pages are filed under ``id(parent)``. An id is an address,
which Python hands to a new object once the old one is gone, so a parent that
was never in a batch can share an id with one that was. These make ids collide
on purpose: every post with an odd primary key shares one, every even one
another.

A backend provides ``orm`` (a fresh ORM that batches relations) and ``models``
(``User``, ``Post``, ``Comment``), and on the test class:

- ``run(schema, query)``, executing against the seeded rows;
- ``comments_on(post)``, an unexecuted query for the post's comments, in id
  order.

Seed: Alice wrote "Hello World" (1) and "GraphQL Guide" (2), Bob "Draft Post"
(3), Charlie "Rust Adventures" (4). "Hello World" has the comments "Nice post!"
and "Thanks!", "GraphQL Guide" has "Great guide", the others none.

Alice's two posts are batched together; Bob's and Charlie's, alone in their
lists, are looked up in that batch and must not be found there.
"""

import builtins

import pytest
import strawberry

import strawberry_orm.batching
from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection

QUERY = "{ users { name posts { title COMMENTS } } }"


def _expected(read):
    return {
        "users": [
            {
                "name": "Alice",
                "posts": [
                    {"title": "Hello World", **read("Nice post!", "Thanks!")},
                    {"title": "GraphQL Guide", **read("Great guide")},
                ],
            },
            {"name": "Bob", "posts": [{"title": "Draft Post", **read()}]},
            {"name": "Charlie", "posts": [{"title": "Rust Adventures", **read()}]},
        ]
    }


class AbstractTestBatchingParentIdentity:
    @pytest.fixture(autouse=True)
    def _colliding_ids(self, monkeypatch, models):
        def colliding(value):
            if isinstance(value, models.Post):
                return value.id % 2
            return builtins.id(value)

        monkeypatch.setattr(strawberry_orm.batching, "id", colliding, raising=False)

    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    def schema(self, orm, models, PostType):
        @orm.type(models.User)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.eager()

        return orm.schema(query=Query)

    async def test_batched_rows_go_only_to_their_own_parent(self, orm, models):
        comments_on = self.comments_on

        @orm.type(models.Comment)
        class CommentType:
            id: auto
            body: auto

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy
            def comments(self) -> list[CommentType]:
                return comments_on(self)

        schema = self.schema(orm, models, PostType)
        query = QUERY.replace("COMMENTS", "comments { body }")
        assert await self.execute(schema, query) == _expected(
            lambda *bodies: {"comments": [{"body": body} for body in bodies]}
        )

    async def test_a_page_goes_only_to_its_own_parent(self, orm, models):
        @orm.type(models.Comment)
        class CommentType:
            id: auto
            body: auto

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto
            comments: ORMListConnection[CommentType] = orm.connection.eager()

        schema = self.schema(orm, models, PostType)
        query = QUERY.replace("COMMENTS", "comments { edges { node { body } } }")
        assert await self.execute(schema, query) == _expected(
            lambda *bodies: {
                "comments": {"edges": [{"node": {"body": body}} for body in bodies]}
            }
        )
