"""A control declared on a field holds (Django)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.declared_controls import AbstractTestDeclaredControls
from tests.backends.django.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


@pytest.fixture
def narrow():
    return SimpleNamespace(
        only=lambda query, model, field, value: query.filter(**{field: value}),
    )


@pytest.fixture
def run(seed):
    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


class TestDeclaredControls(AbstractTestDeclaredControls):
    pass
