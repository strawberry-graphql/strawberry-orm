"""A statement a write runs reaches the client as one message, not its SQL.

``orm.apply_upsert()`` and ``orm.apply_ref_list()`` look rows up, insert,
update and delete through the repo. A constraint the row breaks, or a value
the column type refuses, made SQLAlchemy report the statement with its bound
parameters: the table, the columns the API leaves out, and their values. The
client is told "The database refused the write", and the original is kept as
the cause, for the server log.
"""

from inspect import isawaitable

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import ForeignKey, Integer, String, TypeDecorator, create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto

#: What the database's own message gives away, none of which may reach a client.
LEAKS = (
    "internal_members_v2",
    "tenant_secret_id",
    "4242",
    "INSERT",
    "SELECT",
    "parameters",
    "victim@corp.example",
    "not a code",
)


class Code(TypeDecorator):
    impl = String(20)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and not value.isalnum():
            raise ValueError(f"not a code: {value!r}")
        return value


class Base(DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "internal_teams_v2"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    members: Mapped[list["Member"]] = relationship(back_populates="team")


class Member(Base):
    __tablename__ = "internal_members_v2"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(100), unique=True)
    code: Mapped[str] = mapped_column(Code)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("internal_teams_v2.id"))
    tenant_secret_id: Mapped[int] = mapped_column(Integer, default=4242)
    team: Mapped[Team | None] = relationship(back_populates="members")


def _schema():
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )

    @orm.type(Member, exclude=["tenant_secret_id"])
    class MemberType:
        id: auto
        email: auto

    upsert = orm.mutations.upsert_node_input(
        model=Member,
        project={
            "_meta": {
                "create": ["email", "code"],
                "update": ["email"],
                "upsert": {"where": ["code"], "onMatch": "UPDATE"},
            }
        },
        name="MemberUpsert",
    )
    MemberRef = orm.ref(
        Member, create=orm.input(Member, include=["email", "code"], name="NewMember")
    )

    async def _settled(value):
        return await value if isawaitable(value) else value

    @strawberry.type
    class Query:
        ok: bool = True

    @strawberry.type
    class Mutation:
        @strawberry.mutation
        async def upsert_member(
            self, info: strawberry.Info, input: upsert
        ) -> MemberType:
            return await _settled(orm.apply_upsert(input, info))

        @strawberry.mutation
        async def set_members(
            self, info: strawberry.Info, team_id: int, members: list[MemberRef]
        ) -> bool:
            team = await _settled(info.context["session"].get(Team, team_id))
            await _settled(orm.apply_ref_list(team, "members", members, info))
            return True

    return orm.schema(query=Query, mutation=Mutation)


WRITES = {
    "an upsert's insert": (
        'mutation { upsertMember(input: {where: {code: "NEW1"}, '
        'create: {email: "victim@corp.example", code: "NEW1"}}) { id } }'
    ),
    "an upsert's lookup": (
        'mutation { upsertMember(input: {where: {code: "not a code!"}, '
        'create: {email: "new@corp.example", code: "not a code!"}}) { id } }'
    ),
    "a ref list's insert": (
        "mutation { setMembers(teamId: 1, members: "
        '[{create: {email: "victim@corp.example", code: "NEW3"}}]) }'
    ),
}


def _seed():
    return [
        Team(id=1, name="a"),
        Member(id=1, email="victim@corp.example", code="A1", team_id=1),
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


@pytest.mark.parametrize("write", list(WRITES))
async def test_a_refused_write_does_not_reveal_the_statement(db, write):
    result = await _schema().execute(WRITES[write], context_value={"session": db})
    assert result.errors, result.data
    for error in result.errors:
        assert error.message == "The database refused the write", error.message
        for leak in LEAKS:
            assert leak not in error.message, error.message
        assert error.original_error.__cause__ is not None
