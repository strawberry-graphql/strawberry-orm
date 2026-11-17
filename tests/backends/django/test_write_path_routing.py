"""A write is one unit on the database a router sends it to.

With a primary/replica router, the parent a resolver read came from the
replica, but ``orm.apply_ref_list()`` writes to the primary. A transaction
opened on the parent's database guarded none of those writes, so a refusal
partway through left every earlier item committed.
"""

from types import SimpleNamespace

import pytest
import strawberry
from asgiref.sync import sync_to_async
from django.db import connection, models
from django.test import override_settings

from strawberry_orm import AbstractRepo, StrawberryORM

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "replica"])


class WbTeam(models.Model):
    name = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp"
        db_table = "wb_team"


class WbMember(models.Model):
    name = models.CharField(max_length=20)
    team = models.ForeignKey(
        WbTeam, null=True, on_delete=models.CASCADE, related_name="members"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wb_member"


class PrimaryReplicaRouter:
    """Writes go to the primary; reads stay where Django would send them."""

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        return True


class MemberRepo(AbstractRepo):
    def can_link(self, parent, field, instance, info):
        return instance.name != "spam"


@strawberry.input
class NewMember:
    name: str


@pytest.fixture
def team(setup_tables):
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (WbTeam, WbMember):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    team = WbTeam.objects.create(id=1, name="t1")
    # As a resolver reading through the router's replica would find it.
    team._state.db = "replica"
    with override_settings(DATABASE_ROUTERS=[PrimaryReplicaRouter()]):
        yield team
    WbMember.objects.all().delete()
    WbTeam.objects.all().delete()


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_a_refused_item_rolls_back_the_writes_on_the_primary(team, mode):
    orm = StrawberryORM.for_django(
        lazy_resolution="off", warn_missing_scope=False, repos={WbMember: MemberRepo}
    )
    ref = orm.ref(WbMember, create=NewMember)
    refs = [ref(create=NewMember(name="fine")), ref(create=NewMember(name="spam"))]

    def apply():
        return orm.apply_ref_list(team, "members", refs, SimpleNamespace(context={}))

    with pytest.raises(PermissionError, match="can_link denied"):
        if mode == "sync":
            await sync_to_async(apply, thread_sensitive=True)()
        else:
            await apply()

    def stored():
        return list(WbMember.objects.using("default").values_list("name", flat=True))

    assert await sync_to_async(stored, thread_sensitive=True)() == []
