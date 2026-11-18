from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import strawberry
from django.db import connection, models

from strawberry_orm import AbstractRepo
from tests.abstract.inputs_offer_no_row_keys import (
    ME,
    SECRET,
    THEM,
    AbstractTestACreateOnlyInserts,
    AbstractTestALinkKeepsItsRow,
    AbstractTestAnUpdateKeepsItsRow,
    AbstractTestInputsOfferNoRowKeys,
    KeyedDb,
    Shape,
    own_rows,
)
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)


class WkBinder(models.Model):
    owner = models.IntegerField(default=0)

    class Meta:
        app_label = "testapp"
        db_table = "wk_binder"


class WkDoc(models.Model):
    title = models.CharField(max_length=50)
    owner = models.IntegerField(default=0)
    binder = models.ForeignKey(
        WkBinder, null=True, on_delete=models.SET_NULL, related_name="docs"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wk_doc"


class WkDraft(WkDoc):
    class Meta:
        app_label = "testapp"
        proxy = True


class WkContract(WkDoc):
    terms = models.CharField(max_length=50, default="")
    holder = models.ForeignKey(
        WkBinder, null=True, on_delete=models.SET_NULL, related_name="contracts"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wk_contract"


class WkSealed(WkContract):
    seal = models.CharField(max_length=50, default="")
    keeper = models.ForeignKey(
        WkBinder, null=True, on_delete=models.SET_NULL, related_name="sealeds"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wk_sealed"


class WkNotice(WkDoc):
    base = models.OneToOneField(
        WkDoc, parent_link=True, primary_key=True, on_delete=models.CASCADE
    )
    board = models.ForeignKey(
        WkBinder, null=True, on_delete=models.SET_NULL, related_name="notices"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wk_notice"


class WkLedger(models.Model):
    number = models.IntegerField(primary_key=True)
    title = models.CharField(max_length=50)
    owner = models.IntegerField(default=0)

    class Meta:
        app_label = "testapp"
        db_table = "wk_ledger"


class WkEntry(WkLedger):
    book = models.ForeignKey(
        WkBinder, null=True, on_delete=models.SET_NULL, related_name="entries"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wk_entry"


class WkStamped(models.Model):
    title = models.CharField(max_length=50)
    owner = models.IntegerField(default=0)

    class Meta:
        app_label = "testapp"
        abstract = True


class WkMemo(WkStamped):
    folder = models.ForeignKey(
        WkBinder, null=True, on_delete=models.SET_NULL, related_name="memos"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wk_memo"


class WkBadge(models.Model):
    binder = models.OneToOneField(
        WkBinder, primary_key=True, on_delete=models.CASCADE, related_name="badge"
    )
    title = models.CharField(max_length=50)
    owner = models.IntegerField(default=0)

    class Meta:
        app_label = "testapp"
        db_table = "wk_badge"


class WkSlot(models.Model):
    binder = models.ForeignKey(
        WkBinder, primary_key=True, on_delete=models.CASCADE, related_name="slots"
    )
    title = models.CharField(max_length=50)
    owner = models.IntegerField(default=0)

    class Meta:
        app_label = "testapp"
        db_table = "wk_slot"


def _pin(model: type) -> models.ForeignKey:
    return models.ForeignKey(
        model, null=True, on_delete=models.SET_NULL, related_name="+"
    )


class WkPin(models.Model):
    draft = _pin(WkDraft)
    contract = _pin(WkContract)
    sealed = _pin(WkSealed)
    notice = _pin(WkNotice)
    entry = _pin(WkEntry)
    memo = _pin(WkMemo)
    badge = _pin(WkBadge)

    class Meta:
        app_label = "testapp"
        db_table = "wk_pin"


_DOC_KEYS = {"id", "wkdoc_ptr", "wkdoc_ptr_id"}

SHAPES = {
    "proxy": Shape(WkDraft, {"id"}, ("id",), WkDoc, link="draft"),
    "multi_table": Shape(
        WkContract,
        _DOC_KEYS,
        ("id", "wkdoc_ptr_id"),
        WkDoc,
        relation="contracts",
        link="contract",
    ),
    "multi_level": Shape(
        WkSealed,
        _DOC_KEYS | {"wkcontract_ptr", "wkcontract_ptr_id"},
        ("id", "wkdoc_ptr_id", "wkcontract_ptr_id"),
        WkContract,
        relation="sealeds",
        link="sealed",
    ),
    "named_parent_link": Shape(
        WkNotice,
        {"id", "base", "base_id"},
        ("id", "base_id"),
        WkDoc,
        relation="notices",
        link="notice",
    ),
    "named_parent_key": Shape(
        WkEntry,
        {"number", "wkledger_ptr", "wkledger_ptr_id"},
        ("number", "wkledger_ptr_id"),
        WkLedger,
        victim_key="number",
        relation="entries",
        link="entry",
    ),
    "abstract_base": Shape(
        WkMemo, {"id"}, ("id",), WkMemo, relation="memos", link="memo"
    ),
    "one_to_one_key": Shape(
        WkBadge,
        {"binder", "binder_id"},
        ("binder_id",),
        WkBadge,
        victim_key="binder_id",
        link="badge",
    ),
}

_TABLES = (
    WkBinder,
    WkDoc,
    WkContract,
    WkSealed,
    WkNotice,
    WkLedger,
    WkEntry,
    WkMemo,
    WkBadge,
    WkSlot,
    WkPin,
)


@dataclass
class DjangoKeyedDb(KeyedDb, DjangoWriteDb):
    def owned(self, query: Any, model: type) -> Any:
        return query.filter(owner=ME)


def _create_tables() -> None:
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in _TABLES:
            if model._meta.db_table not in existing:
                editor.create_model(model)
    for pk in (1, 2, 3):
        WkBinder.objects.create(id=pk, owner=THEM)


@pytest_asyncio.fixture(params=["sync", "async"])
async def keyed(request, setup_tables):
    await _on_db_thread(_create_tables)
    return DjangoKeyedDb(
        SimpleNamespace(Binder=WkBinder, Pin=WkPin, Slot=WkSlot),
        async_hooks=False,
        sync=request.param == "sync",
        shapes=SHAPES,
    )


@pytest.fixture(params=sorted(SHAPES))
def shape(request):
    return request.param


class TestInputsOfferNoRowKeys(AbstractTestInputsOfferNoRowKeys):
    pass


class TestACreateOnlyInserts(AbstractTestACreateOnlyInserts):
    pass


class TestAnUpdateKeepsItsRow(AbstractTestAnUpdateKeepsItsRow):
    pass


class TestALinkKeepsItsRow(AbstractTestALinkKeepsItsRow):
    pass


def test_a_ref_list_create_naming_a_hidden_parent_row_is_not_valid(setup_tables):
    """A client sends a multi-table child's create with its parent's key."""
    _create_tables()
    WkDoc.objects.create(id=4, title=SECRET, owner=THEM)
    keyed = DjangoKeyedDb(SimpleNamespace(), async_hooks=False)
    orm = keyed.new_orm(
        repos={WkDoc: own_rows(keyed, WkDoc), WkContract: own_rows(keyed, WkContract)}
    )
    create_input = orm.input(WkContract, name="WkContractInput")
    ref = orm.ref(WkContract, create=create_input)

    @strawberry.type
    class Query:
        ok: bool = True

    @strawberry.type
    class Mutation:
        @strawberry.mutation
        def edit_binder(self, info: strawberry.Info, contracts: list[ref]) -> bool:
            binder = WkBinder.objects.create(owner=ME)
            orm.apply_ref_list(binder, "contracts", contracts, info)
            return True

    schema = strawberry.Schema(query=Query, mutation=Mutation)
    result = schema.execute_sync(
        'mutation { editBinder(contracts: [{create: {id: 4, title: "pwned", '
        'terms: "t"}}]) }'
    )
    assert result.errors is not None
    assert "id" in result.errors[0].message
    victim = WkDoc.objects.get(pk=4)
    assert (victim.title, victim.owner) == (SECRET, THEM)
    assert not WkContract.objects.filter(pk=4).exists()


@pytest.mark.parametrize(
    ("values", "created"),
    [({"title": "new", "owner": ME}, True), ({"id": 4, "title": "pwned"}, False)],
)
def test_before_django_5_a_child_naming_a_parent_key_is_not_created(
    setup_tables, monkeypatch, values, created
):
    """Django 4.2's ``save()`` cannot force the parents' inserts."""
    import django

    _create_tables()
    WkDoc.objects.create(id=4, title=SECRET, owner=THEM)
    monkeypatch.setattr(django, "VERSION", (4, 2, 0, "final", 0))
    keyed = DjangoKeyedDb(SimpleNamespace(), async_hooks=False)
    repo = AbstractRepo(keyed.new_orm().backend)
    try:
        repo._create(WkContract, values, None)
    except ValueError as refused:
        assert "id" in str(refused)
    assert WkContract.objects.filter(title="new").exists() is created
    victim = WkDoc.objects.get(pk=4)
    assert (victim.title, victim.owner) == (SECRET, THEM)
