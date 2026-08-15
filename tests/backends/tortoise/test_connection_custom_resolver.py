"""Connections with a caller-supplied queryset (Tortoise)."""

import pytest
import pytest_asyncio

from tests.abstract.connection_custom_resolver import (
    AbstractTestConnectionCustomResolverAsync,
)


@pytest.fixture
def users_query(orm):
    def _users_query(User, info):
        return orm.get_default_queryset(User).order_by("id")

    return _users_query


@pytest.fixture
def narrow_by_name():
    def _narrow(queryset, name):
        return queryset.filter(name=name)

    return _narrow


@pytest_asyncio.fixture
async def schema_execute():
    async def _execute(schema, query):
        return await schema.execute(query, context_value={})

    return _execute


@pytest.mark.asyncio
class TestConnectionCustomResolver(AbstractTestConnectionCustomResolverAsync):
    pass
