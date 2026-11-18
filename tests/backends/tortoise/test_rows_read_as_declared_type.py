from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.rows_read_as_declared_type import (
    AbstractTestRowsReadAsDeclaredType,
)


class DtFolder(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "dt_folder"


class DtDocument(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=100)
    is_published = fields.BooleanField(default=False)

    class Meta:
        table = "dt_document"


class DtMemo(DtDocument):
    """Tortoise gives a model inheriting another a table of its own."""

    holder = fields.ForeignKeyField(
        "dt_rows_read_as.DtFolder", related_name="memos", null=True
    )

    class Meta:
        table = "dt_memo"


@pytest_asyncio.fixture
async def seeded():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"dt_rows_read_as": [__name__]}
    )
    await Tortoise.generate_schemas()
    for pk in (1, 2):
        await DtFolder.create(id=pk)
    await DtMemo.create(id=1, title="public", is_published=True, holder_id=1)
    await DtMemo.create(id=2, title="secret", is_published=True, holder_id=1)
    await DtMemo.create(id=3, title="draft", holder_id=1)
    # Keyed as the draft memo is, in the base's own table.
    await DtDocument.create(id=3, title="document", is_published=True)
    yield
    await Tortoise.close_connections()


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(
        lazy_resolution="off", warn_missing_scope=False, payload=policy()
    )


@pytest.fixture
def models(seeded):
    return SimpleNamespace(Document=DtDocument, Memo=DtMemo, Folder=DtFolder)


class TestRowsReadAsDeclaredType(AbstractTestRowsReadAsDeclaredType):
    asynchronous = True
    #: A memo's row lives in its own table, so a read of the base finds none.
    documents_read = ["document"]
    #: Nor does a memo's row answer to the base's scope, only to its own type's.
    own_read = ["public", "draft"]

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
        return DtMemo.all().order_by("id")

    @staticmethod
    def documents():
        return DtDocument.all().order_by("id")

    def memos_of(self, folder):
        return self.memos().filter(holder_id=folder.id)

    @staticmethod
    async def read(query):
        return list(await query)
