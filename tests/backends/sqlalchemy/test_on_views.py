from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from strawberry_orm import StrawberryORM
from tests.abstract.on_views import (
    AbstractTestASelfReadFieldIsChecked,
    AbstractTestOnViews,
)
from tests.backends.sqlalchemy.models import Post, Tag, User


def _orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def orm():
    return _orm()


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag)


class _SQLAlchemy:
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    fresh_orm = staticmethod(_orm)

    async def run(self, schema, query):
        statements: list[str] = []

        def count(conn, cursor, statement, *args):
            statements.append(statement)

        engine = self.session.bind
        event.listen(engine, "before_cursor_execute", count)
        try:
            return schema.execute_sync(query, context_value={"session": self.session})
        finally:
            event.remove(engine, "before_cursor_execute", count)
            self.query_count = len(statements)

    @staticmethod
    def hide(qs, model, field, value):
        return qs.where(getattr(model, field) != value)

    @staticmethod
    def newest_first(qs, model):
        return qs.order_by(model.id.desc())

    async def add_authors(self, count):
        for number in range(1, count + 1):
            author = User(id=100 + number, name=f"Author {number}", email="")
            self.session.add(author)
            self.session.add_all(
                [
                    Post(
                        title=f"Book {number}",
                        body="",
                        is_published=True,
                        author_id=author.id,
                    ),
                    Post(title=f"Draft {number}", body="", author_id=author.id),
                ]
            )
        self.session.flush()

    @staticmethod
    def own_posts(user, info):
        session = info.context["session"]
        return list(
            session.scalars(
                select(Post).where(Post.author_id == user.id).order_by(Post.id)
            )
        )


class TestOnViews(_SQLAlchemy, AbstractTestOnViews):
    view_statements = 3


class TestASelfReadFieldIsChecked(_SQLAlchemy, AbstractTestASelfReadFieldIsChecked):
    pass
