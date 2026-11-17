"""``to_query=`` maps an app's own query object for a field that filters.

An ``orm.field.lazy`` given ``filters=`` or ``order=``, and an
``orm.field.eager()`` decorating a resolver, take the client's filter and
order. When the resolver hands back an app's query object, ``to_query=`` turns
it into the ORM's before they apply. Taken as it was, the app's object reads
as its rows, and the filter and order the client sent are silently left out.

A backend provides ``orm`` (a fresh ORM whose ``to_query`` reads ``.query``
off an ``AppQuery``) and ``models`` (``User``, ``Post``), and on the test class:

- ``run(schema, query)``, executing against the seeded rows;
- ``published_only(query)``, keeping the published posts;
- ``all_posts()``, an unexecuted query for every post;
- ``load_posts()``, every post, read before any execution, in id order.

Seed: "Hello World", "GraphQL Guide", "Draft Post" (the only draft) and "Rust
Adventures".
"""

import pytest
import strawberry

from strawberry_orm import auto

ORDERED = "(order: [{field: {title: DESC}}])"
NARROWED = (
    '(filter: {field: {title: {contains: "u"}}}, order: [{field: {title: DESC}}])'
)


class AppQuery:
    """An app's own query object, which reads as the rows it was built with."""

    def __init__(self, query, rows):
        self.query = query
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)


def to_query(result, info):
    return result.query if isinstance(result, AppQuery) else result


class AbstractTestToQueryFilteredFields:
    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    async def schema(self, orm, models, *, optimizer):
        published_only, all_posts = self.published_only, self.all_posts
        loaded = await self.load_posts()
        PostFilter = orm.filter(models.Post)
        PostOrder = orm.order(models.Post)

        @orm.type(models.Post, filters=PostFilter, order=PostOrder)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, query, info):
                return published_only(query)

        @strawberry.type
        class Query:
            @orm.field.lazy(filters=PostFilter, order=PostOrder)
            def lazy_posts(self, info: strawberry.Info) -> list[PostType]:
                return AppQuery(all_posts(), loaded)

            @orm.field.lazy(filters=PostFilter, order=PostOrder)
            async def awaited_posts(self) -> list[PostType]:
                return AppQuery(all_posts(), loaded)

            @orm.field.eager()
            def eager_posts(self, info: strawberry.Info) -> list[PostType]:
                return AppQuery(all_posts(), loaded)

        return orm.schema(query=Query, optimizer=optimizer)

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("field", ["lazyPosts", "awaitedPosts", "eagerPosts"])
    async def test_the_clients_filter_and_order_apply_to_an_apps_query(
        self, orm, models, optimizer, field
    ):
        schema = await self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(
            schema,
            f"{{ ordered: {field}{ORDERED} {{ title }}"
            f" narrowed: {field}{NARROWED} {{ title }} }}",
        )
        assert data == {
            "ordered": [
                {"title": "Rust Adventures"},
                {"title": "Hello World"},
                {"title": "GraphQL Guide"},
            ],
            "narrowed": [{"title": "Rust Adventures"}, {"title": "GraphQL Guide"}],
        }
