from types import SimpleNamespace

import pytest
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from tests.abstract.rows_without_extensions import AbstractTestRowsWithoutExtensions
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def orm(sa_session):
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        session_getter=lambda info: sa_session,
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestRowsWithoutExtensions(AbstractTestRowsWithoutExtensions):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query, context):
        return await schema.execute(query, context_value=context)

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
