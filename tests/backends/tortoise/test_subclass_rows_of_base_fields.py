from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from strawberry_orm import StrawberryORM
from tests.abstract.subclass_rows_of_base_fields import (
    AbstractTestSubclassRowsOfBaseFields,
)


class RaDocument(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=100)
    is_published = fields.BooleanField(default=False)

    class Meta:
        table = "ra_document"


class RaMemo(RaDocument):
    """Tortoise gives a model inheriting another a table of its own."""

    note = fields.CharField(max_length=100, default="")

    class Meta:
        table = "ra_memo"


@pytest_asyncio.fixture
async def memos():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"ra_subclass_rows": [__name__]}
    )
    await Tortoise.generate_schemas()
    await RaMemo.create(id=1, title="public", is_published=True)
    await RaMemo.create(id=2, title="secret", is_published=True)
    await RaMemo.create(id=3, title="draft")
    yield
    await Tortoise.close_connections()


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(memos):
    return SimpleNamespace(Document=RaDocument, Memo=RaMemo)


class TestSubclassRowsOfBaseFields(AbstractTestSubclassRowsOfBaseFields):
    async def run(self, schema, query):
        return await schema.execute(query)

    @staticmethod
    def published_only(queryset):
        return queryset.filter(is_published=True)

    @staticmethod
    def no_secrets(queryset):
        return queryset.exclude(title__startswith="secret")

    @staticmethod
    def memos():
        return RaMemo.all().order_by("id")
