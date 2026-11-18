from types import SimpleNamespace

import pytest
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.rows_behind_awaitables import AbstractTestRowsBehindAwaitables
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        payload=policy(),
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestRowsBehindAwaitables(AbstractTestRowsBehindAwaitables):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    def context(self):
        return {"session": self.session}

    async def run(self, schema, query, context):
        return await schema.execute(query, context_value={**context, **self.context()})

    async def run_sync(self, schema, query, context):
        return schema.execute_sync(query, context_value={**context, **self.context()})

    async def subscribe(self, schema, query, context):
        events = await schema.subscribe(
            query, context_value={**context, **self.context()}
        )
        return [answer async for answer in events]

    @staticmethod
    def published_only(qs):
        return qs.where(Post.is_published.is_(True))

    @staticmethod
    def all_posts():
        return select(Post).order_by(Post.id)

    async def load_posts(self):
        return list(self.session.scalars(select(Post).order_by(Post.id)))

    async def load_users(self):
        return list(self.session.scalars(select(User).order_by(User.id)))
