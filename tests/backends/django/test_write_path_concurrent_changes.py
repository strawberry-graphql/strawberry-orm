"""A write saves the change it decided on, and nothing it only read."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from django.db import connection, models

from tests.abstract.write_path_concurrent_changes import (
    AbstractTestAWriteSavesOnlyWhatItDecided,
    AbstractTestWhatAWriteSaves,
    RaceDb,
)
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)


class CwOwner(models.Model):
    name = models.CharField(max_length=50)
    tier = models.IntegerField(default=0)

    class Meta:
        app_label = "testapp"
        db_table = "cw_owner"


class CwDoc(models.Model):
    title = models.CharField(max_length=50)
    is_published = models.BooleanField(default=False)
    owner = models.ForeignKey(CwOwner, on_delete=models.CASCADE, related_name="docs")
    edited_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "testapp"
        db_table = "cw_doc"


class CwSealedDoc(CwDoc):
    seal = models.CharField(max_length=50, default="")

    class Meta:
        app_label = "testapp"
        db_table = "cw_sealed_doc"


class CwNote(models.Model):
    text = models.CharField(max_length=50)
    flagged = models.BooleanField(default=False)
    doc = models.ForeignKey(
        CwDoc, null=True, on_delete=models.SET_NULL, related_name="notes"
    )

    class Meta:
        app_label = "testapp"
        db_table = "cw_note"


class CwBadge(models.Model):
    label = models.CharField(max_length=50)
    doc = models.OneToOneField(
        CwDoc, null=True, on_delete=models.SET_NULL, related_name="badge"
    )

    class Meta:
        app_label = "testapp"
        db_table = "cw_badge"


MODELS = SimpleNamespace(
    Owner=CwOwner, Doc=CwDoc, SealedDoc=CwSealedDoc, Note=CwNote, Badge=CwBadge
)


@dataclass
class DjangoRaceDb(DjangoWriteDb, RaceDb):
    def change(self, model: type, pk: int, **values: Any) -> None:
        model._base_manager.filter(pk=pk).update(**values)

    def remove(self, model: type, pk: int) -> None:
        model._base_manager.filter(pk=pk).delete()

    async def find(self, model: type, pk: int) -> Any:
        return await _on_db_thread(lambda: model._base_manager.filter(pk=pk).first())

    async def save(self, repo: Any, row: Any) -> None:
        await _on_db_thread(lambda: repo._save(row, self.info))


def _create_tables() -> None:
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in vars(MODELS).values():
            if model._meta.db_table not in existing:
                editor.create_model(model)


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request, setup_tables):
    await _on_db_thread(_create_tables)
    return DjangoRaceDb(MODELS, async_hooks=False, sync=request.param == "sync")


class TestAWriteSavesOnlyWhatItDecided(AbstractTestAWriteSavesOnlyWhatItDecided):
    pass


class TestWhatAWriteSaves(AbstractTestWhatAWriteSaves):
    pass
