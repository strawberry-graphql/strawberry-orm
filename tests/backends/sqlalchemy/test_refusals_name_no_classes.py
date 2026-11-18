"""A query the library refuses is told why in the client's own terms (SQLAlchemy)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.refusals_name_no_classes import AbstractTestRefusalsNameNoClasses
from tests.backends.sqlalchemy.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


class TestRefusalsNameNoClasses(AbstractTestRefusalsNameNoClasses):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    @staticmethod
    def hide_bob(query):
        return query.where(User.name != "Bob")

    async def run(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self.session})
