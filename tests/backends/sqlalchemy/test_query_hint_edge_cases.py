import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.query_hint_edge_cases import AbstractTestQueryHintEdgeCasesSync


@pytest.fixture
def make_basic_orm():
    return lambda: StrawberryORM.for_sqlalchemy(dialect="sqlite")


@pytest.fixture
def schema_execute(sa_session):
    def _execute(schema, query):
        return schema.execute_sync(query, context_value={"session": sa_session})

    return _execute


class TestQueryHintEdgeCases(AbstractTestQueryHintEdgeCasesSync):
    pass
