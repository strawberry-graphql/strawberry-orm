"""Security properties of batching for the SQLAlchemy backend."""

import pytest
import strawberry
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_security import AbstractTestBatchingSecurity
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class TestBatchingSecurity(AbstractTestBatchingSecurity):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session):
        self._sa_session = sa_session

    def scoped_schema(self, *, batching):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )

        @orm.type(SAPost)
        class PT:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.where(SAPost.is_published.is_(True))

        @orm.type(SAUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts(self) -> list[PT]:
                return select(SAPost).where(  # type: ignore[return-value]
                    SAPost.author_id == self.id
                )

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def execute(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self._sa_session})

    def expected_titles_by_user(self):
        return {
            "Alice": ["Hello World", "GraphQL Guide"],
            "Bob": [],
            "Charlie": ["Rust Adventures"],
        }
