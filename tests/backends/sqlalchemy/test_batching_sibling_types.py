from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from strawberry_orm import StrawberryORM
from tests.abstract.batching_sibling_types import (
    AbstractTestBatchingSiblingTypes,
    AbstractTestOnViewSiblingTypes,
)
from tests.backends.sqlalchemy.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


class _SQLAlchemy:
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query, variables):
        statements: list[str] = []

        def count(conn, cursor, statement, *args):
            statements.append(statement)

        engine = self.session.bind
        event.listen(engine, "before_cursor_execute", count)
        try:
            return schema.execute_sync(
                query,
                variable_values=variables,
                context_value={"session": self.session},
            )
        finally:
            event.remove(engine, "before_cursor_execute", count)
            self.query_count = len(statements)

    @staticmethod
    def published_only(qs):
        return qs.where(Post.is_published.is_(True))

    @staticmethod
    def posts_of(user):
        return select(Post).where(Post.author_id == user.id).order_by(Post.id)

    @staticmethod
    def comments_on(post):
        return select(Comment).where(Comment.post_id == post.id).order_by(Comment.id)

    async def load_users(self):
        return list(self.session.scalars(select(User).order_by(User.id)))

    async def load_posts(self):
        return list(self.session.scalars(select(Post).order_by(Post.id)))


class TestBatchingSiblingTypes(_SQLAlchemy, AbstractTestBatchingSiblingTypes):
    pass


class TestOnViewSiblingTypes(_SQLAlchemy, AbstractTestOnViewSiblingTypes):
    pass
