"""A query the library refuses is told why in the client's own terms (Tortoise)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.refusals_name_no_classes import AbstractTestRefusalsNameNoClasses
from tests.backends.tortoise.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


class TestRefusalsNameNoClasses(AbstractTestRefusalsNameNoClasses):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    @staticmethod
    def hide_bob(query):
        return query.exclude(name="Bob")

    async def run(self, schema, query):
        return await schema.execute(query, context_value={})
