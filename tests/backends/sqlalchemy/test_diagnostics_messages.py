"""Diagnostics message tests for the SQLAlchemy backend."""

import logging

import pytest
import strawberry
from sqlalchemy import select

from strawberry_orm.types import auto
from tests.abstract.diagnostics_messages import AbstractTestDiagnosticsMessages
from tests.backends.sqlalchemy.models import Post as SAPost


class TestDiagnosticsMessages(AbstractTestDiagnosticsMessages):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session):
        self._sa_session = sa_session

    def build_schema(self, orm, post_type, *, materialize, optimize=False, mode="warn"):
        session = self._sa_session

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self) -> list[post_type]:
                stmt = select(SAPost)
                if materialize:
                    return list(session.scalars(stmt))  # type: ignore[return-value]
                return stmt  # type: ignore[return-value]

        extensions = [orm.lazy_resolution_extension(mode=mode)]
        if optimize:
            extensions.insert(0, orm.optimizer_extension())
        return strawberry.Schema(query=Query, extensions=extensions)

    def execute(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self._sa_session})


class TestAsyncDiagnostics:
    """Async execution keeps the probe open across the resolver's await."""

    @pytest.mark.asyncio
    async def test_computed_field_lazy_load_is_reported_under_async(
        self, orm, sa_session, seed, caplog
    ):
        caplog.set_level(logging.WARNING, logger="strawberry_orm.lazy_query")

        @orm.type(SAPost)
        class PT:
            id: auto
            title: auto

            @strawberry.field
            def byline(self) -> str:
                return f"by {self.author.name}"

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self) -> list[PT]:
                return list(sa_session.scalars(select(SAPost)))  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=Query, extensions=[orm.lazy_resolution_extension(mode="warn")]
        )
        result = await schema.execute(
            "{ posts { byline } }", context_value={"session": sa_session}
        )

        assert result.errors is None
        messages = [
            r.message for r in caplog.records if r.name == "strawberry_orm.lazy_query"
        ]
        assert messages
        assert 'fix: @orm.field.computed(using=["author"])' in messages[0]
