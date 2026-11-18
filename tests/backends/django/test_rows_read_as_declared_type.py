from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, models

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.rows_read_as_declared_type import (
    AbstractTestRowsReadAsDeclaredType,
)


class DtFolder(models.Model):
    class Meta:
        app_label = "testapp"
        db_table = "dt_folder"


class DtDocument(models.Model):
    title = models.CharField(max_length=100)
    is_published = models.BooleanField(default=False)

    class Meta:
        app_label = "testapp"
        db_table = "dt_document"


class DtProxyMemo(DtDocument):
    class Meta:
        proxy = True
        app_label = "testapp"


class DtChildMemo(DtDocument):
    holder = models.ForeignKey(
        DtFolder, models.CASCADE, null=True, related_name="memos"
    )

    class Meta:
        app_label = "testapp"
        db_table = "dt_child_memo"


class DtProxyFolder(models.Model):
    """A proxy declares no foreign key, so a folder lists its rows through a
    many-to-many to the proxy itself."""

    memos = models.ManyToManyField(DtProxyMemo, related_name="folders")

    class Meta:
        app_label = "testapp"
        db_table = "dt_proxy_folder"


SEED = [("public", True), ("secret", True), ("draft", False)]


@pytest.fixture
def documents(setup_tables):
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (DtFolder, DtDocument, DtChildMemo, DtProxyFolder):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    DtProxyFolder.objects.all().delete()
    DtChildMemo.objects.all().delete()
    DtDocument.objects.all().delete()
    DtFolder.objects.all().delete()


@pytest.fixture
def orm():
    return StrawberryORM.for_django(
        lazy_resolution="off", warn_missing_scope=False, payload=policy()
    )


class _Django(AbstractTestRowsReadAsDeclaredType):
    memo_model: type
    folder_model: type
    documents_read = ["public", "secret"]
    own_read = ["public"]

    @pytest.fixture
    def models(self):
        return SimpleNamespace(
            Document=DtDocument, Memo=self.memo_model, Folder=self.folder_model
        )

    @pytest.fixture(autouse=True)
    def _seed(self, documents):
        folders = [self.folder_model.objects.create(id=pk) for pk in (1, 2)]
        memos = [
            self.memo_model.objects.create(
                id=pk, title=title, is_published=is_published
            )
            for pk, (title, is_published) in enumerate(SEED, start=1)
        ]
        self.file(folders[0], memos)

    async def run(self, schema, query):
        if self.asynchronous:
            return await schema.execute(query)
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

    @staticmethod
    def documents():
        return DtDocument.objects.order_by("id")

    def read(self, query):
        if self.asynchronous:
            return sync_to_async(list)(query)
        return list(query)


class _Proxy(_Django):
    memo_model = DtProxyMemo
    folder_model = DtProxyFolder

    @staticmethod
    def file(folder, memos):
        folder.memos.add(*memos)

    def memos_of(self, folder):
        return self.memos().filter(folders=folder.id)


class _Child(_Django):
    memo_model = DtChildMemo
    folder_model = DtFolder

    @staticmethod
    def file(folder, memos):
        DtChildMemo.objects.filter(id__in=[memo.id for memo in memos]).update(
            holder=folder
        )

    def memos_of(self, folder):
        return self.memos().filter(holder_id=folder.id)


class TestProxySync(_Proxy):
    asynchronous = False


class TestProxyAsync(_Proxy):
    asynchronous = True


class TestChildSync(_Child):
    asynchronous = False


class TestChildAsync(_Child):
    asynchronous = True
