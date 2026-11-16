from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.inputs_sensitive_names import AbstractTestSensitiveNames
from tests.backends.django.account_models import Account


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(Account=Account)


class TestSensitiveNames(AbstractTestSensitiveNames):
    pass
