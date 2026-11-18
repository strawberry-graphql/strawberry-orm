from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.probe_lifetime import AbstractTestProbeLifetime
from tests.backends.sqlalchemy.models import Post


@pytest.fixture
def make_orm():
    def _make_orm(**options):
        return StrawberryORM.for_sqlalchemy(dialect="sqlite", **options)

    return _make_orm


@pytest.fixture
def models():
    return SimpleNamespace(Post=Post)


@pytest.fixture
def run(sa_session, seed):
    async def _run(schema, query, context):
        return await schema.execute(
            query, context_value={"session": sa_session, **context}
        )

    return _run


@pytest.fixture
def hooks(sa_session):
    def _hooks():
        return list(sa_session.dispatch.do_orm_execute)

    return _hooks


@pytest.fixture
def many_posts(sa_session, seed):
    sa_session.add_all(
        [Post(id=i, title=f"p{i}", body="", author_id=1) for i in range(10, 1210)]
    )
    sa_session.flush()


class TestProbeLifetime(AbstractTestProbeLifetime):
    pass
