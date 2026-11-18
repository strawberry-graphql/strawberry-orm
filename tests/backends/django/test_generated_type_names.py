from types import SimpleNamespace

import pytest

from strawberry_orm import PayloadPolicy, StrawberryORM
from tests.abstract.generated_type_names import AbstractTestGeneratedTypeNames
from tests.backends.django.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(
        lazy_resolution="off",
        warn_missing_scope=False,
        payload=PayloadPolicy(errors=str, on_error=lambda error: None),
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestGeneratedTypeNames(AbstractTestGeneratedTypeNames):
    pass
