from types import SimpleNamespace

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from tests.abstract.hint_paths import AbstractTestHintPaths
from tests.backends.tortoise.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def lenient_orm():
    return StrawberryORM.for_tortoise(
        lazy_resolution="off", warn_missing_scope=False, strict_hints=False
    )


@pytest.fixture
def models(tortoise_db):
    """The models, once ``Tortoise.init`` has wired their relations."""
    return SimpleNamespace(User=User, Post=Post, Comment=Comment, Tag=Tag)


class TestHintPaths(AbstractTestHintPaths):
    @pytest.fixture(autouse=True)
    def _seed(self, orm, seed):
        self.backend = orm.backend

        async def materialized_posts(info: strawberry.Info):
            return list(await Post.all())

        self.materialized_posts = materialized_posts

    async def run(self, schema, query):
        with self.backend.query_probe(None) as probe:
            result = await schema.execute(query)
        self.query_count = probe.count
        return result

    @staticmethod
    def read(obj, name):
        field = obj._meta.fields_map[name]
        if type(field).__name__ in ("ForeignKeyFieldInstance", "OneToOneFieldInstance"):
            # A forward relation that was never loaded would be fetched here
            # unscoped, so reading one is an error in the test.
            assert f"_{name}" in obj.__dict__, f"{name} was not loaded"
            return obj.__dict__[f"_{name}"]
        return list(getattr(obj, name))

    @staticmethod
    def hide(qs, model, field, value):
        return qs.exclude(**{field: value})
