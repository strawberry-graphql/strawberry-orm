import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.query_default_limit import AbstractTestQueryDefaultLimitSync


@pytest.fixture
def make_default_limit_orm():
    def _make(**kwargs):
        return StrawberryORM.for_sqlalchemy(dialect="sqlite", **kwargs)

    return _make


@pytest.fixture
def schema_execute(sa_session):
    def _execute(schema, query):
        return schema.execute_sync(query, context_value={"session": sa_session})

    return _execute


class TestQueryDefaultLimit(AbstractTestQueryDefaultLimitSync):
    pass
