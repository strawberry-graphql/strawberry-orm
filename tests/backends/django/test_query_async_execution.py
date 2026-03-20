"""Async execution tests for the Django backend."""

import pytest
import strawberry
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.backends.django.fixtures import main_schema
from tests.backends.django.models import Post as DjPost, Tag as DjTag


class TestAsyncExecution:
    @pytest.mark.asyncio
    async def test_async_schema_execute_supports_basic_query(self, seed):
        result = await main_schema.execute("{ users { name posts { title } } }")
        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World"},
                        {"title": "GraphQL Guide"},
                    ],
                },
                {"name": "Bob", "posts": [{"title": "Draft Post"}]},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }

    @pytest.mark.asyncio
    async def test_async_schema_execute_supports_ref_list_mutation(self, seed):
        orm = StrawberryORM("django")

        @orm.type(DjTag)
        class TagType:
            name: str

        TagRef = orm.ref(DjTag)

        @strawberry.type
        class Query:
            @strawberry.field
            def ping(self) -> str:
                return "pong"

        @strawberry.type
        class Mutation:
            @strawberry.mutation
            async def set_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                post = await sync_to_async(DjPost.objects.get, thread_sensitive=True)(
                    pk=post_id
                )
                await orm.apply_ref_list(post, "tags", tags, info)
                fetched = await sync_to_async(list, thread_sensitive=True)(
                    post.tags.all()
                )
                return fetched  # type: ignore[return-value]

        schema = strawberry.Schema(query=Query, mutation=Mutation)
        result = await schema.execute(
            """
            mutation {
                setPostTags(postId: 2, tags: [{ update: { id: "3" } }]) {
                    name
                }
            }
            """
        )
        assert result.errors is None
        tag_names = sorted(t["name"] for t in result.data["setPostTags"])
        assert "rust" in tag_names
