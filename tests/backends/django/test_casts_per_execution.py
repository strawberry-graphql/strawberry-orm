from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, models

from strawberry_orm import StrawberryORM
from tests.abstract.casts_per_execution import AbstractTestCastsPerExecution


class DtMember(models.Model):
    name = models.CharField(max_length=50)
    email = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp"
        db_table = "dt_member"


@pytest.fixture
def cached(setup_tables):
    if DtMember._meta.db_table not in connection.introspection.table_names():
        with connection.schema_editor() as editor:
            editor.create_model(DtMember)
    DtMember.objects.all().delete()
    DtMember.objects.create(id=1, name="ada", email="ada@corp.example")
    return list(DtMember.objects.all())


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(Member=DtMember)


class _Django(AbstractTestCastsPerExecution):
    async def run(self, schema, query, context):
        if self.asynchronous:
            return await schema.execute(query, context_value=context)
        return await sync_to_async(
            lambda: schema.execute_sync(query, context_value=context),
            thread_sensitive=True,
        )()


class TestCastsPerExecutionSync(_Django):
    asynchronous = False


class TestCastsPerExecutionAsync(_Django):
    asynchronous = True
