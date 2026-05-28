"""Optimizer + lazy-resolution behavior for nested relations under Relay connections."""

import pytest
from graphql import parse
from sqlalchemy import select

from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend
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
def schema_execute(sa_session):
    def _execute(schema, query):
        return schema.execute_sync(query, context_value={"session": sa_session})

    return _execute


@pytest.fixture
def schema_execute_with_queries(sa_session, query_counter):
    def _execute(schema, query):
        start = len(query_counter)
        result = schema.execute_sync(query, context_value={"session": sa_session})
        return result, query_counter[start:]

    return _execute


@pytest.fixture
def apply_optimizer_hints_relay_connection(sa_session):
    def _apply(User):
        backend = SQLAlchemyBackend(dialect="sqlite")
        doc = parse(CONNECTION_QUERY)
        field_node = doc.definitions[0].selection_set.selections[0]
        info = type(
            "Info",
            (),
            {
                "field_nodes": [field_node],
                "fragments": {},
                "context": {"session": sa_session},
            },
        )()
        return backend.apply_optimizer_hints(None, select(User), info)

    return _apply


class TestRelayConnectionOptimizerLazyWarning(
    AbstractTestRelayConnectionOptimizerLazyWarningSync
):
    pass


class TestRelayConnectionOptimizerUnit(AbstractTestRelayConnectionOptimizerUnitSync):
    pass
