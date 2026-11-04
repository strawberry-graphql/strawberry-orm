"""Filters, ordering, group-by and aggregates cover what the type shows (sqlalchemy)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.inputs_follow_the_type import AbstractTestInputsFollowTheType
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestInputsFollowTheType(AbstractTestInputsFollowTheType):
    pass
