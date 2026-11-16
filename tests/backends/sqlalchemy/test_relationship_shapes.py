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
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    String,
    Table,
    and_,
    create_engine,
    select,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
from strawberry import relay

from strawberry_orm import (
    AbstractRepo,
    StrawberryORM,
    UpsertNotFoundError,
    UpsertValidationError,
)
from strawberry_orm.mutations import _sqlalchemy_relation_specs
from strawberry_orm.relay import ORMListConnection
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


class Keeper(Base):
    __tablename__ = "shape_keeper"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    active: Mapped[bool] = mapped_column(Boolean)


class Pet(Base):
    __tablename__ = "shape_pet"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    keeper_id: Mapped[int | None] = mapped_column(ForeignKey("shape_keeper.id"))
    active_keeper: Mapped[Keeper | None] = relationship(
        primaryjoin=lambda: and_(Pet.keeper_id == Keeper.id, Keeper.active.is_(True)),
        viewonly=True,
    )


class Album(Base):
    __tablename__ = "shape_album"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    photos: Mapped[list["Photo"]] = relationship(
        back_populates="album", cascade="all, delete-orphan"
    )


class Photo(Base):
    __tablename__ = "shape_photo"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    album_id: Mapped[int | None] = mapped_column(ForeignKey("shape_album.id"))
    album: Mapped[Album | None] = relationship(back_populates="photos")


class Desk(Base):
    __tablename__ = "shape_desk"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    chair: Mapped["Chair | None"] = relationship(
        back_populates="desk", cascade="all, delete-orphan"
    )


class Chair(Base):
    __tablename__ = "shape_chair"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    # Not unique: the database would refuse the take before the cascade's delete.
    desk_id: Mapped[int | None] = mapped_column(ForeignKey("shape_desk.id"))
    desk: Mapped[Desk | None] = relationship(back_populates="chair")


class Badge(Base):
    __tablename__ = "shape_badge"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class Wearer(Base):
    __tablename__ = "shape_wearer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    badge_id: Mapped[int | None] = mapped_column(ForeignKey("shape_badge.id"))
    badge: Mapped[Badge | None] = relationship(
        cascade="all, delete-orphan", single_parent=True
    )


shelf_book = Table(
    "shape_shelf_book",
    Base.metadata,
    Column("shelf_id", ForeignKey("shape_shelf.id"), primary_key=True),
    Column("book_id", ForeignKey("shape_book.id"), primary_key=True),
)


class Shelf(Base):
    __tablename__ = "shape_shelf"

    id: Mapped[int] = mapped_column(primary_key=True)
    books: Mapped[list["Book"]] = relationship(
        secondary=shelf_book, cascade="all, delete-orphan", single_parent=True
    )


class Book(Base):
    __tablename__ = "shape_book"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    withdrawn: Mapped[bool] = mapped_column(Boolean, default=False)


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
            Album(id=1, name="Holiday"),
            Photo(id=1, title="beach", album_id=1),
            Photo(id=2, title="hills", album_id=1),
            Badge(id=1, name="old"),
            Badge(id=2, name="new"),
            Wearer(id=1, name="Me", badge_id=1),
            Desk(id=1, name="my desk"),
            Desk(id=2, name="their desk"),
            Chair(id=1, name="my chair", desk_id=1),
            Chair(id=2, name="their chair", desk_id=2),
            Chair(id=3, name="spare chair"),
            Shelf(id=1, books=[Book(id=1, title="atlas"), Book(id=2, title="diary")]),
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


# -- A forward link to a one-to-one takes its target from the row that held it -------

PROFILE = {
    "_meta": {
        "create": ["name"],
        "update": ["name"],
        "upsert": {"where": ["name"], "onMatch": "UPDATE"},
    },
    "member": {"_meta": {"upsert": {"where": ["id"], "onMatch": "IGNORE"}}},
}

CHAIR = {
    "_meta": {
        "create": ["name"],
        "update": ["name"],
        "upsert": {"where": ["name"], "onMatch": "UPDATE"},
    },
    "desk": {"_meta": {"upsert": {"where": ["id"], "onMatch": "IGNORE"}}},
}


def point(
    orm: Any,
    model: type,
    project: dict,
    name: str,
    link: str,
    target: str,
    *,
    create: bool = False,
) -> Any:
    upsert = orm.mutations.upsert_node_input(
        model=model, project=project, name=f"{model.__name__}UpsertInput"
    )
    operation = "create" if create else "update"
    link_type = member(upsert, operation, link)
    upsert_type = member(link_type, "upsert")
    value = link_type(upsert=upsert_type(where=member(upsert_type, "where")(id=target)))
    return upsert(
        where=member(upsert, "where")(name=name),
        **{operation: member(upsert, operation)(name=name, **{link: value})},
    )


def holder_repo(
    calls: list[Any],
    *,
    locked: set[str] = frozenset(),
    hidden: tuple[str, ...] = (),
    column: Any = Profile.name,
) -> type:
    class HolderRepo(link_recorder(calls, locked=set(locked))):
        def scope_query(self, query, info):
            return query.where(column.not_in(hidden))

        def can_delete(self, instance, info):
            calls.append(("can_delete", instance.name))
            return instance.name not in locked

        def on_before_delete(self, instance, info):
            calls.append(("on_before_delete", instance.name))

    return HolderRepo


async def chairs(db: Any) -> dict[str, int | None]:
    statement = select(Chair.name, Chair.desk_id).order_by(Chair.id)
    return dict((await resolve(db.session.execute(statement))).all())


SEATED = {"my chair": 1, "their chair": 2, "spare chair": None}


class TestForwardOneToOneLinks:
    @pytest.fixture
    def calls(self):
        return []

    @staticmethod
    def orm(calls: list[Any], **holder: Any) -> Any:
        return new_orm(
            repos={
                Profile: holder_repo(calls, **holder),
                Member: link_recorder(calls, locked=set()),
            }
        )

    async def test_the_specs_name_the_relation_a_target_is_taken_from(self):
        profile = _sqlalchemy_relation_specs(Profile)["member"]
        chair = _sqlalchemy_relation_specs(Chair)["desk"]
        badge = _sqlalchemy_relation_specs(Wearer)["badge"]
        assert (profile.taken_from, profile.orphans_holder) == ("profile", False)
        assert (chair.taken_from, chair.orphans_holder) == ("chair", True)
        assert (badge.taken_from, badge.orphans_holder) == (None, False)

    @pytest.mark.parametrize("create", [False, True])
    async def test_taking_a_member_asks_the_profile_that_holds_it(
        self, db, calls, create
    ):
        orm = self.orm(calls, locked={"their profile"})
        who = "new profile" if create else "spare profile"
        payload = point(orm, Profile, PROFILE, who, "member", "2", create=create)
        with pytest.raises(PermissionError, match="can_unlink denied"):
            await resolve(orm.apply_upsert(payload, db.info))
        asked = ("can_unlink", "Victim", "profile", "their profile")
        assert calls[-1] == asked
        assert await held_by(db) == UNTOUCHED

    @pytest.mark.parametrize("create", [False, True])
    async def test_a_holder_the_repo_hides_is_not_written(self, db, calls, create):
        orm = self.orm(calls, hidden=("their profile",))
        who = "new profile" if create else "spare profile"
        payload = point(orm, Profile, PROFILE, who, "member", "2", create=create)
        with pytest.raises(
            PermissionError, match="Member 2 cannot be taken from the Profile"
        ):
            await resolve(orm.apply_upsert(payload, db.info))
        assert ("can_unlink", "Victim", "profile", "their profile") not in calls
        assert await held_by(db) == UNTOUCHED

    async def test_an_allowed_take_clears_the_holders_key(self, db, calls):
        orm = self.orm(calls)
        payload = point(orm, Profile, PROFILE, "spare profile", "member", "2")
        await resolve(orm.apply_upsert(payload, db.info))
        await resolve(db.session.commit())
        assert calls == [
            ("can_link", "spare profile", "member", "Victim"),
            ("can_unlink", "Victim", "profile", "their profile"),
        ]
        assert await held_by(db) == {
            "my profile": 1,
            "their profile": None,
            "spare profile": 2,
        }

    async def test_a_new_row_taking_a_member_clears_the_holders_key(self, db, calls):
        """The holder lets go before the new row is built; the link is asked
        once the row exists."""
        orm = self.orm(calls)
        payload = point(
            orm, Profile, PROFILE, "new profile", "member", "2", create=True
        )
        await resolve(orm.apply_upsert(payload, db.info))
        await resolve(db.session.commit())
        assert calls == [
            ("can_unlink", "Victim", "profile", "their profile"),
            ("can_link", "new profile", "member", "Victim"),
        ]
        assert await held_by(db) == {
            "my profile": 1,
            "their profile": None,
            "spare profile": None,
            "new profile": 2,
        }

    @pytest.mark.parametrize("create", [False, True])
    async def test_a_holder_the_cascade_would_delete_asks_to_be_deleted(
        self, db, calls, create
    ):
        orm = new_orm(
            repos={Chair: holder_repo(calls, locked={"their chair"}, column=Chair.name)}
        )
        who = "new chair" if create else "spare chair"
        payload = point(orm, Chair, CHAIR, who, "desk", "2", create=create)
        with pytest.raises(PermissionError, match="denied"):
            await resolve(orm.apply_upsert(payload, db.info))
        assert await chairs(db) == SEATED

    async def test_an_allowed_take_deletes_the_orphaned_holder_once(self, db, calls):
        orm = new_orm(repos={Chair: holder_repo(calls, column=Chair.name)})
        payload = point(orm, Chair, CHAIR, "spare chair", "desk", "2")
        await resolve(orm.apply_upsert(payload, db.info))
        await resolve(db.session.commit())
        assert calls == [
            ("can_unlink", "their desk", "chair", "their chair"),
            ("can_delete", "their chair"),
            ("on_before_delete", "their chair"),
        ]
        assert await chairs(db) == {"my chair": 1, "spare chair": 2}


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


# -- A join narrower than its keys is not rebuilt from them ---------------------------


class TestANarrowedJoinIsNotRebuilt:
    """Grouping and ``objects:`` join on the keys alone, so ``done`` tasks would count."""

    def test_grouping_leaves_the_relation_out(self):
        orm = new_orm()
        orm.group(Task)
        group = orm.group(Project)
        assert set(group._object_type.__annotations__) == {"tasks"}
        assert set(group._objects_type.__annotations__) == {"tasks"}

    def test_ordering_by_the_related_rows_together_leaves_it_out(self):
        orm = new_orm()
        orm.order(Task)
        order = orm.order(Project)
        assert set(order._object_type.__annotations__) == {"tasks", "open_tasks"}
        assert set(order._objects_type.__annotations__) == {"tasks"}

    def test_a_connection_over_it_is_refused(self):
        orm = new_orm()

        @orm.type(Task)
        class TaskNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        with pytest.raises(ValueError, match="join says more than that column"):

            @orm.type(Project)
            class ProjectNode(relay.Node):
                id: relay.NodeID[int]
                open_tasks: ORMListConnection[TaskNode] = orm.connection.eager()


class TestANarrowedToOneIsFilteredThroughItsJoin:
    """``Pet.active_keeper`` lists only an active keeper, so the key column
    alone cannot say whom it lists: tom's keeper 7 is inactive."""

    @pytest.fixture
    def pets(self):
        orm = new_orm()

        @orm.type(Keeper, filters=orm.filter(Keeper))
        class KeeperType:
            id: auto
            name: auto

        @orm.type(Pet, filters=orm.filter(Pet))
        class PetType:
            name: auto
            active_keeper: KeeperType | None

        @strawberry.type
        class Query:
            pets: list[PetType] = orm.field.eager()

        schema = orm.schema(query=Query)
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        session.add_all(
            [
                Keeper(id=1, name="ann", active=True),
                Keeper(id=7, name="former", active=False),
                Pet(id=1, name="rex", keeper_id=1),
                Pet(id=2, name="tom", keeper_id=7),
                Pet(id=3, name="stray"),
            ]
        )
        session.commit()

        query = "{ pets(filter: {object: {activeKeeper: %s}}) { name } }"

        def pets(keeper: str) -> list[str]:
            result = schema.execute_sync(
                query % keeper, context_value={"session": session}
            )
            assert result.errors is None, result.errors
            return sorted(pet["name"] for pet in result.data["pets"])

        yield pets
        session.close()

    def test_its_key_matches_only_the_row_it_lists(self, pets):
        assert pets("{field: {id: {exact: 7}}}") == []
        assert pets("{field: {id: {inList: [1, 7]}}}") == ["rex"]

    def test_presence_counts_only_the_row_it_lists(self, pets):
        assert pets("{isNull: false}") == ["rex"]
        assert pets("{isNull: true}") == ["stray", "tom"]


# -- A relationship that deletes orphans deletes the rows it lets go of ---------------

WEARER = {
    "_meta": {"update": ["name"], "upsert": {"where": ["name"], "onMatch": "UPDATE"}},
    "badge": {"_meta": {"onReplace": ["DISCONNECT", "DELETE"]}},
}


def badge_repo(
    calls: list[Any], *, kept: tuple[str, ...] = (), hidden: tuple[str, ...] = ()
) -> type:
    class BadgeRepo(AbstractRepo):
        def scope_query(self, query, info):
            return query.where(Badge.name.not_in(hidden))

        def can_delete(self, instance, info):
            calls.append(("can_delete", instance.name))
            return instance.name not in kept

        def on_before_delete(self, instance, info):
            calls.append(("on_before_delete", instance.name))

    return BadgeRepo


async def stored_rows(db: Any) -> dict[str, Any]:
    photos = await resolve(db.session.execute(select(Photo.title, Photo.album_id)))
    badges = await resolve(db.session.execute(select(Badge.name)))
    worn = await resolve(db.session.execute(select(Wearer.badge_id)))
    return {
        "photos": sorted(photos.all()),
        "badges": sorted(badges.scalars()),
        "worn": worn.scalar_one(),
    }


KEPT = {"photos": [("beach", 1), ("hills", 1)], "badges": ["new", "old"], "worn": 1}


class TestOrphansGoAsDeletes:
    @staticmethod
    def repoint(orm: Any, on_replace: str | None = None) -> Any:
        upsert = orm.mutations.upsert_node_input(
            model=Wearer, project=WEARER, name="WearerUpsertInput"
        )
        link_type = member(upsert, "update", "badge")
        options = {} if on_replace is None else {"on_replace": on_replace}
        link = link_type(update=member(link_type, "update")(id="2"), **options)
        return upsert(
            where=member(upsert, "where")(name="Me"),
            update=member(upsert, "update")(badge=link),
        )

    async def test_the_specs_say_which_relations_delete_orphans(self):
        assert _sqlalchemy_relation_specs(Album)["photos"].deletes_orphans
        assert _sqlalchemy_relation_specs(Wearer)["badge"].deletes_orphans
        assert _sqlalchemy_relation_specs(Shelf)["books"].deletes_orphans
        assert not _sqlalchemy_relation_specs(Member)["profile"].deletes_orphans
        assert not _sqlalchemy_relation_specs(Photo)["album"].deletes_orphans

    async def test_unlinking_from_a_list_that_deletes_orphans_is_refused(self, db):
        orm = new_orm()
        ref = orm.ref(Photo, unlink=True, delete=True)
        album = await resolve(db.session.get(Album, 1))
        with pytest.raises(
            UpsertValidationError, match=r"Album\.photos deletes .* send delete"
        ):
            await resolve(
                orm.apply_ref_list(
                    album, "photos", [item(ref, "unlink", id="1")], db.info
                )
            )
        assert await stored_rows(db) == KEPT

    @pytest.mark.parametrize("on_replace", [None, "DISCONNECT", "DELETE"])
    async def test_replacing_a_link_asks_to_delete_what_it_held(self, db, on_replace):
        calls: list[Any] = []
        orm = new_orm(repos={Badge: badge_repo(calls, kept=("old",))})
        with pytest.raises(PermissionError, match="can_delete denied"):
            await resolve(orm.apply_upsert(self.repoint(orm, on_replace), db.info))
        assert calls == [("can_delete", "old")]
        assert await stored_rows(db) == KEPT

    async def test_a_row_the_repo_hides_is_not_left_to_the_cascade(self, db):
        """Refused without naming the row: its key is no more the caller's to
        read than the row is."""
        calls: list[Any] = []
        orm = new_orm(repos={Badge: badge_repo(calls, hidden=("old",))})
        with pytest.raises(PermissionError) as refused:
            await resolve(orm.apply_upsert(self.repoint(orm), db.info))
        assert str(refused.value) == (
            "The Badge the link held would be deleted by re-pointing it, and "
            "cannot be deleted"
        )
        assert calls == []
        assert await stored_rows(db) == KEPT

    async def test_authorize_decides_the_delete_too(self, db):
        orm = new_orm(repos={Badge: badge_repo([])})

        def authorize(action, model, pk, info):
            return action != "delete"

        payload = self.repoint(orm)
        with pytest.raises(PermissionError, match="denied the delete operation"):
            await resolve(orm.apply_upsert(payload, db.info, authorize=authorize))
        assert await stored_rows(db) == KEPT

    @pytest.mark.parametrize("on_replace", [None, "DELETE"])
    async def test_an_allowed_replacement_deletes_the_old_row_once(
        self, db, on_replace
    ):
        calls: list[Any] = []
        orm = new_orm(repos={Badge: badge_repo(calls)})
        await resolve(orm.apply_upsert(self.repoint(orm, on_replace), db.info))
        await resolve(db.session.commit())
        assert calls == [("can_delete", "old"), ("on_before_delete", "old")]
        assert await stored_rows(db) == {**KEPT, "badges": ["new"], "worn": 2}


class WithdrawingBookRepo(AbstractRepo):
    def _delete(self, instance, info):
        instance.withdrawn = True
        self._save(instance, info)

    async def _delete_async(self, instance, info):
        instance.withdrawn = True
        await self._save_async(instance, info)


async def shelved(db: Any) -> dict[str, Any]:
    books = await resolve(db.session.execute(select(Book.title, Book.withdrawn)))
    links = await resolve(db.session.execute(select(shelf_book.c.book_id)))
    return {"books": sorted(books.all()), "shelved": sorted(links.scalars())}


SHELVED = {"books": [("atlas", False), ("diary", False)], "shelved": [1, 2]}


class TestManyToManyOrphans:
    async def edit(self, db: Any, orm: Any, operation: str) -> None:
        ref = orm.ref(Book, unlink=True, delete=True)
        shelf = await resolve(db.session.get(Shelf, 1))
        await resolve(
            orm.apply_ref_list(shelf, "books", [item(ref, operation, id="1")], db.info)
        )
        await resolve(db.session.commit())
        db.session.expunge_all()

    async def test_unlinking_from_it_is_refused(self, db):
        with pytest.raises(
            UpsertValidationError, match=r"Shelf\.books deletes .* send delete"
        ):
            await self.edit(db, new_orm(), "unlink")
        assert await shelved(db) == SHELVED

    async def test_a_delete_is_left_to_the_repo(self, db):
        await self.edit(db, new_orm(repos={Book: WithdrawingBookRepo}), "delete")
        assert await shelved(db) == {
            **SHELVED,
            "books": [("atlas", True), ("diary", False)],
        }

    async def test_a_deleted_row_takes_its_link_rows_with_it(self, db):
        await self.edit(db, new_orm(), "delete")
        assert await shelved(db) == {"books": [("diary", False)], "shelved": [2]}


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
