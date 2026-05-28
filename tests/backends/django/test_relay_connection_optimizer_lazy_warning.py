"""Optimizer + lazy-resolution behavior for nested relations under Relay connections."""

import pytest
from graphql import parse

from strawberry_orm.backends.django import DjangoBackend
from tests.abstract.relay_connection_optimizer_lazy_warning import (
    CONNECTION_QUERY,
    AbstractTestRelayConnectionOptimizerLazyWarningSync,
    AbstractTestRelayConnectionOptimizerUnitSync,
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


@pytest.fixture
def schema_execute_with_queries():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def _execute(schema, query):
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(query)
        return result, [q["sql"] for q in ctx.captured_queries]

    return _execute


@pytest.fixture
def apply_optimizer_hints_relay_connection():
    def _apply(User):
        backend = DjangoBackend()
        doc = parse(CONNECTION_QUERY)
        field_node = doc.definitions[0].selection_set.selections[0]
        info = type("Info", (), {"field_nodes": [field_node], "fragments": {}})()
        return backend.apply_optimizer_hints(None, User.objects.all(), info)

    return _apply


@pytest.mark.django_db
class TestRelayConnectionOptimizerLazyWarning(
    AbstractTestRelayConnectionOptimizerLazyWarningSync
):
    pass


@pytest.mark.django_db
class TestRelayConnectionOptimizerUnit(AbstractTestRelayConnectionOptimizerUnitSync):
    pass
