"""Tortoise keeps a subclass's rows in a table of its own, so a doc is a doc."""

from types import SimpleNamespace

import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from tests.abstract.tg_write_authorize_models import (
    AbstractTestEveryModelOfTheRowIsAsked,
)
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb


class TgShelf(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=50)

    class Meta:
        table = "tg_shelf"


class TgDoc(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    shelf = fields.ForeignKeyField(
        "tg_authorize_models.TgShelf",
        related_name="docs",
        null=True,
        on_delete=fields.SET_NULL,
    )

    class Meta:
        table = "tg_doc"


class TgPin(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=50)
    doc = fields.ForeignKeyField(
        "tg_authorize_models.TgDoc",
        related_name="pins",
        null=True,
        on_delete=fields.SET_NULL,
    )

    class Meta:
        table = "tg_pin"


@pytest_asyncio.fixture
async def writes():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"tg_authorize_models": [__name__]}
    )
    await Tortoise.generate_schemas()
    yield TortoiseWriteDb(
        SimpleNamespace(Shelf=TgShelf, Doc=TgDoc, Contract=TgDoc, Pin=TgPin), True
    )
    await Tortoise.close_connections()


class TestEveryModelOfTheRowIsAsked(AbstractTestEveryModelOfTheRowIsAsked):
    pass
