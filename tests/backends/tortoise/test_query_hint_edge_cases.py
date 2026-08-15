import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.query_hint_edge_cases import AbstractTestQueryHintEdgeCasesAsync


@pytest.fixture
def make_basic_orm():
    return lambda **kwargs: StrawberryORM.for_tortoise(**kwargs)


@pytest.fixture
def schema_execute_async():
    async def _execute(schema, query):
        return await schema.execute(query)

    return _execute


@pytest.mark.asyncio
class TestQueryHintEdgeCases(AbstractTestQueryHintEdgeCasesAsync):
    pass
