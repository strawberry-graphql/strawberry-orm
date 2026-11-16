"""A statement the database refuses reaches the client as one message.

The database's error names the statement's tables and columns, and Tortoise's
own for a field it cannot resolve lists every column of the model. Whichever
read fails - the page, ``totalCount``, aggregates, groups, a relation's rows,
counts or an eager load - the client is told "Invalid filter expression", or
"Database connection failed" when there is no database to ask, and the
original is kept as the cause, for the server log.

Each table here is broken by moving it from under its model.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
import strawberry
from strawberry import relay
from tortoise import Tortoise, fields
from tortoise.exceptions import DBConnectionError, FieldError, OperationalError
from tortoise.models import Model

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

#: What the database's own message gives away, none of which may reach a client.
LEAKS = ("internal_documents_v2", "internal_folders_v2", "tenant_secret_id", "SELECT")


class Folder(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    tenant_secret_id = fields.IntField(default=42)

    docs: fields.ReverseRelation["Doc"]

    class Meta:
        table = "internal_folders_v2"


class Doc(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=100)
    amount = fields.IntField(default=0)
    tenant_secret_id = fields.IntField(default=42)
    folder = fields.ForeignKeyField("database_errors.Folder", related_name="docs")

    class Meta:
        table = "internal_documents_v2"


def _docs_schema(**options):
    """Docs from the root, and from each folder."""
    orm = StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)

    @orm.type(Doc, filters=orm.filter(Doc), order=orm.order(Doc), group=orm.group(Doc))
    class DocNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        amount: auto

        @classmethod
        def scope_rows(cls, query, info):
            return query.filter(tenant_secret_id=42)

    @orm.type(Folder)
    class FolderType:
        name: auto
        docs: list[DocNode]

    @strawberry.type
    class Query:
        docs: ORMListConnection[DocNode] = orm.connection.eager()
        folders: list[FolderType] = orm.field.eager()

    return orm.schema(query=Query, **options)


def _folders_schema(**options):
    """Each doc's folder."""
    orm = StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)

    @orm.type(Folder)
    class FolderType:
        name: auto

        @classmethod
        def scope_rows(cls, query, info):
            return query.filter(tenant_secret_id=42)

    @orm.type(Doc)
    class DocType:
        title: auto
        folder: FolderType | None

    @strawberry.type
    class Query:
        docs: list[DocType] = orm.field.eager()

    return orm.schema(query=Query, **options)


READS = {
    "edges": "{ docs(first: 5) { edges { node { title } } } }",
    "totalCount": "{ docs(first: 5) { totalCount } }",
    "aggregates": "{ docs(first: 5) { aggregates { count sum { amount } } } }",
    "groups": "{ docs(groupBy: [{field: {title: true}}]) { groups { key { title } } } }",
    "group items": (
        "{ docs(groupBy: [{field: {title: true}}]) "
        "{ groups { items { edges { node { title } } } } } }"
    ),
}

RELATION_READS = {
    "rows": "{ folders { docs { title } } }",
    "filtered rows": '{ folders { docs(filter: {field: {title: {exact: "a"}}}) { title } } }',
    "ordered rows": "{ folders { docs(order: [{field: {title: ASC}}]) { title } } }",
}


@pytest_asyncio.fixture
async def client():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"database_errors": [__name__]}
    )
    await Tortoise.generate_schemas()
    folder = await Folder.create(id=1, name="f")
    await Doc.create(id=1, title="a", amount=1, folder=folder)
    yield Tortoise.get_connection("default")
    await Tortoise.close_connections()


async def _break(client, model):
    table = model._meta.db_table
    await client.execute_script(f'ALTER TABLE "{table}" RENAME TO "{table}_moved"')


def _assert_refused(result, message="Invalid filter expression"):
    assert result.errors, result.data
    for error in result.errors:
        assert error.message == message
        for leak in LEAKS:
            assert leak not in error.message
        assert isinstance(
            error.original_error.__cause__, (OperationalError, DBConnectionError)
        )


@pytest.mark.parametrize("read", list(READS))
async def test_a_refused_read_does_not_reveal_the_statement(client, read):
    await _break(client, Doc)
    _assert_refused(await _docs_schema().execute(READS[read]))


@pytest.mark.parametrize("optimizer", [True, False])
@pytest.mark.parametrize("read", list(RELATION_READS))
async def test_a_refused_relation_read_does_not_reveal_it(client, read, optimizer):
    await _break(client, Doc)
    schema = _docs_schema(optimizer=optimizer)
    _assert_refused(await schema.execute(RELATION_READS[read]))


@pytest.mark.parametrize("optimizer", [True, False])
async def test_a_refused_to_one_read_does_not_reveal_it(client, optimizer):
    await _break(client, Folder)
    schema = _folders_schema(optimizer=optimizer)
    _assert_refused(await schema.execute("{ docs { title folder { name } } }"))


async def test_a_lost_connection_is_reported_plainly(client, monkeypatch):
    async def refuse(*args, **kwargs):
        raise DBConnectionError("could not connect to secret-host:5432")

    monkeypatch.setattr(type(client), "execute_query", refuse)
    result = await _docs_schema().execute(READS["edges"])
    _assert_refused(result, "Database connection failed")


async def test_a_field_the_scope_cannot_resolve_does_not_list_the_columns(client):
    orm = StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)

    @orm.type(Doc)
    class DocType:
        title: auto

        @classmethod
        def scope_rows(cls, query, info):
            return query.filter(tenant=1)

    @strawberry.type
    class Query:
        docs: list[DocType] = orm.field.eager()

    result = await orm.schema(query=Query).execute("{ docs { title } }")
    assert [error.message for error in result.errors] == ["Invalid filter expression"]
    assert isinstance(result.errors[0].original_error.__cause__, FieldError)


async def test_a_relations_counts_are_sanitized(client):
    await _break(client, Doc)
    backend = StrawberryORM.for_tortoise()._backend
    info = SimpleNamespace(context={})
    with pytest.raises(ValueError, match="^Invalid filter expression$") as caught:
        await backend.group_counts(Doc.all(), "folder_id", info)
    assert isinstance(caught.value.__cause__, OperationalError)
