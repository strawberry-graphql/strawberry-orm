from types import SimpleNamespace

import pytest
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from tests.abstract.relay_connection_scoping import AbstractTestRelayConnectionScoping
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestRelayConnectionScoping(AbstractTestRelayConnectionScoping):
    EXECUTIONS = ("sync", "async")

    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query, execution):
        context = {"session": self.session}
        if execution == "sync":
            return schema.execute_sync(query, context_value=context)
        return await schema.execute(query, context_value=context)

    @staticmethod
    def published_only(query):
        return query.where(Post.is_published.is_(True))

    async def load_posts(self):
        return list(self.session.scalars(self.every_post()))

    @staticmethod
    def every_post():
        return select(Post).order_by(Post.id)
