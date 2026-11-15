"""Generated connections without a ``context_value`` (Django)."""

import pytest

from tests.abstract.generated_connections import (
    AbstractTestGeneratedConnections,
    AbstractTestGeneratedConnectionsAsync,
)


@pytest.fixture
def schema_execute():
    def _execute(schema, query):
        return schema.execute_sync(query)

    return _execute


class TestGeneratedConnections(AbstractTestGeneratedConnections):
    pass


@pytest.mark.asyncio
class TestGeneratedConnectionsAsync(AbstractTestGeneratedConnectionsAsync):
    """The connection's own reads run on worker threads, in the execution."""

    @pytest.fixture
    def schema_execute(self):
        async def _execute(schema, query):
            return await schema.execute(query)

        return _execute
