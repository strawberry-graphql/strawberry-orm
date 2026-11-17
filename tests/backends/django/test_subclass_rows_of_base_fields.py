from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, models

from strawberry_orm import StrawberryORM
from tests.abstract.subclass_rows_of_base_fields import (
    AbstractTestSubclassRowsOfBaseFields,
)


class RaDocument(models.Model):
    title = models.CharField(max_length=100)
    is_published = models.BooleanField(default=False)

    class Meta:
        app_label = "testapp"
        db_table = "ra_document"


class RaProxyMemo(RaDocument):
    class Meta:
        proxy = True
        app_label = "testapp"


class RaChildMemo(RaDocument):
    note = models.CharField(max_length=100, default="")

    class Meta:
        app_label = "testapp"
        db_table = "ra_child_memo"


SEED = [("public", True), ("secret", True), ("draft", False)]


@pytest.fixture
def documents(setup_tables):
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (RaDocument, RaChildMemo):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    RaChildMemo.objects.all().delete()
    RaDocument.objects.all().delete()


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


class _DjangoSubclassRows(AbstractTestSubclassRowsOfBaseFields):
    memo_model: type

    @pytest.fixture
    def models(self):
        return SimpleNamespace(Document=RaDocument, Memo=self.memo_model)

    @pytest.fixture(autouse=True)
    def _seed(self, documents):
        for title, is_published in SEED:
            self.memo_model.objects.create(title=title, is_published=is_published)

    async def run(self, schema, query):
        return await sync_to_async(
            lambda: schema.execute_sync(query), thread_sensitive=True
        )()

    @staticmethod
    def published_only(queryset):
        return queryset.filter(is_published=True)

    @staticmethod
    def no_secrets(queryset):
        return queryset.exclude(title__startswith="secret")

    def memos(self):
        return self.memo_model.objects.order_by("id")


class TestProxyRowsOfBaseFields(_DjangoSubclassRows):
    memo_model = RaProxyMemo


class TestChildRowsOfBaseFields(_DjangoSubclassRows):
    memo_model = RaChildMemo
