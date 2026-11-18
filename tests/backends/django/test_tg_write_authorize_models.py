"""A multi-table child reads back through its parent as a row of the parent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio
from django.db import connection, models

from tests.abstract.tg_write_authorize_models import (
    AbstractTestEveryModelOfTheRowIsAsked,
)
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)


class TgShelf(models.Model):
    label = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "tg_shelf"


class TgDoc(models.Model):
    title = models.CharField(max_length=50)
    shelf = models.ForeignKey(
        TgShelf, null=True, on_delete=models.SET_NULL, related_name="docs"
    )

    class Meta:
        app_label = "testapp"
        db_table = "tg_doc"


class TgContract(TgDoc):
    class Meta:
        app_label = "testapp"
        db_table = "tg_contract"


class TgPin(models.Model):
    label = models.CharField(max_length=50)
    doc = models.ForeignKey(
        TgDoc, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        app_label = "testapp"
        db_table = "tg_pin"


MODELS = SimpleNamespace(Shelf=TgShelf, Doc=TgDoc, Contract=TgContract, Pin=TgPin)


def _create_tables() -> None:
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in vars(MODELS).values():
            if model._meta.db_table not in existing:
                editor.create_model(model)


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request, setup_tables):
    await _on_db_thread(_create_tables)
    return DjangoWriteDb(MODELS, async_hooks=False, sync=request.param == "sync")


class TestEveryModelOfTheRowIsAsked(AbstractTestEveryModelOfTheRowIsAsked):
    pass
