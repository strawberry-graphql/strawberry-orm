"""An app's own resolve extension on every field (Django)."""

import pytest

from tests.abstract.app_resolve_extensions import AbstractTestAppResolveExtensions


@pytest.fixture
def schema_execute():
    def _execute(schema, query):
        return schema.execute_sync(query, context_value={})

    return _execute


class TestAppResolveExtensions(AbstractTestAppResolveExtensions):
    pass
