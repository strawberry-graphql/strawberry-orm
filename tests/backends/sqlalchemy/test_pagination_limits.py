"""How much one request can make the database read (SQLAlchemy)."""

import pytest
from sqlalchemy import select

from tests.abstract.pagination_limits import (
    AbstractTestDefaultQueryLimit,
    AbstractTestOrderAndGroupByLimits,
    AbstractTestPaginationLimits,
)
from tests.backends.sqlalchemy.models import User as SAUser
from tests.backends.sqlalchemy.test_relay_connection_pagination import (
    _extra_users,
    _recorder,
)


@pytest.fixture
def many_users(sa_session, seed):
    sa_session.add_all(_extra_users())
    sa_session.commit()
    return list(sa_session.scalars(select(SAUser.name).order_by(SAUser.id)))


@pytest.fixture
def capture(sa_session):
    return _recorder(sa_session)


@pytest.fixture
def run(sa_session):
    async def _run(schema, query):
        return schema.execute_sync(query, context_value={"session": sa_session})

    return _run


class TestPaginationLimits(AbstractTestPaginationLimits):
    pass


class TestOrderAndGroupByLimits(AbstractTestOrderAndGroupByLimits):
    pass


class TestDefaultQueryLimit(AbstractTestDefaultQueryLimit):
    pass
