from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import event, select

from strawberry_orm import StrawberryORM
from tests.abstract.hint_paths import AbstractTestHintPaths
from tests.backends.sqlalchemy.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def lenient_orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        strict_hints=False,
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment, Tag=Tag)


class TestHintPaths(AbstractTestHintPaths):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

        def materialized_posts(info: strawberry.Info):
            return list(info.context["session"].scalars(select(Post)).all())

        self.materialized_posts = materialized_posts

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
    def read(obj, name):
        return getattr(obj, name)

    @staticmethod
    def hide(qs, model, field, value):
        return qs.where(getattr(model, field) != value)
