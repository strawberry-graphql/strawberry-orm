"""Siblings are batched only with parents of their own type.

A list of an interface or a union - Relay's ``nodes(ids:)`` among them - can
hold parents of several GraphQL types, and a field of one name on each may read
other rows, under another scope. What was read for one type's parents must not
answer another's, whichever comes first in the list.

A backend provides ``orm`` (a fresh ORM that batches relations) and ``models``
(``User``, ``Post``, ``Comment``), and on the test class:

- ``run(schema, query, variables)``, executing against the seeded rows and
  recording the number of SQL statements in ``self.query_count``;
- ``published_only(qs)``, keeping the published posts;
- ``posts_of(user)`` and ``comments_on(post)``, an unexecuted query for the
  user's posts or the post's comments, in id order;
- ``load_users()`` and ``load_posts()``, every user or post, read before any
  execution, in id order.

Seed: Alice wrote "Hello World" and "GraphQL Guide", Bob "Draft Post" (the only
draft), Charlie "Rust Adventures". Bob commented "Nice post!" and Alice
"Thanks!" on "Hello World", Charlie "Great guide" on "GraphQL Guide".
"""

from typing import Annotated

import strawberry
from strawberry import relay

from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection

NODES = """query ($ids: [ID!]!) { nodes(ids: $ids) { __typename
  ... on Owner { name FIELD }
  ... on Visitor { name FIELD }
} }"""


def gid(type_name, pk):
    return str(relay.GlobalID(type_name, str(pk)))


def page(*titles):
    return {"edges": [{"node": {"title": title}} for title in titles]}


def titles(*titles):
    return [{"title": title} for title in titles]


class _SiblingTypes:
    async def execute(self, schema, query, variables=None):
        result = await self.run(schema, query, variables)
        assert result.errors is None, result.errors
        return result.data

    async def nodes(self, schema, field, *ids):
        query = NODES.replace("FIELD", field)
        return (await self.execute(schema, query, {"ids": list(ids)}))["nodes"]

    async def _node_parts(self, orm, models):
        users = {user.id: user for user in await self.load_users()}
        published_only = self.published_only

        @orm.type(models.Post)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        def resolve_nodes(cls, *, info, node_ids, required=False):
            return [strawberry.cast(cls, users[int(pk)]) for pk in node_ids]

        def published(qs, info):
            return published_only(qs)

        return PostNode, classmethod(resolve_nodes), published

    @staticmethod
    def _nodes_schema(orm, *types):
        @strawberry.type
        class Query:
            nodes: list[relay.Node | None] = relay.node()

        return orm.schema(query=Query, types=list(types))

    async def mixed_order(self, schema, field, owned, visited):
        """Bob as a ``Visitor`` after an ``Owner``, then as an ``Owner``
        between two ``Visitor``s: each reads what its own type allows."""
        assert await self.nodes(
            schema, field, gid("Owner", 1), gid("Visitor", 2), gid("Visitor", 3)
        ) == [
            {"__typename": "Owner", "name": "Alice", **owned(1)},
            {"__typename": "Visitor", "name": "Bob", **visited(2)},
            {"__typename": "Visitor", "name": "Charlie", **visited(3)},
        ]
        assert await self.nodes(
            schema, field, gid("Visitor", 1), gid("Owner", 2), gid("Visitor", 3)
        ) == [
            {"__typename": "Visitor", "name": "Alice", **visited(1)},
            {"__typename": "Owner", "name": "Bob", **owned(2)},
            {"__typename": "Visitor", "name": "Charlie", **visited(3)},
        ]


_WROTE = {
    1: ["Hello World", "GraphQL Guide"],
    2: ["Draft Post"],
    3: ["Rust Adventures"],
}
_PUBLISHED = {**_WROTE, 2: []}


class AbstractTestBatchingSiblingTypes(_SiblingTypes):
    async def people_schema(self, orm, models):
        """``Owner`` reads every post its user wrote, ``Visitor`` only the
        published ones. Both are over ``User``: only the cast each node is
        returned with tells them apart."""
        PostNode, by_id, published = await self._node_parts(orm, models)
        posts_of, published_only = self.posts_of, self.published_only

        @orm.type(models.User)
        class Owner(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager()
            resolve_nodes = by_id

            @orm.field.lazy
            def writing(self) -> list[PostNode]:
                return posts_of(self)

        @orm.type(models.User)
        class Visitor(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager(scope=published)
            resolve_nodes = by_id

            @orm.field.lazy
            def writing(self) -> list[PostNode]:
                return published_only(posts_of(self))

        return self._nodes_schema(orm, PostNode, Owner, Visitor)

    async def test_a_page_is_cut_under_each_parents_own_type(self, orm, models):
        await self.mixed_order(
            await self.people_schema(orm, models),
            "posts { edges { node { title } } }",
            lambda pk: {"posts": page(*_WROTE[pk])},
            lambda pk: {"posts": page(*_PUBLISHED[pk])},
        )

    async def test_a_resolver_runs_for_parents_of_its_own_type(self, orm, models):
        await self.mixed_order(
            await self.people_schema(orm, models),
            "writing { title }",
            lambda pk: {"writing": titles(*_WROTE[pk])},
            lambda pk: {"writing": titles(*_PUBLISHED[pk])},
        )

    async def test_parents_of_one_type_still_share_one_read(self, orm, models):
        schema = await self.people_schema(orm, models)

        async def statements(*ids):
            await self.nodes(schema, "posts { edges { node { title } } }", *ids)
            return self.query_count

        two = await statements(gid("Visitor", 1), gid("Visitor", 2))
        three = await statements(
            gid("Visitor", 1), gid("Visitor", 2), gid("Visitor", 3)
        )
        assert three == two, "a third visitor cost a read of its own"
        alone = await statements(gid("Owner", 1))
        mixed = await statements(gid("Owner", 1), gid("Visitor", 2), gid("Visitor", 3))
        assert mixed == alone + two, "the owner split up the visitors' read"

    async def test_a_union_of_two_models_reads_each_models_own_rows(self, orm, models):
        """Rows returned uncast are told apart by their model."""
        posts_of, comments_on = self.posts_of, self.comments_on
        entries = [*(await self.load_users())[:2], *(await self.load_posts())[:2]]

        @orm.type(models.Comment)
        class CommentType:
            id: auto
            body: auto

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto
            body: auto

            @orm.field.lazy
            def comments(self) -> list[CommentType]:
                return comments_on(self)

        @orm.type(models.User)
        class UserType:
            id: auto
            name: auto

            @orm.field.lazy
            def posts(self) -> list[PostType]:
                return posts_of(self)

        @strawberry.type
        class Query:
            @strawberry.field
            def feed(
                self,
            ) -> list[Annotated[UserType | PostType, strawberry.union("Entry")]]:
                return entries

        schema = orm.schema(query=Query)
        query = """{ feed {
          ... on UserType { name items: posts { title body } }
          ... on PostType { title items: comments { body } }
        } }"""
        assert await self.execute(schema, query) == {
            "feed": [
                {
                    "name": "Alice",
                    "items": [
                        {"title": "Hello World", "body": "First post"},
                        {"title": "GraphQL Guide", "body": "Learn GraphQL"},
                    ],
                },
                {
                    "name": "Bob",
                    "items": [{"title": "Draft Post", "body": "Not published yet"}],
                },
                {
                    "title": "Hello World",
                    "items": [{"body": "Nice post!"}, {"body": "Thanks!"}],
                },
                {"title": "GraphQL Guide", "items": [{"body": "Great guide"}]},
            ]
        }

    async def _two_user_types(self, orm, models, *, decides_its_own=False):
        posts_of, published_only = self.posts_of, self.published_only

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

        @orm.type(models.User)
        class Wide:
            id: auto
            name: auto

            @orm.field.lazy
            def writing(self) -> list[PostType]:
                return posts_of(self)

            if decides_its_own:

                @classmethod
                def is_type_of(cls, obj, info):
                    return isinstance(obj, models.User) and obj.id == 1

        @orm.type(models.User)
        class Narrow:
            id: auto
            name: auto

            @orm.field.lazy
            def writing(self) -> list[PostType]:
                return published_only(posts_of(self))

            if decides_its_own:

                @classmethod
                def is_type_of(cls, obj, info):
                    return isinstance(obj, models.User) and obj.id != 1

        return Wide, Narrow

    @staticmethod
    def _people_schema(orm, wide, narrow, people):
        """*people* returns the rows, as a cast holds in its own execution."""
        Person = Annotated[wide | narrow, strawberry.union("Person")]

        @strawberry.type
        class Query:
            @strawberry.field
            def people(self) -> list[Person | None]:
                return people()

        return orm.schema(query=Query)

    async def test_a_type_that_decides_its_own_rows_is_read_a_parent_at_a_time(
        self, orm, models
    ):
        """Which type an app's own ``is_type_of`` picks is not knowable ahead,
        so its parents are not batched with anyone."""
        Wide, Narrow = await self._two_user_types(orm, models, decides_its_own=True)
        users = await self.load_users()
        schema = self._people_schema(orm, Wide, Narrow, lambda: users)
        query = "{ people { __typename ... on Wide { name writing { title } }"
        query += " ... on Narrow { name writing { title } } } }"
        assert await self.execute(schema, query) == {
            "people": [
                {
                    "__typename": "Wide",
                    "name": "Alice",
                    "writing": titles("Hello World", "GraphQL Guide"),
                },
                {"__typename": "Narrow", "name": "Bob", "writing": []},
                {
                    "__typename": "Narrow",
                    "name": "Charlie",
                    "writing": titles("Rust Adventures"),
                },
            ]
        }

    async def test_rows_of_no_knowable_type_are_left_out_of_every_batch(
        self, orm, models
    ):
        """A user both types read, returned uncast, and a post neither reads,
        are refused; their siblings are still batched by type."""
        Wide, Narrow = await self._two_user_types(orm, models)
        alice, bob, charlie = await self.load_users()
        post = (await self.load_posts())[0]
        schema = self._people_schema(
            orm,
            Wide,
            Narrow,
            lambda: [
                strawberry.cast(Narrow, alice),
                bob,
                post,
                strawberry.cast(Narrow, charlie),
            ],
        )
        query = "{ people { ... on Wide { name writing { title } }"
        query += " ... on Narrow { name writing { title } } } }"
        result = await self.run(schema, query, None)
        assert result.data == {
            "people": [
                {"name": "Alice", "writing": titles("Hello World", "GraphQL Guide")},
                None,
                None,
                {"name": "Charlie", "writing": titles("Rust Adventures")},
            ]
        }
        assert [error.path for error in result.errors] == [
            ["people", 1],
            ["people", 2],
        ]


class AbstractTestOnViewSiblingTypes(_SiblingTypes):
    """An ``on=`` view's rows are read once for every parent, so the same
    holds for them."""

    async def test_a_view_is_scoped_under_each_parents_own_type(self, orm, models):
        PostNode, by_id, published = await self._node_parts(orm, models)

        @orm.type(models.User)
        class Owner(relay.Node):
            id: relay.NodeID[int]
            name: auto
            recent: list[PostNode] = orm.field.eager(on="posts")
            resolve_nodes = by_id

        @orm.type(models.User)
        class Visitor(relay.Node):
            id: relay.NodeID[int]
            name: auto
            recent: list[PostNode] = orm.field.eager(on="posts", scope=published)
            resolve_nodes = by_id

        await self.mixed_order(
            self._nodes_schema(orm, PostNode, Owner, Visitor),
            "recent { title }",
            lambda pk: {"recent": titles(*_WROTE[pk])},
            lambda pk: {"recent": titles(*_PUBLISHED[pk])},
        )
