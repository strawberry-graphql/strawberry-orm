from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.db import connection

from strawberry_orm import StrawberryORM
from tests.abstract.insert_only_upserts import (
    AbstractTestApplyRefList,
    AbstractTestBooks,
    AbstractTestFindOrCreate,
    AbstractTestForwardLinks,
    AbstractTestHandBuiltPayloads,
    AbstractTestHooks,
    AbstractTestOnMatch,
    AbstractTestUpdateOnMatch,
    AbstractTestValidation,
    UpsertDb,
    build_schema,
    resolve,
)
from tests.backends.django.insert_only_models import Address, Event, Group, Person
from tests.backends.django.models import Book, Post, Publisher, Tag, User

ORM = StrawberryORM.for_django(
    lazy_resolution="off", warn_missing_scope=False, warn_sensitive=False
)
BUILT = build_schema(
    ORM,
    SimpleNamespace(
        Person=Person,
        Address=Address,
        Event=Event,
        Group=Group,
        User=User,
        Post=Post,
        Tag=Tag,
        Publisher=Publisher,
        Book=Book,
    ),
)
#: Tables this module adds to the ones the shared Django fixtures manage.
_TABLES = [Person, Group, Address, Event, Publisher, Book]


def _on_db_thread(call: Callable[[], Any]) -> Any:
    """Run *call* on the one thread every ``thread_sensitive`` call shares."""
    return sync_to_async(call, thread_sensitive=True)()


@dataclass
class DjangoDb(UpsertDb):
    #: GraphQL ``execute_sync`` on the database thread, or async ``execute``.
    sync: bool = True

    async def write(self, call: Callable[[], Any]) -> Any:
        if self.sync:
            return await _on_db_thread(call)
        return await resolve(call())

    async def run_query(self, query: str) -> Any:
        if self.sync:
            return await _on_db_thread(lambda: self.built.schema.execute_sync(query))
        return await self.built.schema.execute(query)

    async def create(self, model: type, **values: Any) -> Any:
        return await _on_db_thread(lambda: model.objects.create(**values))

    async def add_related(self, instance: Any, name: str, *rows: Any) -> None:
        await _on_db_thread(lambda: getattr(instance, name).add(*rows))

    async def rows(self, model: type) -> list[Any]:
        return await _on_db_thread(lambda: list(model.objects.order_by("pk")))

    async def reload(self, instance: Any) -> Any:
        return await _on_db_thread(lambda: type(instance).objects.get(pk=instance.pk))

    async def related(self, instance: Any, name: str) -> list[Any]:
        return await _on_db_thread(lambda: list(getattr(instance, name).all()))


def _create_tables() -> None:
    existing = set(connection.introspection.table_names())
    with connection.schema_editor() as editor:
        for model in _TABLES:
            if model._meta.db_table not in existing:
                editor.create_model(model)


def _empty_tables() -> None:
    Person.groups.through.objects.all().delete()
    for model in reversed(_TABLES):
        model.objects.all().delete()
    with connection.cursor() as cursor:
        for model in _TABLES:
            cursor.execute(
                "DELETE FROM sqlite_sequence WHERE name = %s", [model._meta.db_table]
            )


@pytest.fixture
def upsert_tables(setup_tables):
    _create_tables()
    yield
    _empty_tables()


@pytest_asyncio.fixture(params=["sync", "async"])
async def db(request, upsert_tables):
    harness = DjangoDb(BUILT, async_hooks=False, sync=request.param == "sync")

    def record(execute, sql, params, many, context):
        harness.statements.append(sql)
        return execute(sql, params, many, context)

    await _on_db_thread(lambda: connection.execute_wrappers.append(record))
    yield harness
    await _on_db_thread(lambda: connection.execute_wrappers.remove(record))


class TestFindOrCreate(AbstractTestFindOrCreate):
    pass


class TestOnMatch(AbstractTestOnMatch):
    pass


class TestForwardLinks(AbstractTestForwardLinks):
    pass


class TestValidation(AbstractTestValidation):
    pass


class TestHandBuiltPayloads(AbstractTestHandBuiltPayloads):
    pass


class TestHooks(AbstractTestHooks):
    pass


class TestApplyRefList(AbstractTestApplyRefList):
    pass


class TestUpdateOnMatch(AbstractTestUpdateOnMatch):
    pass


class TestBooks(AbstractTestBooks):
    pass
