"""``auto`` and nullable columns (Django)."""

import pytest

from tests.abstract.auto_nullability import AbstractTestAutoNullability
from tests.backends.django.insert_only_models import Person


@pytest.fixture
def person_model():
    return Person


class TestAutoNullability(AbstractTestAutoNullability):
    pass
