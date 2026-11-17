from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.hint_path_type_scopes import AbstractTestHintPathTypeScopes
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post)


class TestHintPathTypeScopes(AbstractTestHintPathTypeScopes):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await schema.execute(query)

    @staticmethod
    def read(obj, name):
        field = obj._meta.fields_map[name]
        if type(field).__name__ == "ForeignKeyFieldInstance":
            # A forward relation that was never loaded would be fetched here
            # unscoped, so reading one is an error in the test.
            assert f"_{name}" in obj.__dict__, f"{name} was not loaded"
            return obj.__dict__[f"_{name}"]
        return list(getattr(obj, name))

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    @staticmethod
    def hide(qs, model, field, value):
        return qs.exclude(**{field: value})
