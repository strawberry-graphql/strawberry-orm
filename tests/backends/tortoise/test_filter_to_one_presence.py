"""A to-one relation into a scoped type: a hidden related row reads as none (Tortoise)."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from strawberry_orm import StrawberryORM
from tests.abstract.filter_to_one_presence import (
    AbstractTestToOnePresenceIntoAScopedType,
    AbstractTestToOnePresenceOfAReadableRow,
)
from tests.backends.tortoise.models import Comment


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(Comment=Comment)


@pytest.fixture
def narrow():
    return SimpleNamespace(
        exclude=lambda query, model, field, value: query.exclude(**{field: value}),
    )


@pytest_asyncio.fixture
async def run(seed):
    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


class TestToOnePresenceIntoAScopedType(AbstractTestToOnePresenceIntoAScopedType):
    pass


class TestToOnePresenceOfAReadableRow(AbstractTestToOnePresenceOfAReadableRow):
    pass
