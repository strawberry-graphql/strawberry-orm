"""An app's own resolve extension on every field (SQLAlchemy)."""

import pytest

from tests.abstract.app_resolve_extensions import AbstractTestAppResolveExtensions


@pytest.fixture
def schema_execute(sa_session):
    def _execute(schema, query):
        return schema.execute_sync(query, context_value={"session": sa_session})

    return _execute


class TestAppResolveExtensions(AbstractTestAppResolveExtensions):
    pass
