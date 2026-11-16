"""A to-one relation into a scoped type: a hidden related row reads as none (Django)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.filter_to_one_presence import (
    AbstractTestToOnePresenceIntoAScopedType,
    AbstractTestToOnePresenceOfAReadableRow,
)
from tests.backends.django.models import Comment


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(Comment=Comment)


@pytest.fixture
def narrow():
    return SimpleNamespace(
        exclude=lambda query, model, field, value: query.exclude(**{field: value}),
    )


@pytest.fixture
def run(seed):
    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


class TestToOnePresenceIntoAScopedType(AbstractTestToOnePresenceIntoAScopedType):
    pass


class TestToOnePresenceOfAReadableRow(AbstractTestToOnePresenceOfAReadableRow):
    pass
