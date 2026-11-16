"""Relationship shapes only SQLAlchemy's mapper declares.

A one-to-one whose foreign key is on the other row, written as a link from the
row without it, and a relationship whose ``primaryjoin`` is narrower than its
key columns. Django and Tortoise offer neither as a writable relation.
"""

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import Boolean, ForeignKey, String, and_, create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from strawberry_orm import StrawberryORM, UpsertNotFoundError
from strawberry_orm.mutations import _sqlalchemy_relation_specs
from strawberry_orm.types import auto
from tests.abstract.write_path_authorization import (
    fields_of,
    item,
    link_recorder,
    member,
    resolve,
)


class Base(DeclarativeBase):
    pass


class Member(Base):
    __tablename__ = "shape_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    profile: Mapped["Profile | None"] = relationship(back_populates="member")


class Profile(Base):
    __tablename__ = "shape_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("shape_member.id"), unique=True
    )
    member: Mapped[Member | None] = relationship(back_populates="profile")


class Project(Base):
    __tablename__ = "shape_project"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    tasks: Mapped[list["Task"]] = relationship(back_populates="project")
    open_tasks: Mapped[list["Task"]] = relationship(
        primaryjoin=lambda: and_(Project.id == Task.project_id, Task.done.is_(False)),
        overlaps="tasks,project",
    )


class Task(Base):
    __tablename__ = "shape_task"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("shape_project.id"))
    project: Mapped[Project | None] = relationship(
        back_populates="tasks", overlaps="open_tasks"
    )


def new_orm(**kwargs: Any) -> Any:
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        warn_sensitive=False,
        **kwargs,
    )


@pytest_asyncio.fixture(params=["sync", "async"])
async def session(request):
    if request.param == "sync":
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        yield session
        session.close()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def db(session):
    """Me and Victim each hold a profile, Loner none; Launch has one open task."""
    me = Member(id=1, name="Me")
    session.add_all(
        [
            me,
            Member(id=2, name="Victim"),
            Member(id=3, name="Loner"),
            Profile(id=1, name="my profile", member_id=1),
            Profile(id=2, name="their profile", member_id=2),
            Profile(id=3, name="spare profile"),
            Project(id=1, name="Launch"),
            Task(id=1, title="open", project_id=1),
            Task(id=2, title="done", done=True, project_id=1),
            Task(id=3, title="also open", project_id=1),
        ]
    )
    await resolve(session.commit())
    info = SimpleNamespace(context={"session": session})
    return SimpleNamespace(session=session, info=info, me=me)


async def held_by(db: Any) -> dict[str, int | None]:
    statement = select(Profile.name, Profile.member_id).order_by(Profile.id)
    return dict((await resolve(db.session.execute(statement))).all())


async def tasks(db: Any) -> list[tuple[int, int | None]]:
    statement = select(Task.id, Task.project_id).order_by(Task.id)
    return [tuple(row) for row in (await resolve(db.session.execute(statement))).all()]


# -- A reverse one-to-one takes its target from the row that held it -----------------

MEMBER = {
    "_meta": {
        "create": ["name"],
        "update": ["name"],
        "upsert": {"where": ["name"], "onMatch": "UPDATE"},
    },
    "profile": {
        "_meta": {"update": ["name"], "upsert": {"where": ["id"], "onMatch": "IGNORE"}}
    },
}

UNTOUCHED = {"my profile": 1, "their profile": 2, "spare profile": None}


class TestReverseOneToOneLinks:
    @pytest.fixture
    def calls(self):
        return []

    @staticmethod
    def orm(calls: list[Any], locked: set[str]) -> Any:
        return new_orm(repos={Profile: link_recorder(calls, locked=locked)})

    @staticmethod
    def take(orm: Any, who: str, profile: str, *, create: bool = False) -> Any:
        upsert = orm.mutations.upsert_node_input(
            model=Member, project=MEMBER, name="MemberUpsertInput"
        )
        operation = "create" if create else "update"
        link_type = member(upsert, operation, "profile")
        link = link_type(update=member(link_type, "update")(id=profile))
        return upsert(
            where=member(upsert, "where")(name=who),
            **{operation: member(upsert, operation)(name=who, profile=link)},
        )

    async def test_the_link_is_labelled_as_the_reverse_side(self):
        spec = _sqlalchemy_relation_specs(Member)["profile"]
        assert (spec.kind, spec.relation_mode, spec.fk_column, spec.remote_attr) == (
            "single",
            "reverse_fk",
            None,
            "member",
        )
        upsert = new_orm().mutations.upsert_node_input(
            model=Member, project=MEMBER, name="MemberUpsertInput"
        )
        assert fields_of(member(upsert, "update")) == {"name", "profile"}

    async def test_taking_another_members_profile_asks_its_owner(self, db, calls):
        orm = self.orm(calls, locked={"Victim"})
        with pytest.raises(PermissionError, match="can_unlink denied"):
            await resolve(orm.apply_upsert(self.take(orm, "Me", "2"), db.info))
        assert calls == [
            ("can_link", "Me", "profile", "their profile"),
            ("can_unlink", "Victim", "profile", "their profile"),
        ]
        assert await held_by(db) == UNTOUCHED

    async def test_a_new_member_taking_a_profile_asks_its_owner(self, db, calls):
        orm = self.orm(calls, locked={"Victim"})
        payload = self.take(orm, "New", "2", create=True)
        with pytest.raises(PermissionError, match="can_unlink denied"):
            await resolve(orm.apply_upsert(payload, db.info))
        assert calls == [
            ("can_link", "New", "profile", "their profile"),
            ("can_unlink", "Victim", "profile", "their profile"),
        ]
        assert await held_by(db) == UNTOUCHED
        names = await resolve(db.session.execute(select(Member.name)))
        assert sorted(names.scalars()) == ["Loner", "Me", "Victim"]

    async def test_an_allowed_take_asks_every_row_it_moves(self, db, calls):
        orm = self.orm(calls, locked=set())
        await resolve(orm.apply_upsert(self.take(orm, "Me", "2"), db.info))
        assert calls == [
            ("can_link", "Me", "profile", "their profile"),
            ("can_unlink", "Victim", "profile", "their profile"),
            ("can_unlink", "Me", "profile", "my profile"),
        ]
        assert await held_by(db) == {
            "my profile": None,
            "their profile": 1,
            "spare profile": None,
        }

    async def test_a_profile_nobody_holds_is_only_linked(self, db, calls):
        orm = self.orm(calls, locked=set())
        await resolve(orm.apply_upsert(self.take(orm, "Loner", "3"), db.info))
        assert calls == [("can_link", "Loner", "profile", "spare profile")]
        assert await held_by(db) == {**UNTOUCHED, "spare profile": 3}

    async def test_a_ref_list_cannot_write_it(self, db, calls):
        orm = self.orm(calls, locked=set())
        ref = orm.ref(Profile)
        with pytest.raises(TypeError, match=r"Member\.profile links a single row"):
            await resolve(
                orm.apply_ref_list(
                    db.me, "profile", [item(ref, "update", id="2")], db.info
                )
            )
        assert calls == []
        assert await held_by(db) == UNTOUCHED


# -- A primaryjoin narrower than its keys bounds unlink and delete --------------------


class TestPrimaryjoinMembership:
    @pytest.fixture
    def orm(self):
        return new_orm(repos={Task: link_recorder([], locked=set())})

    async def project(self, db: Any) -> Any:
        return await resolve(db.session.get(Project, 1))

    async def test_a_row_the_primaryjoin_leaves_out_is_not_a_member(self, db, orm):
        ref = orm.ref(Task, unlink=True, delete=True)
        project = await self.project(db)
        for operation in ("delete", "unlink"):
            with pytest.raises(
                UpsertNotFoundError, match=r"No Task 2 in Project\.open_tasks"
            ):
                await resolve(
                    orm.apply_ref_list(
                        project, "open_tasks", [item(ref, operation, id="2")], db.info
                    )
                )
        assert await tasks(db) == [(1, 1), (2, 1), (3, 1)]

    async def test_rows_in_the_relation_can_still_be_unlinked_and_deleted(
        self, db, orm
    ):
        ref = orm.ref(Task, unlink=True, delete=True)
        refs = [item(ref, "unlink", id="1"), item(ref, "delete", id="3")]
        await resolve(
            orm.apply_ref_list(await self.project(db), "open_tasks", refs, db.info)
        )
        assert await tasks(db) == [(1, None), (2, 1)]

    async def test_the_unnarrowed_relation_still_reaches_every_row(self, db, orm):
        ref = orm.ref(Task, delete=True)
        refs = [item(ref, "delete", id="2")]
        await resolve(
            orm.apply_ref_list(await self.project(db), "tasks", refs, db.info)
        )
        assert await tasks(db) == [(1, 1), (3, 1)]


# -- isNull on a reverse one-to-one -----------------------------------------------------

read_orm = new_orm()
ProfileFilter = read_orm.filter(Profile)
MemberFilter = read_orm.filter(Member)


@read_orm.type(Profile, filters=ProfileFilter)
class ProfileType:
    id: auto
    name: auto


@read_orm.type(Member, filters=MemberFilter)
class MemberType:
    id: auto
    name: auto
    profile: ProfileType | None


@strawberry.type
class Query:
    members: list[MemberType] = read_orm.field.auto()


class TestReverseOneToOnePresence:
    async def test_is_null_asks_whether_the_related_row_exists(self, db):
        schema = read_orm.schema(query=Query)
        for value, expected in (("true", ["Loner"]), ("false", ["Me", "Victim"])):
            result = await schema.execute(
                f"{{ members(filter: {{object: {{profile: {{isNull: {value}}}}}}}) "
                f"{{ name }} }}",
                context_value=db.info.context,
            )
            assert result.errors is None, result.errors
            assert sorted(row["name"] for row in result.data["members"]) == expected
