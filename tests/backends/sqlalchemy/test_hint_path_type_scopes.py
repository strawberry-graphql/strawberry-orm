from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.hint_path_type_scopes import AbstractTestHintPathTypeScopes
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestHintPathTypeScopes(AbstractTestHintPathTypeScopes):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self.session})

    @staticmethod
    def read(obj, name):
        return getattr(obj, name)

    @staticmethod
    def published_only(qs):
        return qs.where(Post.is_published.is_(True))

    @staticmethod
    def hide(qs, model, field, value):
        return qs.where(getattr(model, field) != value)
