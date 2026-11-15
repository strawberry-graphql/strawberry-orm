"""A statement the database or driver refuses reaches the client as one message.

SQLAlchemy's error names the statement's tables and columns, the type's
``scope_rows`` criteria and the bound parameters. Whichever read fails - the
page, ``totalCount``, aggregates, groups, a relation's counts or an eager load
- the client is told "Invalid filter expression", and the original is kept as
the cause, for the server log.

``Doc`` has a UUID key, which a client can send any string for, and a ``code``
column whose type refuses some values as it binds them, as custom types do.
"""

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import Integer, String, TypeDecorator, Uuid, create_engine, select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

#: What the database's own message gives away, none of which may reach a client.
LEAKS = (
    "internal_documents_v2",
    "tenant_secret_id",
    "SELECT",
    "WHERE",
    "parameters",
    "not-a-uuid",
    "not a code",
    "hex",
)

BY_ID = '{field: {id: {exact: "not-a-uuid"}}}'
BY_CODE = '{field: {code: {exact: "not a code!"}}}'

READS = {
    "edges": "{ docs(first: 5, filter: %s) { edges { node { title } } } }",
    "totalCount": "{ docs(first: 5, filter: %s) { totalCount } }",
    "aggregates": (
        "{ docs(first: 5, filter: %s) { aggregates { count sum { amount } } } }"
    ),
    "groups": (
        "{ docs(first: 5, filter: %s, groupBy: [{field: {title: true}}]) "
        "{ groups { key { title } } } }"
    ),
}


class Code(TypeDecorator):
    impl = String(20)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and not value.isalnum():
            raise ValueError(f"not a code: {value!r}")
        return value


class Base(DeclarativeBase):
    pass


class Doc(Base):
    __tablename__ = "internal_documents_v2"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    code: Mapped[str] = mapped_column(Code)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    tenant_secret_id: Mapped[int] = mapped_column(Integer)


def _doc() -> Doc:
    return Doc(id=uuid.UUID(int=1), title="a", code="A1", amount=1, tenant_secret_id=42)


def _schema(**options):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False, **options
    )

    @orm.type(Doc, filters=orm.filter(Doc), order=orm.order(Doc), group=orm.group(Doc))
    class DocNode(relay.Node):
        id: relay.NodeID[str]
        title: auto
        code: auto
        amount: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.where(Doc.tenant_secret_id == 42)

    @strawberry.type
    class Query:
        docs: ORMListConnection[DocNode] = orm.connection.eager()

    return orm.schema(query=Query)


def _assert_hidden(message: str) -> None:
    for leak in LEAKS:
        assert leak not in message, message


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(_doc())
        session.commit()
        yield session
    engine.dispose()


@pytest_asyncio.fixture
async def async_session():
    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(_doc())
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture(params=["sync", "async", "offloaded"])
def run(request, session):
    """Run a query on a ``Session``, an ``AsyncSession``, or a ``Session``
    whose statements are sent from a worker thread."""
    if request.param == "sync":
        schema = _schema()

        async def _run(query):
            return schema.execute_sync(query, context_value={"session": session})

    elif request.param == "async":
        schema = _schema()
        async_session = request.getfixturevalue("async_session")

        async def _run(query):
            return await schema.execute(query, context_value={"session": async_session})

    else:
        schema = _schema(offload_sync_session=True)

        async def _run(query):
            return await schema.execute(query, context_value={"session": session})

    return _run


@pytest.mark.parametrize("read", list(READS))
async def test_a_value_the_column_type_refuses_is_reported_plainly(run, read):
    result = await run(READS[read] % BY_CODE)
    assert result.errors, result.data
    for error in result.errors:
        assert error.message == "Invalid filter expression", error.message
        _assert_hidden(error.message)


@pytest.mark.parametrize("read", list(READS))
async def test_a_malformed_key_does_not_reveal_the_statement(run, read):
    result = await run(READS[read] % BY_ID)
    assert result.errors, result.data
    for error in result.errors:
        assert error.message == "'not-a-uuid' is not a valid ID.", error.message


def _refused() -> Exception:
    return StatementError(
        "(builtins.ValueError) bad row",
        "SELECT tenant_secret_id FROM internal_documents_v2 WHERE id = ?",
        {"id": "not-a-uuid"},
        ValueError("bad row"),
    )


class _RefusedRows:
    """A result whose rows fail as they are read, as a driver's can mid-fetch."""

    def scalars(self):
        raise _refused()


def test_an_eager_load_that_fails_as_its_rows_are_read_is_sanitized():
    backend = SQLAlchemyBackend(dialect="sqlite")
    session = SimpleNamespace(execute=lambda stmt: _RefusedRows())
    with pytest.raises(ValueError, match="^Invalid filter expression$") as caught:
        backend._execute_stmt_sync(session, select(Doc))
    assert isinstance(caught.value.__cause__, StatementError)


async def test_an_async_load_that_fails_as_its_rows_are_read_is_sanitized():
    backend = SQLAlchemyBackend(dialect="sqlite")

    async def execute(stmt):
        return _RefusedRows()

    with pytest.raises(ValueError, match="^Invalid filter expression$") as caught:
        await backend._execute_stmt_async(SimpleNamespace(execute=execute), select(Doc))
    assert isinstance(caught.value.__cause__, StatementError)


def test_a_relations_counts_are_sanitized(session):
    backend = SQLAlchemyBackend(dialect="sqlite")
    info = SimpleNamespace(context={"session": session})
    refused = select(Doc).where(Doc.code == "not a code!")
    with pytest.raises(ValueError, match="^Invalid filter expression$") as caught:
        backend.group_counts(refused, "id", info)
    assert isinstance(caught.value.__cause__, StatementError)


async def test_a_relations_counts_on_an_async_session_are_sanitized(async_session):
    backend = SQLAlchemyBackend(dialect="sqlite")
    info = SimpleNamespace(context={"session": async_session})
    refused = select(Doc).where(Doc.code == "not a code!")
    with pytest.raises(ValueError, match="^Invalid filter expression$") as caught:
        await backend.group_counts(refused, "id", info)
    assert isinstance(caught.value.__cause__, StatementError)
