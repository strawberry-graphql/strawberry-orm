from types import SimpleNamespace

import pytest
from sqlalchemy import event

from strawberry_orm import StrawberryORM
from tests.abstract.optimizer_repeated_fragments import AbstractTestRepeatedFragments
from tests.backends.sqlalchemy.models import Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag)


class TestRepeatedFragments(AbstractTestRepeatedFragments):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query):
        statements: list[str] = []

        def count(conn, cursor, statement, *args):
            statements.append(statement)

        # A run must not find the rows an earlier run loaded still in the
        # session, or it reads less and the two cannot be compared.
        self.session.expunge_all()
        engine = self.session.bind
        event.listen(engine, "before_cursor_execute", count)
        try:
            return schema.execute_sync(query, context_value={"session": self.session})
        finally:
            event.remove(engine, "before_cursor_execute", count)
            self.query_count = len(statements)
