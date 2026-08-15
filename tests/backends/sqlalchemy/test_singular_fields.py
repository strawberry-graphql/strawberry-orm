"""Singular field tests for the SQLAlchemy backend."""

import pytest
import strawberry
from sqlalchemy import select

from tests.abstract.singular_fields import (
    AbstractTestSingularFields,
    build_query,
    build_types,
)
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class TestSingularFields(AbstractTestSingularFields):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session):
        self._sa_session = sa_session

    def single_post_schema(self, orm):
        PT = build_types(orm, SAUser, SAPost)

        def post(id: int) -> PT | None:
            return select(SAPost).where(SAPost.id == id)  # type: ignore[return-value]

        def posts() -> list[PT]:
            return select(SAPost)  # type: ignore[return-value]

        Query = build_query(orm, PT, post, posts)
        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def execute(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self._sa_session})
