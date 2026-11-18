"""A filter value a column's validator refuses is told to the client plainly.

Tortoise runs a column's validators as it turns a filter's value into the
column's, and its ``ValidationError`` names the field, the validator's pattern
or bound and the value. Wherever the value is filtered on - a list, a page,
its ``totalCount``, aggregates and groups, through a relation, or on a
relation's rows - the client is told "Invalid filter expression", and the
original is kept as the cause, for the server log.
"""

import pytest
import pytest_asyncio
import strawberry
from strawberry import relay
from tortoise import Tortoise, fields
from tortoise.exceptions import ValidationError
from tortoise.models import Model
from tortoise.validators import MinValueValidator, RegexValidator

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class DzLedger(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    accounts: fields.ReverseRelation["DzAccount"]

    class Meta:
        table = "dz_ledger"


class DzAccount(Model):
    id = fields.IntField(primary_key=True)
    handle = fields.CharField(max_length=12)
    ledger_code = fields.CharField(
        max_length=40, validators=[RegexValidator(r"^ACME-[0-9]{4}-INT$", 0)]
    )
    credit = fields.IntField(default=0, validators=[MinValueValidator(-5000)])
    ledger = fields.ForeignKeyField("dz_validated.DzLedger", related_name="accounts")

    notes: fields.ReverseRelation["DzNote"]

    class Meta:
        table = "dz_account"


class DzNote(Model):
    id = fields.IntField(primary_key=True)
    body = fields.CharField(max_length=50)
    account = fields.ForeignKeyField("dz_validated.DzAccount", related_name="notes")

    class Meta:
        table = "dz_note"


#: Each validator's refusal gives away, by name, the rule it enforces.
REFUSED_VALUES = {
    "a pattern": '{ledgerCode: {inList: ["zzz"]}}',
    "a minimum": "{credit: {exact: -999999}}",
    "a length": '{handle: {exact: "%s"}}' % ("A" * 30),
}
LEAKS = ("ACME", "-5000", "12", "ledger_code", "Length", "regex")

READS = {
    "list": "{ accounts(filter: {field: %s}) { handle } }",
    "negated": "{ accounts(filter: {not: {field: %s}}) { handle } }",
    "edges": "{ accountPages(first: 5, filter: {field: %s}) { edges { node { id } } } }",
    "totalCount": "{ accountPages(first: 5, filter: {field: %s}) { totalCount } }",
    "aggregates": (
        "{ accountPages(filter: {field: %s}) { aggregates { count sum { credit } } } }"
    ),
    "groups": (
        "{ accountPages(filter: {field: %s}, groupBy: [{field: {handle: true}}]) "
        "{ groups { key { handle } } } }"
    ),
    "through a relation": "{ notes(filter: {object: {account: {field: %s}}}) { body } }",
    "a relation's rows": "{ ledgers { name accounts(filter: {field: %s}) { handle } } }",
}


@pytest_asyncio.fixture
async def schema():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"dz_validated": [__name__]}
    )
    await Tortoise.generate_schemas()
    ledger = await DzLedger.create(id=1, name="main")
    account = await DzAccount.create(
        id=1, handle="ann", ledger_code="ACME-0001-INT", credit=10, ledger=ledger
    )
    await DzNote.create(id=1, body="hi", account=account)

    orm = StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)

    @orm.type(
        DzAccount,
        filters=orm.filter(DzAccount),
        group=orm.group(DzAccount),
        aggregate=orm.aggregate(DzAccount),
    )
    class AccountNode(relay.Node):
        id: relay.NodeID[int]
        handle: auto
        ledger_code: auto
        credit: auto

    @orm.type(DzNote, filters=orm.filter(DzNote))
    class NoteType:
        body: auto
        account: AccountNode

    @orm.type(DzLedger)
    class LedgerType:
        name: auto
        accounts: list[AccountNode]

    @strawberry.type
    class Query:
        accounts: list[AccountNode] = orm.field.eager()
        account_pages: ORMListConnection[AccountNode] = orm.connection.eager()
        notes: list[NoteType] = orm.field.eager()
        ledgers: list[LedgerType] = orm.field.eager()

    yield orm.schema(query=Query)
    await Tortoise.close_connections()


@pytest.mark.parametrize("value", list(REFUSED_VALUES))
@pytest.mark.parametrize("read", list(READS))
async def test_a_refused_value_does_not_reveal_the_validator(schema, read, value):
    result = await schema.execute(READS[read] % REFUSED_VALUES[value])
    assert result.errors, result.data
    for error in result.errors:
        assert error.message == "Invalid filter expression", error.message
        assert not any(leak in error.message for leak in LEAKS)
        assert isinstance(error.original_error.__cause__, ValidationError)


async def test_a_value_every_validator_accepts_is_answered(schema):
    result = await schema.execute(
        READS["through a relation"] % '{ledgerCode: {exact: "ACME-0001-INT"}}'
    )
    assert result.errors is None, result.errors
    assert result.data == {"notes": [{"body": "hi"}]}
