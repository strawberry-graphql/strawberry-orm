"""A write saves the change it decided on, and nothing it only read.

Tortoise has no multi-table children, so its sealed doc is a model of its own.
"""

from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from tests.abstract.write_path_concurrent_changes import (
    AbstractTestAWriteSavesOnlyWhatItDecided,
    AbstractTestWhatAWriteSaves,
    RaceDb,
)
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb


class CwOwner(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    tier = fields.IntField(default=0)

    class Meta:
        table = "cw_owner"


class CwDoc(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    is_published = fields.BooleanField(default=False)
    owner = fields.ForeignKeyField("cw.CwOwner", related_name="docs")
    edited_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "cw_doc"


class CwSealedDoc(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    is_published = fields.BooleanField(default=False)
    owner = fields.ForeignKeyField("cw.CwOwner", related_name="sealed_docs")
    edited_at = fields.DatetimeField(auto_now=True)
    seal = fields.CharField(max_length=50, default="")

    class Meta:
        table = "cw_sealed_doc"


class CwNote(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)
    flagged = fields.BooleanField(default=False)
    doc = fields.ForeignKeyField(
        "cw.CwDoc", related_name="notes", null=True, on_delete=fields.SET_NULL
    )

    class Meta:
        table = "cw_note"


class CwBadge(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=50)
    doc = fields.OneToOneField(
        "cw.CwDoc", related_name="badge", null=True, on_delete=fields.SET_NULL
    )

    class Meta:
        table = "cw_badge"


MODELS = SimpleNamespace(
    Owner=CwOwner, Doc=CwDoc, SealedDoc=CwSealedDoc, Note=CwNote, Badge=CwBadge
)


class TortoiseRaceDb(TortoiseWriteDb, RaceDb):
    saves_async = True

    def change(self, model: type, pk: int, **values: Any) -> Any:
        return model.filter(pk=pk).update(**values)

    def remove(self, model: type, pk: int) -> Any:
        return model.filter(pk=pk).delete()

    async def find(self, model: type, pk: int) -> Any:
        return await model.filter(pk=pk).first()


@pytest_asyncio.fixture
async def writes():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"cw": [__name__]})
    await Tortoise.generate_schemas()
    yield TortoiseRaceDb(MODELS, async_hooks=True)
    await Tortoise.close_connections()


class TestAWriteSavesOnlyWhatItDecided(AbstractTestAWriteSavesOnlyWhatItDecided):
    pass


class TestWhatAWriteSaves(AbstractTestWhatAWriteSaves):
    pass
