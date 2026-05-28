"""``totalCount`` on Relay connections (Tortoise)."""

import pytest
import pytest_asyncio

from tests.abstract.relay_connection_total_count import (
    AbstractTestRelayConnectionTotalCountAsync,
)


@pytest.fixture
def users_query(orm):
    def _users_query(User, info):
        return orm.get_default_queryset(User)

    return _users_query


@pytest_asyncio.fixture
async def schema_execute():
    async def _execute(schema, query):
        return await schema.execute(query)

    return _execute


@pytest.mark.asyncio
class TestRelayConnectionTotalCount(AbstractTestRelayConnectionTotalCountAsync):
    pass
