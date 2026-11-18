"""Generated inputs read the words of a name (Tortoise)."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise import Tortoise

from strawberry_orm import StrawberryORM
from tests.abstract.fb_sensitive_names import AbstractTestSensitiveNameWords
from tests.backends.tortoise.fb_vault_models import FbVault


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest_asyncio.fixture
async def models():
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["tests.backends.tortoise.fb_vault_models"]},
    )
    yield SimpleNamespace(FbVault=FbVault)
    await Tortoise.close_connections()


class TestSensitiveNameWords(AbstractTestSensitiveNameWords):
    pass
