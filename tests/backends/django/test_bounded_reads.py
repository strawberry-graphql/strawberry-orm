"""Every page and root list is cut by the database (Django).

Run under ``execute_sync`` and under ``execute``.
"""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from django.db import connection, models
from django.db.backends.sqlite3.base import SQLiteCursorWrapper
from django.db.models.signals import post_init

from strawberry_orm import StrawberryORM
from tests.abstract.bounded_reads import AbstractTestBoundedReads


class UbOwner(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp"
        db_table = "ub_owner"


class UbItem(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=20)
    bucket = models.IntegerField()
    hidden = models.BooleanField(default=False)
    owner = models.ForeignKey(UbOwner, on_delete=models.CASCADE, related_name="items")

    class Meta:
        app_label = "testapp"
        db_table = "ub_item"


@pytest.fixture
def seeded(setup_tables):
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (UbOwner, UbItem):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    for i in (1, 2, 3):
        UbOwner.objects.create(id=i, name=f"o{i}")
    UbItem.objects.bulk_create(
        UbItem(
            id=i,
            name=f"i{i:02}",
            bucket=i % 3,
            hidden=i in (2, 15),
            owner_id=(i - 1) // 10 + 1,
        )
        for i in range(1, 31)
    )
    yield
    UbItem.objects.all().delete()
    UbOwner.objects.all().delete()


@pytest.fixture
def make_orm():
    def _make(**options):
        options.setdefault("lazy_resolution", "off")
        return StrawberryORM.for_django(
            warn_missing_scope=False, default_query_limit=5, **options
        )

    return _make


@pytest.fixture
def Owner():
    return UbOwner


@pytest.fixture
def Item():
    return UbItem


@pytest.fixture
def hide():
    return lambda query: query.filter(hidden=False)


@pytest.fixture
def all_items():
    return lambda: UbItem.objects.all()


@pytest.fixture
def owner_items():
    return lambda owner: UbItem.objects.filter(owner_id=owner.pk)


@pytest.fixture
def capture(monkeypatch):
    @contextmanager
    def _capture():
        seen = SimpleNamespace(loaded=0, params=[])
        original = SQLiteCursorWrapper.execute

        def execute(self, query, params=None):
            seen.params.append(len(params or ()))
            return original(self, query, params)

        def count(sender, instance, **kwargs):
            seen.loaded += 1

        monkeypatch.setattr(SQLiteCursorWrapper, "execute", execute)
        post_init.connect(count, sender=UbItem, weak=False)
        try:
            yield seen
        finally:
            post_init.disconnect(count, sender=UbItem)
            monkeypatch.setattr(SQLiteCursorWrapper, "execute", original)

    return _capture


@pytest.fixture(params=["sync", "async"])
def run(request):
    async def _run(schema, query):
        if request.param == "sync":
            return await asyncio.to_thread(schema.execute_sync, query)
        return await schema.execute(query)

    return _run


class TestBoundedReads(AbstractTestBoundedReads):
    pass
