from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.insert_only_schema import (
    AbstractTestAcceptanceExample,
    AbstractTestConfigurationErrors,
    AbstractTestGeneratedShapes,
    AbstractTestSnapshots,
)
from tests.backends.sqlalchemy.insert_only_models import Address, Event, Person
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(dialect="sqlite", lazy_resolution="off")


@pytest.fixture
def models():
    return SimpleNamespace(
        User=User, Post=Post, Person=Person, Address=Address, Event=Event
    )


class TestSnapshots(AbstractTestSnapshots):
    pass


class TestAcceptanceExample(AbstractTestAcceptanceExample):
    pass


class TestGeneratedShapes(AbstractTestGeneratedShapes):
    pass


class TestConfigurationErrors(AbstractTestConfigurationErrors):
    pass
