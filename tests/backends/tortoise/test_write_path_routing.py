"""A write is one unit on the connection a router sends it to.

With a router that sends writes to a primary, ``orm.apply_ref_list()`` and
``orm.apply_upsert()`` write there, not on the model's default connection. A
transaction opened on the default connection guarded none of those writes, so
a refusal partway through left every earlier item committed.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
import strawberry
from tortoise import Tortoise, fields
from tortoise.models import Model

from strawberry_orm import AbstractRepo, StrawberryORM
from strawberry_orm.upserts import _declared_fields


class Team(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)

    members: fields.ReverseRelation["Member"]

    class Meta:
        table = "routed_team"


class Member(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    team = fields.ForeignKeyField("routing.Team", related_name="members", null=True)

    class Meta:
        table = "routed_member"


class PrimaryRouter:
    def db_for_write(self, model):
        return "primary"


class MemberRepo(AbstractRepo):
    def can_link(self, parent, field, instance, info):
        return instance.name != "spam"


@strawberry.input
class NewMember:
    name: str


@pytest_asyncio.fixture
async def team(tmp_path):
    # Both names reach one database, as a primary and its replica hold one dataset.
    url = f"sqlite://{tmp_path / 'routed.sqlite3'}"
    await Tortoise.init(
        config={
            "connections": {"default": url, "primary": url},
            "apps": {
                "routing": {"models": [__name__], "default_connection": "default"}
            },
        },
        routers=[PrimaryRouter],
    )
    await Tortoise.generate_schemas()
    yield await Team.create(id=1, name="t1")
    await Tortoise.close_connections()


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(
        lazy_resolution="off", warn_missing_scope=False, repos={Member: MemberRepo}
    )


async def test_a_refused_ref_list_item_rolls_back_the_routed_writes(team, orm):
    ref = orm.ref(Member, create=NewMember)
    refs = [ref(create=NewMember(name="fine")), ref(create=NewMember(name="spam"))]
    with pytest.raises(PermissionError, match="can_link denied"):
        await orm.apply_ref_list(team, "members", refs, SimpleNamespace(context={}))
    assert await Member.all().values_list("name", flat=True) == []


async def test_a_refused_upsert_rolls_back_the_routed_writes(team, orm):
    upsert = orm.mutations.upsert_node_input(
        model=Team,
        project={
            "_meta": {
                "update": ["name"],
                "upsert": {"where": ["id"], "onMatch": "UPDATE"},
            },
            "members": {"_meta": {"create": ["name"]}},
        },
        name="RoutedTeamUpsert",
    )
    update = _declared_fields(upsert)["update"]
    item = _declared_fields(update)["members"]
    create = _declared_fields(item)["create"]
    payload = upsert(
        where=_declared_fields(upsert)["where"](id="1"),
        update=update(
            name="renamed",
            members=[
                item(create=create(name="fine")),
                item(create=create(name="spam")),
            ],
        ),
    )
    with pytest.raises(PermissionError, match="can_link denied"):
        await orm.apply_upsert(payload, SimpleNamespace(context={}))
    assert await Member.all().values_list("name", flat=True) == []
    assert (await Team.get(id=1)).name == "t1"
