"""Connections with a caller-supplied select (SQLAlchemy)."""

import pytest

from tests.abstract.connection_custom_resolver import (
    AbstractTestConnectionCustomResolver,
)


@pytest.fixture
def users_query(orm):
    def _users_query(model, info):
        # No baseline ordering: SQLAlchemy appends order_by rather than
        # replacing it, so a baseline would outrank the generated argument.
        return orm.get_default_queryset(model)

    return _users_query


@pytest.fixture
def narrow_by_name(User):
    def _narrow(select, name):
        return select.where(User.name == name)

    return _narrow


@pytest.fixture
def schema_execute(sa_session):
    def _execute(schema, query):
        return schema.execute_sync(query, context_value={"session": sa_session})

    return _execute


class TestConnectionCustomResolver(AbstractTestConnectionCustomResolver):
    pass
