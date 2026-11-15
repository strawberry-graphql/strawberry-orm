"""Rows a resolver hands back are scoped, whatever shape they come back in.

A query a resolver returns is scoped before it runs. Rows it already read are
checked against ``scope_rows`` afterwards, so a hidden row has to be dropped
there, however the rows arrive: through ``orm.optimize`` or a payload, as a
tuple or a generator rather than a list, or as the rows of a connection. What
the client selects must not change that, and neither must the optimizer.

A backend provides ``orm`` (a fresh ORM with a payload policy) and ``models``
(``User``, ``Post``), and on the test class:

- ``run(schema, query)``, executing against the seeded rows;
- ``published_only(qs)``, keeping the published posts;
- ``load_posts()``, every post, read before any execution, in id order.

Seed: Bob wrote the only draft, "Draft Post", whose id is 3.
"""

from collections.abc import Iterable
from inspect import isawaitable

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection

ALL_TITLES = ["Hello World", "GraphQL Guide", "Draft Post", "Rust Adventures"]
PUBLISHED_TITLES = ["Hello World", "GraphQL Guide", "Rust Adventures"]
DRAFT = 2


class AbstractTestResolverRowsScoping:
    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    def post_type(self, orm, models, base=(), **fields):
        published_only = self.published_only

        @classmethod
        def scope_rows(cls, qs, info):
            return published_only(qs)

        @orm.type(models.User)
        class AuthorType:
            id: auto
            name: auto

        namespace = {
            "__annotations__": {
                "id": auto,
                "title": auto,
                "author": AuthorType | None,
                **fields,
            },
            "scope_rows": scope_rows,
        }
        return orm.type(models.Post)(type("PostType", base, namespace))

    # -- orm.optimize and payloads --------------------------------------------

    async def test_payload_with_a_relation_still_hides_a_scoped_row(self, orm, models):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()

        @strawberry.type
        class Query:
            @orm.payload.query
            def post(self, info: strawberry.Info, index: int) -> PostType:
                return posts[index]

        schema = orm.schema(query=Query)
        for selection in ("title", "title author { name }"):
            data = await self.execute(
                schema, f"{{ post(index: {DRAFT}) {{ data {{ {selection} }} }} }}"
            )
            assert data == {"post": {"data": None}}, selection

    async def test_payload_with_a_relation_still_hides_scoped_rows(self, orm, models):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()

        @strawberry.type
        class Query:
            @orm.payload.query
            def posts(self, info: strawberry.Info) -> list[PostType]:
                return list(posts)

        data = await self.execute(
            orm.schema(query=Query), "{ posts { data { title author { name } } } }"
        )
        assert [post["title"] for post in data["posts"]["data"]] == PUBLISHED_TITLES

    async def test_optimize_with_a_relation_drops_scoped_rows(self, orm, models):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.Info) -> list[PostType]:
                return orm.optimize(list(posts), info)

            @strawberry.field
            def draft(self, info: strawberry.Info) -> PostType | None:
                return orm.optimize(posts[DRAFT], info)

        data = await self.execute(
            orm.schema(query=Query),
            "{ posts { title author { name } } draft { title author { name } } }",
        )
        assert [post["title"] for post in data["posts"]] == PUBLISHED_TITLES
        assert data["draft"] is None

    async def test_rows_optimize_saw_are_still_scoped_when_returned_as_they_were(
        self, orm, models
    ):
        """The documented form calls ``orm.optimize(rows, info)`` and returns
        ``rows``. Only the rows it checked and loaded are prepared, so the
        hidden one is checked again on the way out."""
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.Info) -> list[PostType]:
                rows = list(posts)
                optimized = orm.optimize(rows, info)
                if not isawaitable(optimized):
                    return rows

                async def loaded():
                    await optimized
                    return rows

                return loaded()

        data = await self.execute(
            orm.schema(query=Query), "{ posts { title author { name } } }"
        )
        assert [post["title"] for post in data["posts"]] == PUBLISHED_TITLES

    # -- iterables other than a list ------------------------------------------

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", ["tuple", "generator"])
    async def test_an_iterable_of_rows_is_scoped_like_a_list(
        self, orm, models, optimizer, shape
    ):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()
        wrap = tuple if shape == "tuple" else lambda rows: (row for row in rows)

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.Info) -> list[PostType]:
                return wrap(posts)

        schema = orm.schema(query=Query, optimizer=optimizer)
        for selection in ("title", "title author { name }"):
            data = await self.execute(schema, f"{{ posts {{ {selection} }} }}")
            assert [post["title"] for post in data["posts"]] == PUBLISHED_TITLES

    async def test_an_iterable_of_scalars_is_left_to_graphql(self, orm, models):
        self.post_type(orm, models)

        @strawberry.type
        class Query:
            @strawberry.field
            def numbers(self) -> list[int]:
                return (n for n in range(3))

        data = await self.execute(orm.schema(query=Query), "{ numbers }")
        assert data == {"numbers": [0, 1, 2]}

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_a_mapping_for_a_list_is_left_to_graphql(
        self, orm, models, optimizer
    ):
        PostType = self.post_type(orm, models)

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self) -> list[PostType]:
                return {"title": "not a row"}

        result = await self.run(
            orm.schema(query=Query, optimizer=optimizer), "{ posts { title } }"
        )
        assert result.errors[0].message.startswith("Expected Iterable")

    # -- connections over rows ------------------------------------------------

    @pytest.mark.parametrize("shape", ["list", "generator"])
    async def test_a_connection_over_rows_counts_only_the_visible_ones(
        self, orm, models, shape
    ):
        PostNode = self.post_type(orm, models, base=(relay.Node,), id=relay.NodeID[int])
        posts = await self.load_posts()
        wrap = list if shape == "list" else lambda rows: (row for row in rows)

        @strawberry.type
        class Query:
            @orm.connection(ORMListConnection[PostNode])
            def posts(self, info: strawberry.Info) -> Iterable[PostNode]:
                return wrap(posts)

        schema = orm.schema(query=Query)
        data = await self.execute(
            schema, "{ posts { totalCount edges { cursor node { title } } } }"
        )
        connection = data["posts"]
        assert connection["totalCount"] == len(PUBLISHED_TITLES)
        assert [edge["node"]["title"] for edge in connection["edges"]] == (
            PUBLISHED_TITLES
        )

        data = await self.execute(
            schema,
            "{ posts(first: 2) { totalCount edges { cursor } "
            "pageInfo { hasNextPage } } }",
        )
        assert data["posts"]["totalCount"] == len(PUBLISHED_TITLES)
        assert len(data["posts"]["edges"]) == 2
        assert data["posts"]["pageInfo"]["hasNextPage"] is True

        last = await self.execute(
            schema, "{ posts(last: 1) { edges { node { title } } } }"
        )
        assert last["posts"]["edges"] == [{"node": {"title": "Rust Adventures"}}]

    async def test_a_connection_over_a_mapping_is_left_to_relay(self, orm, models):
        PostNode = self.post_type(orm, models, base=(relay.Node,), id=relay.NodeID[int])

        @strawberry.type
        class Query:
            @orm.connection(ORMListConnection[PostNode])
            def posts(self, info: strawberry.Info) -> Iterable[PostNode]:
                return {"title": "not a row"}

        result = await self.run(
            orm.schema(query=Query), "{ posts { edges { node { title } } } }"
        )
        # Relay slices what it was handed, and a mapping has no slices.
        assert result.errors[0].message.startswith("slice(")
