from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise import Tortoise

from strawberry_orm import StrawberryORM
from tests.abstract.inputs_sensitive_names import AbstractTestSensitiveNames
from tests.backends.tortoise.account_models import Account


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest_asyncio.fixture
async def models():
    """The models, once ``Tortoise.init`` has wired their relations."""
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["tests.backends.tortoise.account_models"]},
    )
    yield SimpleNamespace(Account=Account)
    await Tortoise.close_connections()


class TestSensitiveNames(AbstractTestSensitiveNames):
    pass
