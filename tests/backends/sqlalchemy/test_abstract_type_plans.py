from types import SimpleNamespace

import pytest
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from tests.abstract.abstract_type_plans import AbstractTestAbstractTypePlans
from tests.backends.sqlalchemy.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


class TestAbstractTypePlans(AbstractTestAbstractTypePlans):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query, variables):
        return schema.execute_sync(
            query, variable_values=variables, context_value={"session": self.session}
        )

    async def load_users(self):
        self.session.expunge_all()
        return list(self.session.scalars(select(User).order_by(User.id)))

    @staticmethod
    def read(obj, name):
        return getattr(obj, name)

    @staticmethod
    def published_only(qs):
        return qs.where(Post.is_published.is_(True))

    @staticmethod
    def hide(qs, model, field, value):
        return qs.where(getattr(model, field) != value)
