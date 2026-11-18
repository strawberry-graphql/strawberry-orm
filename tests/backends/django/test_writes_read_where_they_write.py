"""A write decides on the rows of the database it writes to."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from django.db import connections, models
from django.test import override_settings

from tests.abstract.writes_read_where_they_write import (
    ME,
    AbstractTestAWriteReadsWhereItWrites,
    ReplicaDb,
)
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "replica"])


class RwTag(models.Model):
    owner = models.CharField(max_length=50)
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "rw_tag"


class RwDoc(models.Model):
    owner = models.CharField(max_length=50)
    title = models.CharField(max_length=50)
    tags = models.ManyToManyField(RwTag, through="RwTagging", related_name="docs")

    class Meta:
        app_label = "testapp"
        db_table = "rw_doc"


class RwNote(models.Model):
    owner = models.CharField(max_length=50)
    text = models.CharField(max_length=50)
    doc = models.ForeignKey(
        RwDoc, null=True, on_delete=models.SET_NULL, related_name="notes"
    )

    class Meta:
        app_label = "testapp"
        db_table = "rw_note"


class RwGrant(models.Model):
    doc_id = models.IntegerField()
    user = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "rw_grant"


class RwTagging(models.Model):
    doc = models.ForeignKey(RwDoc, on_delete=models.CASCADE)
    tag = models.ForeignKey(RwTag, on_delete=models.CASCADE)
    owner = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "rw_tagging"


MODELS = SimpleNamespace(
    Tag=RwTag, Doc=RwDoc, Note=RwNote, Grant=RwGrant, Tagging=RwTagging
)

ALIASES = {"primary": "default", "replica": "replica"}


class ReplicaRouter:
    def db_for_read(self, model, **hints):
        return "replica"

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, *args, **hints):
        return True


@dataclass
class DjangoReplicaDb(DjangoWriteDb, ReplicaDb):
    def owned(self, query: Any, model: type) -> Any:
        return query.filter(owner=ME)

    def granted(self, doc_id: int) -> Any:
        return RwGrant.objects.filter(doc_id=doc_id, user=ME).exists()

    async def create_in(self, database: str, model: type, **values: Any) -> None:
        alias = ALIASES[database]
        await _on_db_thread(lambda: model._base_manager.using(alias).create(**values))

    async def primary_rows(self, model: type) -> list[Any]:
        return await _on_db_thread(
            lambda: list(model._base_manager.using("default").order_by("pk"))
        )

    async def fetch(self, model: type, pk: int) -> Any:
        return await _on_db_thread(lambda: model._default_manager.get(pk=pk))


def _fresh_tables() -> None:
    for alias in ALIASES.values():
        connection = connections[alias]
        existing = connection.introspection.table_names()
        with connection.schema_editor() as editor:
            for model in vars(MODELS).values():
                if model._meta.db_table not in existing:
                    editor.create_model(model)
        for model in vars(MODELS).values():
            model._base_manager.using(alias).all().delete()


@pytest_asyncio.fixture(params=["sync", "async"])
async def db(request, setup_tables):
    await _on_db_thread(_fresh_tables)
    with override_settings(DATABASE_ROUTERS=[ReplicaRouter()]):
        yield DjangoReplicaDb(MODELS, async_hooks=False, sync=request.param == "sync")


class TestAWriteReadsWhereItWrites(AbstractTestAWriteReadsWhereItWrites):
    pass
