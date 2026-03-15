"""Async execution tests for the Tortoise backend."""

import pytest

from tests.backends.tortoise.fixtures import main_schema


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
        result = await main_schema.execute(
            """
            mutation {
                setPostTags(postId: 2, tags: [{ id: "3" }]) {
                    tags { name }
                }
            }
            """
        )
        assert result.errors is None
        assert result.data == {"setPostTags": {"tags": [{"name": "rust"}]}}
