"""``auto`` and nullable columns (SQLAlchemy)."""

import pytest

from tests.abstract.auto_nullability import AbstractTestAutoNullability
from tests.backends.sqlalchemy.insert_only_models import Person


@pytest.fixture
def person_model():
    return Person


class TestAutoNullability(AbstractTestAutoNullability):
    pass
