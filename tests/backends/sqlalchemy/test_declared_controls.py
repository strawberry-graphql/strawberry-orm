"""A control declared on a field holds (SQLAlchemy)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from tests.abstract.declared_controls import AbstractTestDeclaredControls
from tests.backends.sqlalchemy.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


@pytest.fixture
def narrow():
    return SimpleNamespace(
        only=lambda query, model, field, value: query.where(
            getattr(model, field) == value
        ),
    )


@pytest.fixture
def first(sa_session):
    return lambda model: sa_session.scalars(select(model).order_by(model.id)).first()


@pytest.fixture
def run(sa_session, seed):
    async def _run(schema, query, variables=None):
        return schema.execute_sync(
            query, variable_values=variables, context_value={"session": sa_session}
        )

    return _run


class TestDeclaredControls(AbstractTestDeclaredControls):
    pass
