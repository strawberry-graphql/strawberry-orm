"""Filters, ordering, group-by and aggregates cover what the type shows (django)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.inputs_follow_the_type import AbstractTestInputsFollowTheType
from tests.backends.django.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


@pytest.mark.django_db
class TestInputsFollowTheType(AbstractTestInputsFollowTheType):
    pass
