"""Session resolution tests: verify all _get_session code paths (SQLAlchemy only)."""

import strawberry
from sqlalchemy import select

from strawberry_orm.types import auto

EXPECTED_USERS = {
    "users": [
        {"name": "Alice"},
        {"name": "Bob"},
        {"name": "Charlie"},
    ]
}


class TestQuerySessionResolution:
    def _build_schema(self, orm, User):
        @orm.type(User)
        class UT:
            id: auto
            name: auto

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        return strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])

    def test_session_from_dict_key(self, orm, sa_session, seed, User):
        schema = self._build_schema(orm, User)
        result = schema.execute_sync(
            "{ users { name } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == EXPECTED_USERS

    def test_session_from_dict_callable(self, orm, sa_session, seed, User):
        """Callable sessions in context are rejected; use session_getter= instead."""
        schema = self._build_schema(orm, User)
        result = schema.execute_sync(
            "{ users { name } }",
            context_value={"session": lambda: sa_session},
        )
        assert result.errors is not None
        assert "callable" in str(result.errors[0]).lower()

    def test_session_from_context_attribute(self, orm, sa_session, seed, User):
        class Context:
            def __init__(self, session):
                self.session = session

        schema = self._build_schema(orm, User)
        result = schema.execute_sync(
            "{ users { name } }",
            context_value=Context(sa_session),
        )
        assert result.errors is None
        assert result.data == EXPECTED_USERS

    def test_session_from_context_callable_attribute(self, orm, sa_session, seed, User):
        """Callable sessions on context objects are rejected; use session_getter= instead."""

        class Context:
            def __init__(self, session):
                self.session = lambda: session

        schema = self._build_schema(orm, User)
        result = schema.execute_sync(
            "{ users { name } }",
            context_value=Context(sa_session),
        )
        assert result.errors is not None
        assert "callable" in str(result.errors[0]).lower()

    def test_session_from_get_session_method(self, orm, sa_session, seed, User):
        class Context:
            def __init__(self, session):
                self._session = session

            def get_session(self):
                return self._session

        schema = self._build_schema(orm, User)
        result = schema.execute_sync(
            "{ users { name } }",
            context_value=Context(sa_session),
        )
        assert result.errors is None
        assert result.data == EXPECTED_USERS
