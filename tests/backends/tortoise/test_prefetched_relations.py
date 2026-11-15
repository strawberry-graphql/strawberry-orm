"""A filterable relation reads loaded rows only when they were scoped as loaded."""

import pytest
import strawberry

from strawberry_orm.types import auto


def users_schema(orm, User, Post, users, *, published_only=False):
    @orm.type(Post, filters=orm.filter(Post), order=orm.order(Post))
    class PostType:
        id: auto
        title: auto

        if published_only:

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.filter(is_published=True)

    @orm.type(User)
    class UserType:
        id: auto
        name: auto
        posts: list[PostType]

    @strawberry.type
    class Query:
        @strawberry.field
        async def users(self) -> list[UserType]:
            return list(await users())

    return strawberry.Schema(query=Query)


async def titles(schema):
    result = await schema.execute("{ users { name posts { title } } }")
    assert result.errors is None, result.errors
    return {
        user["name"]: [p["title"] for p in user["posts"]]
        for user in result.data["users"]
    }


@pytest.mark.asyncio
class TestPrefetchedRelations:
    async def test_a_relation_not_loaded_is_queried(self, orm, seed, User, Post):
        schema = users_schema(orm, User, Post, lambda: User.all().order_by("id"))

        assert await titles(schema) == {
            "Alice": ["Hello World", "GraphQL Guide"],
            "Bob": ["Draft Post"],
            "Charlie": ["Rust Adventures"],
        }

    async def test_a_relation_the_app_prefetched_is_still_scoped(
        self, orm, seed, User, Post
    ):
        """Its cache holds every row, the ones ``scope_rows`` hides included."""
        schema = users_schema(
            orm,
            User,
            Post,
            lambda: User.all().order_by("id").prefetch_related("posts"),
            published_only=True,
        )

        assert await titles(schema) == {
            "Alice": ["Hello World", "GraphQL Guide"],
            "Bob": [],
            "Charlie": ["Rust Adventures"],
        }
