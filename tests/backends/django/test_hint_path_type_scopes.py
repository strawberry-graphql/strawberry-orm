from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.hint_path_type_scopes import AbstractTestHintPathTypeScopes
from tests.backends.django.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestHintPathTypeScopes(AbstractTestHintPathTypeScopes):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await sync_to_async(
            lambda: schema.execute_sync(query), thread_sensitive=True
        )()

    @staticmethod
    def read(obj, name):
        field = obj._meta.get_field(name)
        if field.one_to_many or field.many_to_many:
            return list(getattr(obj, name).all())
        return getattr(obj, name)

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    @staticmethod
    def hide(qs, model, field, value):
        return qs.exclude(**{field: value})
