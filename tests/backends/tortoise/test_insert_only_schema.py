from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise import Tortoise

from strawberry_orm import StrawberryORM
from tests.abstract.insert_only_schema import (
    AbstractTestAcceptanceExample,
    AbstractTestConfigurationErrors,
    AbstractTestGeneratedShapes,
    AbstractTestSnapshots,
)
from tests.backends.tortoise.insert_only_models import Address, Event, Person
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off")


@pytest_asyncio.fixture
async def models():
    """The models, once ``Tortoise.init`` has wired their relations."""
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={
            "models": [
                "tests.backends.tortoise.models",
                "tests.backends.tortoise.insert_only_models",
            ]
        },
    )
    yield SimpleNamespace(
        User=User, Post=Post, Person=Person, Address=Address, Event=Event
    )
    await Tortoise.close_connections()


class TestSnapshots(AbstractTestSnapshots):
    pass


class TestAcceptanceExample(AbstractTestAcceptanceExample):
    pass


class TestGeneratedShapes(AbstractTestGeneratedShapes):
    pass


class TestConfigurationErrors(AbstractTestConfigurationErrors):
    pass
