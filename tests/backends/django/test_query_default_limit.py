import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.query_default_limit import AbstractTestQueryDefaultLimitSync


@pytest.fixture
def make_default_limit_orm():
    def _make(**kwargs):
        return StrawberryORM("django", **kwargs)

    return _make


@pytest.fixture
def schema_execute():
    def _execute(schema, query):
        return schema.execute_sync(query)

    return _execute


class TestQueryDefaultLimit(AbstractTestQueryDefaultLimitSync):
    pass
