"""Diagnostics message tests for the Django backend."""

import logging

import pytest
import strawberry

from strawberry_orm.types import auto
from tests.abstract.diagnostics_messages import AbstractTestDiagnosticsMessages
from tests.backends.django.models import Post as DjPost


class TestDiagnosticsMessages(AbstractTestDiagnosticsMessages):
    def build_schema(self, orm, post_type, *, materialize, optimize=False, mode="warn"):
        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self) -> list[post_type]:
                qs = DjPost.objects.all()
                return list(qs) if materialize else qs  # type: ignore[return-value]

        extensions = [orm.lazy_resolution_extension(mode=mode)]
        if optimize:
            extensions.insert(0, orm.optimizer_extension())
        return strawberry.Schema(query=Query, extensions=extensions)

    def execute(self, schema, query):
        return schema.execute_sync(query)


class TestAsyncDiagnostics:
    """Async execution keeps the probe open across the resolver's await."""

    @pytest.mark.asyncio
    async def test_computed_field_lazy_load_is_reported_under_async(
        self, orm, seed, caplog
    ):
        caplog.set_level(logging.WARNING, logger="strawberry_orm.lazy_query")

        @orm.type(DjPost)
        class PT:
            id: auto
            title: auto

            @orm.field.custom
            def byline(self) -> str:
                return f"by {self.author.name}"

        @strawberry.type
        class Query:
            @orm.field.custom
            def posts(self) -> list[PT]:
                return list(DjPost.objects.all())  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=Query, extensions=[orm.lazy_resolution_extension(mode="warn")]
        )
        result = await schema.execute("{ posts { byline } }")

        assert result.errors is None
        messages = [
            r.message for r in caplog.records if r.name == "strawberry_orm.lazy_query"
        ]
        assert messages
        assert 'fix: @orm.field.computed(using=["author"])' in messages[0]
