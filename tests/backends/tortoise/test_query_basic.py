"""Basic query tests for the Tortoise backend."""

import pytest


class TestQueryBasic:
    @pytest.mark.asyncio
    async def test_list_all_users(self, execute, seed):
        data = await execute("{ users { id name email } }")
        assert data == {
            "users": [
                {"id": 1, "name": "Alice", "email": "alice@example.com"},
                {"id": 2, "name": "Bob", "email": "bob@example.com"},
                {"id": 3, "name": "Charlie", "email": "charlie@test.org"},
            ]
        }

    @pytest.mark.asyncio
    async def test_single_user_by_id(self, execute, seed):
        data = await execute("{ user(id: 1) { name email } }")
        assert data == {"user": {"name": "Alice", "email": "alice@example.com"}}

    @pytest.mark.asyncio
    async def test_single_user_not_found(self, execute, seed):
        data = await execute("{ user(id: 999) { name } }")
        assert data == {"user": None}

    @pytest.mark.asyncio
    async def test_list_all_posts(self, execute, seed):
        data = await execute("{ posts { id title isPublished } }")
        assert data == {
            "posts": [
                {"id": 1, "title": "Hello World", "isPublished": True},
                {"id": 2, "title": "GraphQL Guide", "isPublished": True},
                {"id": 3, "title": "Draft Post", "isPublished": False},
                {"id": 4, "title": "Rust Adventures", "isPublished": True},
            ]
        }
