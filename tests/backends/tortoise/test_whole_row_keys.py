"""A row is told apart from every other by its whole primary key (Tortoise).

Tortoise refuses a model with a key of several columns, so only the key of
one column is bound here.
"""

from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from strawberry_orm import StrawberryORM
from tests.abstract.whole_row_keys import (
    AbstractTestReadsTellRowsApart,
    AbstractTestWritesTellRowsApart,
    KeyedRows,
)


class WrTag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "wr_tag"


class WrLine(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=20)
    tag = fields.ForeignKeyField("wr_row_keys.WrTag", related_name="lines")

    class Meta:
        table = "wr_line"


class WrNote(Model):
    id = fields.IntField(primary_key=True)
    line_id = fields.IntField()
    text = fields.CharField(max_length=20)

    class Meta:
        table = "wr_note"


class TortoiseKeyedRows(KeyedRows):
    def orm(self, **kwargs: Any) -> Any:
        return StrawberryORM.for_tortoise(
            lazy_resolution="off", warn_missing_scope=False, **kwargs
        )

    def hide_secret(self, query: Any) -> Any:
        return query.exclude(label="secret")

    def is_first(self, query: Any, value: bool) -> Any:
        return query.exclude(id=2) if value else query.filter(id=2)

    def notes_of(self, line: Any) -> Any:
        return WrNote.filter(line_id=line.id).order_by("id")

    async def held(self) -> dict[str, Any]:
        return {row.label: row for row in await WrLine.all()}

    async def table(self) -> dict[Any, str]:
        return {row.pk: row.label for row in await WrLine.all()}


@pytest_asyncio.fixture
async def keyed():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"wr_row_keys": [__name__]})
    await Tortoise.generate_schemas()
    for id, name in enumerate(("plain", "hush", "other"), start=1):
        await WrTag.create(id=id, name=name)
    for id, label in enumerate(("shown", "secret", "other"), start=1):
        await WrLine.create(id=id, label=label, tag_id=id)
    await WrNote.create(id=1, line_id=1, text="first")
    await WrNote.create(id=2, line_id=2, text="second")
    keys = {"shown": 1, "secret": 2, "other": 3}
    yield TortoiseKeyedRows(
        m=SimpleNamespace(Tag=WrTag, Line=WrLine, Note=WrNote),
        keys=keys,
        ids={label: str(key) for label, key in keys.items()},
        part_id=None,
        key_names={"id"},
        moved=("id", 2),
    )
    await Tortoise.close_connections()


class TestReadsTellRowsApart(AbstractTestReadsTellRowsApart):
    pass


class TestWritesTellRowsApart(AbstractTestWritesTellRowsApart):
    pass
