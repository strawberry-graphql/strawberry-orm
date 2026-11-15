"""Generated connections without a ``context_value`` (Tortoise)."""

import pytest
import pytest_asyncio

from tests.abstract.generated_connections import (
    AbstractTestGeneratedConnectionsAsync,
)


@pytest_asyncio.fixture
async def schema_execute():
    async def _execute(schema, query):
        return await schema.execute(query)

    return _execute


@pytest.mark.asyncio
class TestGeneratedConnections(AbstractTestGeneratedConnectionsAsync):
    pass
