"""Diagnostics tests for the Tortoise backend."""

import logging

import pytest
import strawberry

from strawberry_orm.types import auto

LOGGER = "strawberry_orm.lazy_query"


class TestAsyncDiagnostics:
    @pytest.mark.asyncio
    async def test_hinted_computed_field_is_silent(self, orm, seed, caplog, Post):
        caplog.set_level(logging.WARNING, logger=LOGGER)

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["author"])
            def byline(self) -> str:
                return f"by {self.author.name}"

        @strawberry.type
        class Q:
            @strawberry.field
            def posts(self) -> list[PT]:
                return Post.all()  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=Q,
            extensions=[
                orm.optimizer_extension(),
                orm.lazy_resolution_extension(mode="warn"),
            ],
        )
        result = await schema.execute("{ posts { byline } }")

        assert result.errors is None
        assert result.data["posts"][0]["byline"] == "by Alice"
        assert [r.message for r in caplog.records if r.name == LOGGER] == []
