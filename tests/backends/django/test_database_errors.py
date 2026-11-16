"""A read the database refuses reaches the client as one message.

Django's error names the statement's tables and columns, and on PostgreSQL
the value it refused. Whichever read fails - a list, a page, ``totalCount``,
aggregates, groups, a relation's rows or an eager load - the client is told
"Invalid filter expression", and the original is kept as the cause, for the
server log. A regex the database cannot compile is refused as the query runs.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import strawberry
from asgiref.sync import sync_to_async
from django.db import (
    DatabaseError,
    InterfaceError,
    OperationalError,
    connection,
    models,
)
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.backends.django import DjangoBackend, _read_errors
from strawberry_orm.grouping import GroupPath
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class Ledger(models.Model):
    memo = models.CharField(max_length=50)
    amount = models.IntegerField(default=0)

    class Meta:
        app_label = "testapp"
        db_table = "internal_ledger_v2"


class LedgerLine(models.Model):
    memo = models.CharField(max_length=50)
    ledger = models.ForeignKey(Ledger, on_delete=models.CASCADE, related_name="lines")

    class Meta:
        app_label = "testapp"
        db_table = "internal_ledger_line_v2"


class Stamp(models.Model):
    on = models.DateField()
    key = models.UUIDField(null=True)
    took = models.DurationField()

    class Meta:
        app_label = "testapp"
        db_table = "internal_stamp_v1"


REFUSED = "Invalid filter expression"
BAD = '{field: {memo: {regex: "["}}}'

READS = {
    "list": "{ ledgers(filter: %s) { memo } }",
    "edges": "{ ledgerPages(first: 5, filter: %s) { edges { node { memo } } } }",
    "totalCount": "{ ledgerPages(first: 5, filter: %s) { totalCount } }",
    "aggregates": (
        "{ ledgerPages(first: 5, filter: %s) { aggregates { count sum { amount } } } }"
    ),
    "groups": (
        "{ ledgerPages(first: 5, filter: %s, groupBy: [{field: {memo: true}}]) "
        "{ groups { key { memo } } } }"
    ),
    "a relation's rows": "{ ledgers { memo lines(filter: %s) { memo } } }",
}

#: Reads whose rows come from a scope the database refuses.
SCOPED_READS = {
    "an eager load": "{ ledgers { memo lines { memo } } }",
    "a load onto loaded rows": "{ loaded { memo lines { memo } } }",
}


@pytest.fixture
def ledgers(setup_tables):
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (Ledger, LedgerLine):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    alpha = Ledger.objects.create(id=1, memo="alpha", amount=5)
    LedgerLine.objects.create(id=1, memo="one", ledger=alpha)
    yield
    LedgerLine.objects.all().delete()
    Ledger.objects.all().delete()


def refuse(queryset):
    return queryset.filter(memo__regex="[")


def build(*, refuse_lines=False, optimized=True):
    orm = StrawberryORM.for_django(warn_missing_scope=False, enable_regex_filters=True)

    @orm.type(LedgerLine, filters=orm.filter(LedgerLine))
    class LineType:
        memo: auto

        if refuse_lines:
            scope_rows = classmethod(lambda cls, qs, info: refuse(qs))

    @orm.type(
        Ledger,
        filters=orm.filter(Ledger),
        order=orm.order(Ledger),
        group=orm.group(Ledger),
    )
    class LedgerNode(relay.Node):
        id: relay.NodeID[int]
        memo: auto
        amount: auto
        lines: list[LineType]

    @strawberry.type
    class Query:
        ledgers: list[LedgerNode] = orm.field.eager()
        ledger_pages: ORMListConnection[LedgerNode] = orm.connection.eager()

        @strawberry.field
        def loaded(self) -> list[LedgerNode]:
            return list(Ledger.objects.order_by("id"))

        @strawberry.field
        async def loaded_async(self) -> list[LedgerNode]:
            return await sync_to_async(list)(Ledger.objects.order_by("id"))

    if optimized:
        return orm.schema(query=Query)
    return strawberry.Schema(query=Query)


def build_plain():
    """Types without filters, read by resolvers with no optimizer to load for them."""
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(Ledger)
    class LedgerType:
        memo: auto

        scope_rows = classmethod(lambda cls, qs, info: refuse(qs))

    @orm.type(LedgerLine)
    class LineType:
        memo: auto
        ledger: LedgerType

        scope_rows = classmethod(lambda cls, qs, info: refuse(qs))

    @orm.type(Ledger)
    class LedgerWithLines:
        memo: auto
        lines: list[LineType]

    @strawberry.type
    class Query:
        @strawberry.field
        def lines(self) -> list[LineType]:
            return list(LedgerLine.objects.order_by("id"))

        @strawberry.field
        def ledgers(self) -> list[LedgerWithLines]:
            return list(Ledger.objects.order_by("id"))

    return strawberry.Schema(query=Query)


def assert_refused(result):
    assert result.errors, result.data
    for error in result.errors:
        assert error.message == REFUSED, error.message
        assert isinstance(error.original_error.__cause__, DatabaseError)


@pytest.mark.parametrize("read", list(READS))
def test_a_read_the_database_refuses_is_reported_plainly(ledgers, read):
    assert_refused(build().execute_sync(READS[read] % BAD))


@pytest.mark.parametrize("read", list(READS))
async def test_an_async_read_the_database_refuses_is_reported_plainly(ledgers, read):
    assert_refused(await build().execute(READS[read] % BAD))


@pytest.mark.parametrize("read", list(SCOPED_READS))
def test_a_load_the_database_refuses_is_reported_plainly(ledgers, read):
    assert_refused(build(refuse_lines=True).execute_sync(SCOPED_READS[read]))


@pytest.mark.parametrize("read", list(SCOPED_READS))
async def test_an_async_load_the_database_refuses_is_reported_plainly(ledgers, read):
    query = SCOPED_READS[read].replace("loaded", "loadedAsync")
    assert_refused(await build(refuse_lines=True).execute(query))


@pytest.mark.parametrize(
    "query",
    [
        "{ loaded { lines { memo } } }",
        f"{{ loaded {{ lines(filter: {BAD}) {{ memo }} }} }}",
    ],
)
def test_a_relation_read_without_the_optimizer_is_reported_plainly(ledgers, query):
    assert_refused(build(refuse_lines=True, optimized=False).execute_sync(query))


@pytest.mark.parametrize(
    "query", ["{ ledgers { lines { memo } } }", "{ lines { ledger { memo } } }"]
)
def test_a_plain_relation_resolver_is_reported_plainly(ledgers, query):
    assert_refused(build_plain().execute_sync(query))


def test_the_grouping_reads_are_reported_plainly(ledgers):
    backend = DjangoBackend()
    refused = refuse(Ledger.objects.all())
    by_memo = [GroupPath(alias="_g0", name="memo", column="memo")]
    reads = [
        lambda: backend.batch_group_items(
            refused, by_memo, info=None, model=Ledger, per_group_limit=1
        ),
        lambda: backend.group_memberships(refused, by_memo, [1], info=None),
        lambda: backend.group_counts(refused, "memo", info=None),
        lambda: backend.materialize_query(refused, info=None),
    ]
    for read in reads:
        with pytest.raises(ValueError, match=f"^{REFUSED}$") as caught:
            read()
        assert isinstance(caught.value.__cause__, OperationalError)


def test_rechecking_loaded_rows_is_reported_plainly(ledgers):
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(Ledger)
    class LedgerType:
        memo: auto

        scope_rows = classmethod(lambda cls, qs, info: refuse(qs))

    with pytest.raises(ValueError, match=f"^{REFUSED}$") as caught:
        orm.backend.filter_scoped_pks(Ledger, [1], SimpleNamespace(context={}))
    assert isinstance(caught.value.__cause__, OperationalError)


@pytest.fixture
def stamps(setup_tables):
    if Stamp._meta.db_table not in connection.introspection.table_names():
        with connection.schema_editor() as editor:
            editor.create_model(Stamp)
    yield
    Stamp.objects.all().delete()


def build_stamps():
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(Stamp, filters=orm.filter(Stamp))
    class StampNode(relay.Node):
        id: relay.NodeID[int]
        on: auto
        key: auto
        took: auto

    @strawberry.type
    class Query:
        stamps: list[StampNode] = orm.field.eager()
        stamp_pages: ORMListConnection[StampNode] = orm.connection.eager()

    return orm.schema(query=Query)


UNHELD = [
    # Refused as the filter is added, in Django's words for the column.
    '{on: {exact: "garbage"}}',
    '{key: {exact: "not-a-uuid"}}',
    # Refused as the statement compiles, by a Python error converting it.
    '{took: {exact: "zz"}}',
]
STAMP_READS = [
    "{ stamps(filter: {field: %s}) { id } }",
    "{ stampPages(first: 2, filter: {field: %s}) { totalCount } }",
]


def assert_unheld(result):
    assert [error.message for error in result.errors] == [REFUSED]
    assert result.errors[0].original_error.__cause__ is not None


@pytest.mark.parametrize("value", UNHELD)
@pytest.mark.parametrize("read", STAMP_READS)
def test_a_value_the_column_cannot_hold_is_reported_plainly(stamps, value, read):
    assert_unheld(build_stamps().execute_sync(read % value))


@pytest.mark.parametrize("value", UNHELD)
@pytest.mark.parametrize("read", STAMP_READS)
async def test_an_async_value_the_column_cannot_hold_is_reported_plainly(
    stamps, value, read
):
    assert_unheld(await build_stamps().execute(read % value))


def raising(error, *, raw):
    """Raise *error* from a read while the connection holds *raw*."""
    with (
        patch("django.db.connections", {"default": SimpleNamespace(connection=raw)}),
        _read_errors("default"),
    ):
        raise error


@pytest.mark.parametrize(
    ("error", "raw"),
    [
        (OperationalError("could not connect to server at db.internal:5432"), None),
        (
            OperationalError("server closed the connection unexpectedly"),
            SimpleNamespace(closed=1),
        ),
        (InterfaceError("connection already closed"), SimpleNamespace(closed=0)),
    ],
)
def test_a_connection_that_failed_is_reported_as_such(error, raw):
    with pytest.raises(ConnectionError, match="^Database connection failed$") as caught:
        raising(error, raw=raw)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("canceling statement due to statement timeout"),
        DatabaseError('invalid input syntax for type inet: "x" at internal_ledger_v2'),
    ],
)
def test_a_statement_refused_on_an_open_connection_is_the_request(error):
    with pytest.raises(ValueError, match=f"^{REFUSED}$") as caught:
        raising(error, raw=SimpleNamespace(closed=0))
    assert caught.value.__cause__ is error
