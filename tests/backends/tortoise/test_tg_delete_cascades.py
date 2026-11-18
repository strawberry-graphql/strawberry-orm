"""A box's notes go with it through the foreign key's default ``on_delete``,
``CASCADE``, which the database carries out."""

from types import SimpleNamespace

import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from tests.abstract.tg_delete_cascades import AbstractTestADeleteAsksTheRowsItCascadesTo
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb


class TgcShelf(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=50)

    class Meta:
        table = "tgc_shelf"


class TgcBox(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=50)
    shelf = fields.ForeignKeyField(
        "tg_cascade_models.TgcShelf",
        related_name="boxes",
        null=True,
        on_delete=fields.SET_NULL,
    )

    class Meta:
        table = "tgc_box"


class TgcNote(Model):
    id = fields.IntField(primary_key=True)
    body = fields.CharField(max_length=50)
    box = fields.ForeignKeyField("tg_cascade_models.TgcBox", related_name="notes")

    class Meta:
        table = "tgc_note"


class TgcStamp(Model):
    id = fields.IntField(primary_key=True)
    mark = fields.CharField(max_length=50)
    note = fields.ForeignKeyField("tg_cascade_models.TgcNote", related_name="stamps")

    class Meta:
        table = "tgc_stamp"


class TgcPin(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=50)
    box = fields.ForeignKeyField(
        "tg_cascade_models.TgcBox",
        related_name="pins",
        null=True,
        on_delete=fields.SET_NULL,
    )

    class Meta:
        table = "tgc_pin"


@pytest_asyncio.fixture
async def writes():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"tg_cascade_models": [__name__]}
    )
    await Tortoise.generate_schemas()
    yield TortoiseWriteDb(
        SimpleNamespace(
            Shelf=TgcShelf, Box=TgcBox, Note=TgcNote, Stamp=TgcStamp, Pin=TgcPin
        ),
        True,
    )
    await Tortoise.close_connections()


class TestADeleteAsksTheRowsItCascadesTo(AbstractTestADeleteAsksTheRowsItCascadesTo):
    pass
