"""Error handling tests — common from abstract, SA-specific kept local."""

import strawberry
from sqlalchemy import select

from strawberry_orm.types import auto
from tests.abstract.query_error_handling import AbstractTestQueryErrorHandling


class TestQueryErrorHandling(AbstractTestQueryErrorHandling):
    def test_missing_session_raises_runtime_error(self, orm, sa_session, seed, User):
        """Executing a query without a session in the context should raise RuntimeError."""

        @orm.type(User)
        class UT:
            id: auto
            name: auto

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync("{ users { name } }", context_value={})
        assert result.errors is not None
        assert any("session" in str(e).lower() for e in result.errors)

    def test_is_query_object_with_query(self, orm, User):
        stmt = select(User)
        assert orm.is_query_object(stmt) is True
