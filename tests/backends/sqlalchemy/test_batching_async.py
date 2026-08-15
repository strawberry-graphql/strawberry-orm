"""Async batching behaviour for the SQLAlchemy backend."""

import pytest
import strawberry
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_async import AbstractTestBatchingAsync
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class TestBatchingAsync(AbstractTestBatchingAsync):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session):
        self._sa_session = sa_session

    def schema_for(self, *, batching, async_resolver=False):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )
        session = self._sa_session

        @orm.type(SAPost)
        class PT:
            id: auto
            title: auto

        if async_resolver:

            @orm.type(SAUser)
            class UT:
                id: auto
                name: auto

                @strawberry.field
                async def posts(self) -> list[PT]:
                    return list(  # type: ignore[return-value]
                        session.scalars(
                            select(SAPost)
                            .where(SAPost.author_id == self.id)
                            .order_by(SAPost.id)
                        )
                    )

        else:

            @orm.type(SAUser)
            class UT:
                id: auto
                name: auto

                @strawberry.field
                def posts(self) -> list[PT]:
                    return (  # type: ignore[return-value]
                        select(SAPost)
                        .where(SAPost.author_id == self.id)
                        .order_by(SAPost.id)
                    )

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    async def execute_async(self, schema, query):
        return await schema.execute(query, context_value={"session": self._sa_session})

    def execute_sync(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self._sa_session})
