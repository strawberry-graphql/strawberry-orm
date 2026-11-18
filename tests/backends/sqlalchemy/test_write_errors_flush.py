"""A flush that cannot write the rows reaches the client as one message.

The unit of work refuses some writes itself, naming the tables, columns and
instances: an update another request got to first (``StaleDataError``), or a
link let go of that would blank out a key column (an ``AssertionError``).
The client is told "The database refused the write"; the original is kept as
the cause, for the server log, and an ``assert`` of the app's own is left as
it is.
"""

from inspect import isawaitable

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import ForeignKey, Integer, String, create_engine, select, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import StaticPool

from strawberry_orm import StrawberryORM
from strawberry_orm.backends.sqlalchemy import _database_errors
from strawberry_orm.repo import AbstractRepo
from strawberry_orm.types import auto

REFUSED = "The database refused the write"
LEAKS = ("internal_", "account_ref", "Profile", "Member", "expected", "matched")


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "internal_accounts_v5"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    profile: Mapped["Profile | None"] = relationship(back_populates="account")


class Profile(Base):
    __tablename__ = "internal_profiles_v5"
    account_ref: Mapped[int] = mapped_column(
        ForeignKey("internal_accounts_v5.id"), primary_key=True
    )
    bio: Mapped[str] = mapped_column(String(50), default="")
    account: Mapped[Account] = relationship(back_populates="profile")


class Member(Base):
    __tablename__ = "internal_members_v5"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(50))
    version: Mapped[int] = mapped_column(Integer)
    __mapper_args__ = {"version_id_col": version}


class AssertingRepo(AbstractRepo):
    def on_before_update(self, instance, data, info):
        assert data["email"].endswith("@corp.example"), "emails stay on corp.example"
        return data


def _schema(**kwargs):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False, **kwargs
    )

    @orm.type(Account)
    class AccountType:
        id: auto
        name: auto

    @orm.type(Member)
    class MemberType:
        id: auto
        email: auto

    account = orm.mutations.upsert_node_input(
        model=Account,
        project={
            "_meta": {
                "update": ["name"],
                "upsert": {"where": ["id"], "onMatch": "UPDATE"},
            },
        },
        name="AccountUpsert",
    )
    member = orm.mutations.upsert_node_input(
        model=Member,
        project={
            "_meta": {
                "update": ["email"],
                "upsert": {"where": ["id"], "onMatch": "UPDATE"},
            }
        },
        name="MemberUpsert",
    )

    async def _settled(value):
        return await value if isawaitable(value) else value

    @strawberry.type
    class Query:
        ok: bool = True

    @strawberry.type
    class Mutation:
        @strawberry.mutation
        async def upsert_account(
            self, info: strawberry.Info, input: account
        ) -> AccountType:
            # Keyed by its link, the profile cannot be let go of: its key would
            # be blanked out when the write flushes.
            session = info.context["session"]
            mine = await _settled(session.get(Account, 1))
            await _settled(session.refresh(mine, ["profile"]))
            mine.profile = None
            return await _settled(orm.apply_upsert(input, info))

        @strawberry.mutation
        async def upsert_member(
            self, info: strawberry.Info, input: member
        ) -> MemberType:
            if info.context.get("raced"):
                session = info.context["session"]
                info.context["read"] = await _settled(session.get(Member, 1))
                await _settled(
                    session.execute(
                        text(f"UPDATE {Member.__tablename__} SET version = 2")
                    )
                )
            return await _settled(orm.apply_upsert(input, info))

    return orm.schema(query=Query, mutation=Mutation)


LET_GO = (
    'mutation { upsertAccount(input: {where: {id: "1"}, update: {name: "me"}}) { id } }'
)
RENAME = (
    'mutation { upsertMember(input: {where: {id: "1"}, update: {email: "%s"}}) { id } }'
)


def _seed():
    return [
        Account(id=1, name="me"),
        Account(id=2, name="other"),
        Profile(account_ref=1, bio="mine"),
        Profile(account_ref=2, bio="theirs"),
        Member(id=1, email="old@corp.example", version=1),
    ]


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add_all(_seed())
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
        session.add_all(_seed())
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture(params=["sync", "async"])
def db(request):
    return request.getfixturevalue(
        "session" if request.param == "sync" else "async_session"
    )


async def _profiles(db):
    rows = await _settled(db.execute(select(Profile.account_ref, Profile.bio)))
    return sorted(tuple(row) for row in rows)


async def _settled(value):
    return await value if isawaitable(value) else value


def _assert_refused(result, cause):
    assert result.errors, result.data
    [error] = result.errors
    assert error.message == REFUSED, error.message
    assert isinstance(error.original_error.__cause__, cause)
    for leak in LEAKS:
        assert leak not in error.message


async def test_a_link_that_would_blank_out_a_key_is_refused(db):
    result = await _schema().execute(LET_GO, context_value={"session": db})
    _assert_refused(result, AssertionError)
    await _settled(db.rollback())
    assert await _profiles(db) == [(1, "mine"), (2, "theirs")]


async def test_a_row_changed_since_it_was_read_is_refused(db):
    result = await _schema().execute(
        RENAME % "new@corp.example", context_value={"session": db, "raced": True}
    )
    _assert_refused(result, StaleDataError)


async def test_an_assert_in_a_hook_is_the_apps_own(db):
    schema = _schema(repos={Member: AssertingRepo})
    result = await schema.execute(
        RENAME % "new@else.example", context_value={"session": db}
    )
    [error] = result.errors
    assert error.message.startswith("emails stay on corp.example")
    assert type(error.original_error) is AssertionError


def test_a_pool_timeout_is_the_connection_failing():
    failure = pytest.raises(ConnectionError, match="Database connection failed")
    with failure as raised, _database_errors(REFUSED):
        raise PoolTimeoutError("QueuePool limit of size 5 overflow 10 reached")
    assert isinstance(raised.value.__cause__, PoolTimeoutError)
