"""``auto`` and nullable columns (Tortoise)."""

import pytest

from tests.abstract.auto_nullability import AbstractTestAutoNullability
from tests.backends.tortoise.insert_only_models import Person


@pytest.fixture
def person_model():
    return Person


class TestAutoNullability(AbstractTestAutoNullability):
    pass
