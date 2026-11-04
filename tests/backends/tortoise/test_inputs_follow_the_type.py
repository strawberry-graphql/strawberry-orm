"""Filters, ordering, group-by and aggregates cover what the type shows (tortoise)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.inputs_follow_the_type import AbstractTestInputsFollowTheType
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    """The models, once ``Tortoise.init`` has wired their relations."""
    return SimpleNamespace(User=User, Post=Post)


class TestInputsFollowTheType(AbstractTestInputsFollowTheType):
    pass
