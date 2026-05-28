"""``totalCount`` on Relay connections (SQLAlchemy)."""

import pytest

from tests.abstract.relay_connection_total_count import (
    AbstractTestRelayConnectionTotalCount,
)


@pytest.fixture
def users_query(orm):
    def _users_query(User, info):
        return orm.get_default_queryset(User)

    return _users_query


@pytest.fixture
def schema_execute(sa_session):
    def _execute(schema, query):
        return schema.execute_sync(query, context_value={"session": sa_session})

    return _execute


class TestRelayConnectionTotalCount(AbstractTestRelayConnectionTotalCount):
    pass
