"""Tortoise refuses an eager ``on=`` view (see ``test_on.py``), so only the
check on rows a field reads itself applies here."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.on_views import AbstractTestASelfReadFieldIsChecked
from tests.backends.tortoise.models import Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post, Tag=Tag)


class TestASelfReadFieldIsChecked(AbstractTestASelfReadFieldIsChecked):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await schema.execute(query)

    @staticmethod
    def hide(qs, model, field, value):
        return qs.exclude(**{field: value})

    @staticmethod
    async def own_posts(user, info):
        return list(await Post.filter(author_id=user.id).order_by("id"))
