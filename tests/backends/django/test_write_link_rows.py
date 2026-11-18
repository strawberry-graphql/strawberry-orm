"""Members go through ``ManyToManyField(through=LrMembership)``; fans through
the table Django creates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio
from django.db import connection, models

from tests.abstract.write_link_rows import AbstractTestLinkRowsAreWritesOfTheirModel
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)


class LrPerson(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "lr_person"


class LrClub(models.Model):
    name = models.CharField(max_length=50)
    members = models.ManyToManyField(
        LrPerson, through="LrMembership", related_name="clubs"
    )
    fans = models.ManyToManyField(LrPerson, related_name="fan_of")

    class Meta:
        app_label = "testapp"
        db_table = "lr_club"


class LrMembership(models.Model):
    club = models.ForeignKey(LrClub, on_delete=models.CASCADE)
    person = models.ForeignKey(LrPerson, on_delete=models.CASCADE)

    class Meta:
        app_label = "testapp"
        db_table = "lr_membership"


MODELS = SimpleNamespace(Person=LrPerson, Club=LrClub, Membership=LrMembership)


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


class TestLinkRowsAreWritesOfTheirModel(AbstractTestLinkRowsAreWritesOfTheirModel):
    pass
