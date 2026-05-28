"""Optimizer + lazy-resolution behavior for nested relations under Relay connections."""

import pytest
import pytest_asyncio
from graphql import parse

from strawberry_orm.backends.tortoise import TortoiseBackend
from tests.abstract.relay_connection_optimizer_lazy_warning import (
    CONNECTION_QUERY,
    AbstractTestRelayConnectionOptimizerLazyWarningAsync,
    AbstractTestRelayConnectionOptimizerUnitAsync,
)


@pytest.fixture
def users_query(orm):
    def _users_query(User, info):
        return orm.get_default_queryset(User)

    return _users_query


@pytest.fixture
def schema_execute_async():
    async def _execute(schema, query):
        return await schema.execute(query)

    return _execute


@pytest.fixture
def schema_execute(schema_execute_async):
    return schema_execute_async


@pytest_asyncio.fixture
async def schema_execute_with_queries(query_counter, schema_execute_async):
    async def _execute(schema, query):
        start = len(query_counter)
        result = await schema_execute_async(schema, query)
        return result, query_counter[start:]

    return _execute


@pytest_asyncio.fixture
async def query_counter(tortoise_db):
    """Track SQL statements executed during a test."""
    from tortoise import connections

    conn = connections.get("default")
    queries: list[str] = []
    original = conn.execute_query

    async def counting_execute(query: str, values: list | None = None):
        queries.append(query)
        return await original(query, values)

    conn.execute_query = counting_execute
    yield queries
    conn.execute_query = original


@pytest_asyncio.fixture
async def apply_optimizer_hints_relay_connection(tortoise_db):
    async def _apply(User):
        backend = TortoiseBackend()
        doc = parse(CONNECTION_QUERY)
        field_node = doc.definitions[0].selection_set.selections[0]
        info = type("Info", (), {"field_nodes": [field_node], "fragments": {}})()
        return await backend.apply_optimizer_hints(None, User.all(), info)

    return _apply


@pytest.mark.asyncio
class TestRelayConnectionOptimizerLazyWarning(
    AbstractTestRelayConnectionOptimizerLazyWarningAsync
):
    pass


@pytest.mark.asyncio
class TestRelayConnectionOptimizerUnit(AbstractTestRelayConnectionOptimizerUnitAsync):
    pass
