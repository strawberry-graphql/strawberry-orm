"""A row scope means the same thing wherever it is applied (SQLAlchemy)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import and_, false, func, literal_column, select

from strawberry_orm import StrawberryORM
from tests.abstract.scope_conversions import AbstractTestScopeConversions
from tests.backends.sqlalchemy.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


class TestScopeConversions(AbstractTestScopeConversions):
    DENY = {
        "zero_limit": lambda stmt: stmt.limit(0),
        "false": lambda stmt: stmt.where(false()),
        "empty_in": lambda stmt: stmt.where(Post.id.in_([])),
        "contradiction": lambda stmt: stmt.where(
            and_(Post.title == "a", Post.title == "b")
        ),
    }
    NARROWING = {
        "published": lambda stmt: stmt.where(Post.is_published.is_(True)),
        "commented": lambda stmt: (
            stmt.join(Post.comments)
            .group_by(Post.id)
            .having(func.count(Comment.id) >= 1)
        ),
        "tagged": lambda stmt: stmt.join(Post.tags).distinct(),
    }
    BOUNDED = {
        "limit": lambda stmt: stmt.order_by(Post.id).limit(2),
        "offset": lambda stmt: stmt.order_by(Post.id).offset(1),
        "expression_limit": lambda stmt: stmt.order_by(Post.id).limit(
            literal_column("2")
        ),
        "fetch": lambda stmt: stmt.order_by(Post.id).fetch(2),
    }

    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self.session})

    @staticmethod
    def read(obj, name):
        return getattr(obj, name)

    @staticmethod
    def rows(info, model):
        return list(info.context["session"].scalars(select(model)))

    @staticmethod
    def first_ten(model):
        return select(model).order_by(model.id).limit(10)

    @staticmethod
    def nodes(info, model, ids):
        session = info.context["session"]
        return [session.get(model, pk) for pk in ids]
