"""Async execution tests for the SQLAlchemy backend."""

import pytest

from tests.backends.sqlalchemy.fixtures import main_schema


class TestAsyncExecution:
    @pytest.mark.asyncio
    async def test_async_schema_execute_supports_basic_query(self, sa_session, seed):
        result = await main_schema.execute(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
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
    async def test_async_schema_execute_supports_ref_list_mutation(
        self, sa_session, seed
    ):
        result = await main_schema.execute(
            """
            mutation {
                setPostTags(postId: 2, tags: [{ update: { id: "3" } }]) {
                    tags { name }
                }
            }
            """,
            context_value={"session": sa_session},
        )
        assert result.errors is None
        tag_names = sorted(t["name"] for t in result.data["setPostTags"]["tags"])
        assert "rust" in tag_names
