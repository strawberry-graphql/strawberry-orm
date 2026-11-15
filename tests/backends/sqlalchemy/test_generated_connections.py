"""Generated connections without a ``context_value`` (SQLAlchemy)."""

import pytest

from tests.abstract.generated_connections import AbstractTestGeneratedConnections


@pytest.fixture
def orm(orm_factory, sa_session):
    return orm_factory(session_getter=lambda info: sa_session)


@pytest.fixture
def schema_execute():
    def _execute(schema, query):
        return schema.execute_sync(query)

    return _execute


class TestGeneratedConnections(AbstractTestGeneratedConnections):
    pass
