import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.query_hint_edge_cases import AbstractTestQueryHintEdgeCasesSync


@pytest.fixture
def make_basic_orm():
    return lambda: StrawberryORM("django")


@pytest.fixture
def schema_execute():
    def _execute(schema, query):
        return schema.execute_sync(query)

    return _execute


class TestQueryHintEdgeCases(AbstractTestQueryHintEdgeCasesSync):
    pass
