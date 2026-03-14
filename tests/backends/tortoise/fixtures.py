"""All fixtures for Tortoise backend tests."""

import pytest

from strawberry_orm import StrawberryORM
from tests.backends.tortoise.models import (
    Comment as TortComment,
    Post as TortPost,
    Tag as TortTag,
    User as TortUser,
)


# -- Fresh ORM instance -----------------------------------------------------

@pytest.fixture
def orm():
    return StrawberryORM("tortoise")


# -- Model class fixtures ----------------------------------------------------

@pytest.fixture
def User():
    return TortUser

@pytest.fixture
def Post():
    return TortPost

@pytest.fixture
def Tag():
    return TortTag

@pytest.fixture
def Comment():
    return TortComment
