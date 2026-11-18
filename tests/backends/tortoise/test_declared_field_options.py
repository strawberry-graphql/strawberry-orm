"""A field the library is asked to build carries everything it was declared with
(Tortoise)."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from strawberry_orm import StrawberryORM
from tests.abstract.declared_field_options import AbstractTestDeclaredFieldOptions
from tests.backends.tortoise.models import Comment, Post, User


@pytest.fixture
def make_orm():
    def _make(**options):
        return StrawberryORM.for_tortoise(
            warn_missing_scope=False, **{"lazy_resolution": "off", **options}
        )

    return _make


@pytest.fixture
def models(tortoise_db):
    """The models, once ``Tortoise.init`` has wired their relations."""
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


@pytest.fixture
def narrow():
    return SimpleNamespace(
        only=lambda query, model, field, value: query.filter(**{field: value}),
        exclude=lambda query, model, field, value: query.exclude(**{field: value}),
    )


@pytest_asyncio.fixture
async def held(seed):
    async def _held(model, *ids):
        return await model.filter(id__in=ids).order_by("id")

    return _held


@pytest_asyncio.fixture
async def run(seed):
    async def _run(schema, query, context):
        return await schema.execute(query, context_value=dict(context))

    return _run


class TestDeclaredFieldOptions(AbstractTestDeclaredFieldOptions):
    pass
