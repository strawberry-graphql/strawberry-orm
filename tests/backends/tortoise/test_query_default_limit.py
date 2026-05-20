import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.query_default_limit import AbstractTestQueryDefaultLimitAsync


@pytest.fixture
def make_default_limit_orm():
    def _make(**kwargs):
        return StrawberryORM.for_tortoise( **kwargs)

    return _make


@pytest.fixture
def schema_execute_async():
    async def _execute(schema, query):
        return await schema.execute(query)

    return _execute


@pytest.mark.asyncio
class TestQueryDefaultLimit(AbstractTestQueryDefaultLimitAsync):
    pass
