"""Rows a resolver hands back inside a list of lists are scoped as a list's are.

graphql-core reads every level of ``[[PostType]]`` as it reads a list, so the
rows in the inner lists reach the client just as the rows of a flat list do. A
row ``scope_rows`` hides is dropped from the inner list holding it, and the
lists keep their shape. Each inner list may be any iterable, as at the top.

A backend provides what ``AbstractTestResolverRowsScoping`` needs.

Seed: Bob wrote the only draft, "Draft Post", the third post.
"""

import pytest
import strawberry

from tests.abstract.resolver_rows_scoping import AbstractTestResolverRowsScoping

SHAPES = {
    "lists": lambda rows: [rows[:2], rows[2:]],
    "tuples": lambda rows: (tuple(rows[:2]), tuple(rows[2:])),
    "generators": lambda rows: [(row for row in rows[:2]), (row for row in rows[2:])],
}


def titles(pages):
    return [[post["title"] for post in page] for page in pages]


class AbstractTestNestedListRowsScoping:
    execute = AbstractTestResolverRowsScoping.execute
    post_type = AbstractTestResolverRowsScoping.post_type

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", sorted(SHAPES))
    async def test_a_hidden_row_is_dropped_from_its_inner_list(
        self, orm, models, optimizer, shape
    ):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()
        wrap = SHAPES[shape]

        @strawberry.type
        class Query:
            @strawberry.field
            def pages(self, info: strawberry.Info) -> list[list[PostType]]:
                return wrap(posts)

        schema = orm.schema(query=Query, optimizer=optimizer)
        for selection in ("title", "title author { name }"):
            data = await self.execute(schema, f"{{ pages {{ {selection} }} }}")
            assert titles(data["pages"]) == [
                ["Hello World", "GraphQL Guide"],
                ["Rust Adventures"],
            ], selection

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_lists_three_deep_keep_their_shape(self, orm, models, optimizer):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()

        @strawberry.type
        class Query:
            @strawberry.field
            def shelves(self, info: strawberry.Info) -> list[list[list[PostType]]]:
                return [[posts[:1], posts[1:3]], [], [posts[3:], []]]

        data = await self.execute(
            orm.schema(query=Query, optimizer=optimizer),
            "{ shelves { title author { name } } }",
        )
        assert [titles(shelf) for shelf in data["shelves"]] == [
            [["Hello World"], ["GraphQL Guide"]],
            [],
            [["Rust Adventures"], []],
        ]

    async def test_optimize_drops_a_hidden_row_from_its_inner_list(self, orm, models):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()

        @strawberry.type
        class Query:
            @strawberry.field
            def pages(self, info: strawberry.Info) -> list[list[PostType]]:
                return orm.optimize([posts[:2], posts[2:]], info)

        data = await self.execute(
            orm.schema(query=Query), "{ pages { title author { name } } }"
        )
        assert titles(data["pages"]) == [
            ["Hello World", "GraphQL Guide"],
            ["Rust Adventures"],
        ]
