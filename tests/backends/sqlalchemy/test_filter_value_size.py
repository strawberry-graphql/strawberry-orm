from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from strawberry_orm import StrawberryORM
from tests.abstract.filter_value_size import AbstractTestFilterValueSize
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def make_orm():
    def _make_orm(**options):
        return StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", **options
        )

    return _make_orm


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


@pytest.fixture
def run(sa_session, seed):
    async def _run(schema, query, variables=None):
        return schema.execute_sync(
            query, variable_values=variables, context_value={"session": sa_session}
        )

    return _run


@pytest.fixture
def bound(sa_session):
    engine = sa_session.get_bind()

    @contextmanager
    def _bound():
        sizes: list[int] = []

        def before(conn, cursor, statement, params, context, executemany):
            values = params.values() if isinstance(params, dict) else params or ()
            sizes.append(sum(len(v) for v in values if isinstance(v, str)))

        event.listen(engine, "before_cursor_execute", before)
        try:
            yield sizes
        finally:
            event.remove(engine, "before_cursor_execute", before)

    return _bound


class TestFilterValueSize(AbstractTestFilterValueSize):
    pass
