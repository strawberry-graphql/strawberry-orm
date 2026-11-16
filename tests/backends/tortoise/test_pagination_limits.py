"""How much one request can make the database read (Tortoise)."""

import pytest

from tests.abstract.pagination_limits import (
    AbstractTestDefaultQueryLimit,
    AbstractTestEveryConnectionCutsItsPage,
    AbstractTestOrderAndGroupByLimits,
    AbstractTestPaginationLimits,
)
from tests.backends.tortoise.test_relay_connection_pagination import (  # noqa: F401
    capture,
    many_users,
)


@pytest.fixture
def run():
    async def _run(schema, query):
        return await schema.execute(query, context_value={})

    return _run


class TestPaginationLimits(AbstractTestPaginationLimits):
    pass


class TestOrderAndGroupByLimits(AbstractTestOrderAndGroupByLimits):
    pass


class TestDefaultQueryLimit(AbstractTestDefaultQueryLimit):
    pass


class TestEveryConnectionCutsItsPage(AbstractTestEveryConnectionCutsItsPage):
    pass
