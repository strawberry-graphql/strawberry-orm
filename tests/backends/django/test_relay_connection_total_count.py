"""``totalCount`` on Relay connections (Django)."""

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
def schema_execute():
    def _execute(schema, query):
        return schema.execute_sync(query)

    return _execute


@pytest.mark.django_db
class TestRelayConnectionTotalCount(AbstractTestRelayConnectionTotalCount):
    pass
