"""A box's notes go with it through ``on_delete=CASCADE``; a sealed note is a
multi-table child, which reads as a plain note."""

from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio
from django.db import connection, models

from tests.abstract.tg_delete_cascades import AbstractTestADeleteAsksTheRowsItCascadesTo
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)


class TgcShelf(models.Model):
    label = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "tgc_shelf"


class TgcBox(models.Model):
    label = models.CharField(max_length=50)
    shelf = models.ForeignKey(
        TgcShelf, null=True, on_delete=models.SET_NULL, related_name="boxes"
    )

    class Meta:
        app_label = "testapp"
        db_table = "tgc_box"


class TgcNote(models.Model):
    body = models.CharField(max_length=50)
    box = models.ForeignKey(TgcBox, on_delete=models.CASCADE, related_name="notes")

    class Meta:
        app_label = "testapp"
        db_table = "tgc_note"


class TgcSealedNote(TgcNote):
    class Meta:
        app_label = "testapp"
        db_table = "tgc_sealed_note"


class TgcStamp(models.Model):
    mark = models.CharField(max_length=50)
    note = models.ForeignKey(TgcNote, on_delete=models.CASCADE, related_name="stamps")

    class Meta:
        app_label = "testapp"
        db_table = "tgc_stamp"


class TgcPin(models.Model):
    label = models.CharField(max_length=50)
    box = models.ForeignKey(
        TgcBox, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        app_label = "testapp"
        db_table = "tgc_pin"


MODELS = SimpleNamespace(
    Shelf=TgcShelf,
    Box=TgcBox,
    Note=TgcNote,
    SealedNote=TgcSealedNote,
    Stamp=TgcStamp,
    Pin=TgcPin,
)


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


class TestADeleteAsksTheRowsItCascadesTo(AbstractTestADeleteAsksTheRowsItCascadesTo):
    pass
