"""Async resolver shapes that force the optimizer awaitable-query path."""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class TestAsyncOptimizerPath:
    @pytest.mark.asyncio
    async def test_async_resolver_returning_query_object_is_optimized(self, seed, User):
        orm = StrawberryORM("tortoise")

        @orm.type(User)
        class UserType:
            id: auto
            name: auto

        @strawberry.type
        class Query:
            @strawberry.field
            async def users(self) -> list[UserType]:
                return orm.get_default_queryset(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = await schema.execute("{ users { name } }")

        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ]
        }
