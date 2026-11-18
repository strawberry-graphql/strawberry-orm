from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from django.db import connection, models

from tests.abstract.write_path_identity_columns import (
    ME,
    AbstractTestACreateKeepsItsModel,
    AbstractTestALinkKeepsItsRowAndModel,
    AbstractTestAnUpdateKeepsItsRowAndModel,
    AbstractTestInputsOfferNoIdentityColumn,
    Held,
    IdentityDb,
    Shape,
)
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)


class IcHolder(models.Model):
    owner = models.IntegerField(default=0)

    class Meta:
        app_label = "testapp"
        db_table = "ic_holder"


class IcTenant(models.Model):
    owner = models.IntegerField(default=0)

    class Meta:
        app_label = "testapp"
        db_table = "ic_tenant"


def _holder(related_name: str) -> models.ForeignKey:
    return models.ForeignKey(
        IcHolder, null=True, on_delete=models.SET_NULL, related_name=related_name
    )


class IcNote(models.Model):
    title = models.CharField(max_length=50)
    owner = models.IntegerField(default=0)
    holder = _holder("notes")

    class Meta:
        app_label = "testapp"
        db_table = "ic_note"


class IcBadge(models.Model):
    pk = models.CompositePrimaryKey("tenant", "code")
    tenant = models.IntegerField()
    code = models.CharField(max_length=20)
    title = models.CharField(max_length=50)
    owner = models.IntegerField(default=0)
    holder = _holder("badges")

    class Meta:
        app_label = "testapp"
        db_table = "ic_badge"


class IcSlot(models.Model):
    pk = models.CompositePrimaryKey("tenant_id", "code")
    tenant = models.ForeignKey(IcTenant, on_delete=models.CASCADE, related_name="slots")
    code = models.CharField(max_length=20)
    title = models.CharField(max_length=50)
    owner = models.IntegerField(default=0)
    holder = _holder("slots")

    class Meta:
        app_label = "testapp"
        db_table = "ic_slot"


class IcSeat(models.Model):
    tenant = models.ForeignKey(
        IcTenant, primary_key=True, on_delete=models.CASCADE, related_name="seats"
    )
    title = models.CharField(max_length=50)
    owner = models.IntegerField(default=0)
    holder = _holder("seats")

    class Meta:
        app_label = "testapp"
        db_table = "ic_seat"


def _pin(model: type) -> models.ForeignKey:
    return models.ForeignKey(
        model, null=True, on_delete=models.SET_NULL, related_name="+"
    )


class IcPin(models.Model):
    note = _pin(IcNote)
    seat = _pin(IcSeat)

    class Meta:
        app_label = "testapp"
        db_table = "ic_pin"


# Django has no foreign key to a model whose key is made of several columns.
SHAPES = {
    "key": Shape(
        IcNote,
        {"id"},
        mine={"id": 1},
        theirs={"id": 2},
        moves=[{"id": 2}, {"id": 50}],
        forges=[{"id": 2}],
        columns=("id",),
        own_id="1",
        relation="notes",
        link="note",
    ),
    "composite": Shape(
        IcBadge,
        {"pk", "tenant", "code"},
        mine={"tenant": 1, "code": "a"},
        theirs={"tenant": 2, "code": "a"},
        moves=[{"tenant": 2}, {"code": "z"}],
        forges=[{"tenant": 2, "code": "a"}],
        columns=("tenant", "code"),
        own_id='[1, "a"]',
        relation="badges",
        keyed_by_hook={"tenant": 5, "code": "n"},
    ),
    "composite_foreign_key": Shape(
        IcSlot,
        {"pk", "tenant", "tenant_id", "code"},
        mine={"tenant_id": 1, "code": "a"},
        theirs={"tenant_id": 2, "code": "a"},
        moves=[{"tenant_id": 2}, {"code": "z"}],
        forges=[{"tenant_id": 2, "code": "a"}],
        columns=("tenant_id", "code"),
        own_id='[1, "a"]',
        relation="slots",
        held_by=Held(IcTenant, "slots", mine=1, other=3),
        keyed_by_hook={"tenant_id": 3, "code": "n"},
    ),
    "foreign_key_key": Shape(
        IcSeat,
        {"tenant", "tenant_id"},
        mine={"tenant_id": 1},
        theirs={"tenant_id": 2},
        moves=[{"tenant_id": 2}, {"tenant_id": 3}],
        forges=[{"tenant_id": 2}],
        columns=("tenant_id",),
        own_id="1",
        relation="seats",
        link="seat",
        held_by=Held(IcTenant, "seats", mine=1, other=3),
        keyed_by_hook={"tenant_id": 3},
    ),
}

_TABLES = (IcHolder, IcTenant, IcNote, IcBadge, IcSlot, IcSeat, IcPin)


@dataclass
class DjangoIdentityDb(IdentityDb, DjangoWriteDb):
    def owned(self, query: Any, model: type) -> Any:
        return query.filter(owner=ME)

    async def fetch(self, model: type, pk: Any) -> Any:
        return await _on_db_thread(lambda: model.objects.get(pk=pk))


def _create_tables() -> None:
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in _TABLES:
            if model._meta.db_table not in existing:
                editor.create_model(model)


@pytest_asyncio.fixture(params=["sync", "async"])
async def identity(request, setup_tables):
    await _on_db_thread(_create_tables)
    return DjangoIdentityDb(
        SimpleNamespace(Holder=IcHolder, Pin=IcPin),
        async_hooks=False,
        sync=request.param == "sync",
        shapes=SHAPES,
    )


@pytest.fixture(params=sorted(SHAPES))
def shape(request):
    return request.param


class TestInputsOfferNoIdentityColumn(AbstractTestInputsOfferNoIdentityColumn):
    pass


class TestAnUpdateKeepsItsRowAndModel(AbstractTestAnUpdateKeepsItsRowAndModel):
    pass


class TestACreateKeepsItsModel(AbstractTestACreateKeepsItsModel):
    pass


class TestALinkKeepsItsRowAndModel(AbstractTestALinkKeepsItsRowAndModel):
    pass
