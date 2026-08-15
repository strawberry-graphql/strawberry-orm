"""Connections with a caller-supplied queryset (Django)."""

import pytest

from tests.abstract.connection_custom_resolver import (
    AbstractTestConnectionCustomResolver,
)


@pytest.fixture
def users_query(orm):
    def _users_query(User, info):
        return orm.get_default_queryset(User).order_by("id")

    return _users_query


@pytest.fixture
def narrow_by_name():
    def _narrow(queryset, name):
        return queryset.filter(name=name)

    return _narrow


@pytest.fixture
def schema_execute():
    def _execute(schema, query):
        return schema.execute_sync(query, context_value={})

    return _execute


@pytest.mark.django_db
class TestConnectionCustomResolver(AbstractTestConnectionCustomResolver):
    pass
