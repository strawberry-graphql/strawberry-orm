"""Tests for optimizer handling of union inline fragments and fragment spreads."""

import pytest
import pytest_asyncio
from graphql import parse

from strawberry_orm.backends.tortoise import TortoiseBackend
from tests.abstract.optimizer_inline_fragments import (
    AbstractTestOptimizerInlineFragmentsAsync,
    AbstractTestOptimizerInlineFragmentsUnitAsync,
)


@pytest.fixture
def users_query():
    def _users_query(User, info):
        return User.all()

    return _users_query


@pytest.fixture
def posts_query():
    def _posts_query(Post, info):
        return Post.all()

    return _posts_query


@pytest.fixture
def schema_execute_async():
    async def _execute(schema, query):
        return await schema.execute(query)

    return _execute


@pytest_asyncio.fixture
async def schema_execute_with_queries_async(query_counter, schema_execute_async):
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
async def apply_optimizer_hints_inline_fragment(tortoise_db, Post):
    async def _apply(Post):
        backend = TortoiseBackend()
        doc = parse(
            "{ posts { ... on PostBrief { title } ... on PostFull { title } } }"
        )
        field_node = doc.definitions[0].selection_set.selections[0]
        info = type("Info", (), {"field_nodes": [field_node], "fragments": {}})()
        return await backend.apply_optimizer_hints(None, Post.all(), info)

    return _apply


@pytest.mark.asyncio
class TestOptimizerInlineFragments(AbstractTestOptimizerInlineFragmentsAsync):
    @pytest.mark.skip(
        reason="GraphQL union list runtime mapping is only exercised on Django"
    )
    def test_graphql_union_list_inline_fragments(self) -> None:
        pass


@pytest.mark.asyncio
class TestOptimizerInlineFragmentsUnit(AbstractTestOptimizerInlineFragmentsUnitAsync):
    pass
