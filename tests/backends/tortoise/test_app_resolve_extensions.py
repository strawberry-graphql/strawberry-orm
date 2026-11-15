"""An app's own resolve extension on every field (Tortoise)."""

import pytest
import pytest_asyncio

from tests.abstract.app_resolve_extensions import (
    AbstractTestAppResolveExtensionsAsync,
)


@pytest_asyncio.fixture
async def schema_execute():
    async def _execute(schema, query):
        return await schema.execute(query, context_value={})

    return _execute


@pytest.mark.asyncio
class TestAppResolveExtensions(AbstractTestAppResolveExtensionsAsync):
    pass
