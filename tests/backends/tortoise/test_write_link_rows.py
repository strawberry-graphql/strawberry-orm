"""Members go through ``lr_membership``, which ``LrMembership`` maps; fans
through the table Tortoise creates."""

from types import SimpleNamespace

import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from tests.abstract.write_link_rows import AbstractTestLinkRowsAreWritesOfTheirModel
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb


class LrPerson(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "lr_person"


class LrClub(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    members = fields.ManyToManyField(
        "lr_link_rows.LrPerson",
        through="lr_membership",
        forward_key="person_id",
        backward_key="club_id",
        related_name="clubs",
    )
    fans = fields.ManyToManyField("lr_link_rows.LrPerson", related_name="fan_of")

    class Meta:
        table = "lr_club"


class LrMembership(Model):
    id = fields.IntField(primary_key=True)
    club = fields.ForeignKeyField("lr_link_rows.LrClub", related_name="memberships")
    person = fields.ForeignKeyField("lr_link_rows.LrPerson", related_name="memberships")

    class Meta:
        table = "lr_membership"


@pytest_asyncio.fixture
async def writes():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"lr_link_rows": [__name__]}
    )
    await Tortoise.generate_schemas()
    yield TortoiseWriteDb(
        SimpleNamespace(Person=LrPerson, Club=LrClub, Membership=LrMembership), True
    )
    await Tortoise.close_connections()


class TestLinkRowsAreWritesOfTheirModel(AbstractTestLinkRowsAreWritesOfTheirModel):
    pass
