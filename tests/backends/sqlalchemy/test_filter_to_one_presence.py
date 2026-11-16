"""A to-one relation into a scoped type: a hidden related row reads as none (SQLAlchemy)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.filter_to_one_presence import (
    AbstractTestToOnePresenceIntoAScopedType,
    AbstractTestToOnePresenceOfAReadableRow,
)
from tests.backends.sqlalchemy.models import Comment


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(Comment=Comment)


@pytest.fixture
def narrow():
    return SimpleNamespace(
        exclude=lambda query, model, field, value: query.where(
            getattr(model, field) != value
        ),
    )


@pytest.fixture
def run(sa_session, seed):
    async def _run(schema, query, variables=None):
        return schema.execute_sync(
            query, variable_values=variables, context_value={"session": sa_session}
        )

    return _run


class TestToOnePresenceIntoAScopedType(AbstractTestToOnePresenceIntoAScopedType):
    pass


class TestToOnePresenceOfAReadableRow(AbstractTestToOnePresenceOfAReadableRow):
    pass
