"""A ``scope_rows`` that joins is applied whole when its rows load as a relation.

A loader option carries only a WHERE clause. Kept on its own, the WHERE clause
of ``select.join(Membership).where(Membership.user_id == me)`` names a table
the loader never joins: every child of a visible parent matched, or the
statement failed. So a scope that says more than a WHERE clause on the related
table's own columns is read by a statement of its own, narrowed to the
parents' keys, on every kind of relation and by both ways rows are loaded.
"""

import pytest
import strawberry
from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Table,
    create_engine,
    event,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    with_loader_criteria,
)

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class Base(DeclarativeBase):
    pass


team_projects = Table(
    "team_projects",
    Base.metadata,
    Column("team_id", ForeignKey("teams.id"), primary_key=True),
    Column("project_id", ForeignKey("projects.id"), primary_key=True),
)

team_releases = Table(
    "team_releases",
    Base.metadata,
    Column("team_id", ForeignKey("teams.id"), primary_key=True),
    Column("project_id", primary_key=True),
    Column("number", primary_key=True),
    ForeignKeyConstraint(
        ["project_id", "number"], ["releases.project_id", "releases.number"]
    ),
)


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    projects: Mapped[list["Project"]] = relationship(back_populates="org")
    live_projects: Mapped[list["Project"]] = relationship(
        primaryjoin="and_(Org.id == Project.org_id, Project.name != 'gone')",
        viewonly=True,
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"))
    org: Mapped[Org] = relationship(back_populates="projects")
    tasks: Mapped[list["Task"]] = relationship(back_populates="project")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    project: Mapped[Project | None] = relationship(back_populates="tasks")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    projects: Mapped[list[Project]] = relationship(secondary=team_projects)
    live_projects: Mapped[list[Project]] = relationship(
        secondary=team_projects,
        secondaryjoin="and_(Project.id == team_projects.c.project_id, "
        "Project.name != 'gone')",
        viewonly=True,
    )
    releases: Mapped[list["Release"]] = relationship(
        secondary=team_releases, viewonly=True
    )


class Release(Base):
    __tablename__ = "releases"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    number: Mapped[int] = mapped_column(primary_key=True)


class Membership(Base):
    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))


ME = 7


def seed(session):
    acme, initech = Org(id=1, name="acme"), Org(id=2, name="initech")
    mine = Project(id=1, name="mine", org=acme)
    merger = Project(id=2, name="SECRET merger", org=acme)
    layoffs = Project(id=3, name="SECRET layoffs", org=initech)
    also_mine = Project(id=4, name="also mine", org=initech)
    session.add_all([acme, initech, mine, merger, layoffs, also_mine])
    session.add_all(
        [
            Task(id=1, title="on mine", project=mine),
            Task(id=2, title="on the merger", project=merger),
            Task(id=3, title="on nothing", project=None),
            Team(id=1, name="core", projects=[mine, merger, layoffs]),
            Team(id=2, name="ops", projects=[layoffs, also_mine]),
            Membership(id=1, user_id=ME, project_id=1),
            Membership(id=2, user_id=ME, project_id=4),
            Membership(id=3, user_id=99, project_id=2),
        ]
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        session.commit()
        session.expunge_all()
        yield session


def members_only(stmt, info):
    return stmt.join(Membership, Membership.project_id == Project.id).where(
        Membership.user_id == info.context["me"]
    )


def not_secret(stmt, info):
    return stmt.options(
        with_loader_criteria(Project, ~Project.name.startswith("SECRET"))
    )


def build(scope=members_only):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )

    @orm.type(Project)
    class ProjectType:
        id: auto
        name: auto

        scope_rows = classmethod(lambda cls, stmt, info: scope(stmt, info))

    @orm.type(Org)
    class OrgType:
        name: auto
        projects: list[ProjectType]

        @orm.field.computed(using=["projects"])
        def project_names(self) -> list[str]:
            return sorted(project.name for project in self.projects)

    @orm.type(Task)
    class TaskType:
        title: auto
        project: ProjectType | None

    @orm.type(Team)
    class TeamType:
        name: auto
        projects: list[ProjectType]

    @strawberry.type
    class Query:
        orgs: list[OrgType] = orm.field.eager()
        tasks: list[TaskType] = orm.field.eager()
        teams: list[TeamType] = orm.field.eager()

        @strawberry.field
        def org_rows(self, info: strawberry.Info) -> list[OrgType]:
            return [info.context["session"].get(Org, pk) for pk in (1, 2)]

        @strawberry.field
        def task_rows(self, info: strawberry.Info) -> list[TaskType]:
            return [info.context["session"].get(Task, pk) for pk in (1, 2, 3)]

        @strawberry.field
        def team_rows(self, info: strawberry.Info) -> list[TeamType]:
            return [info.context["session"].get(Team, pk) for pk in (1, 2)]

    return orm, orm.schema(query=Query)


def run(schema, session, query):
    session.expunge_all()
    result = schema.execute_sync(query, context_value={"session": session, "me": ME})
    assert result.errors is None, result.errors
    return result.data


def names(projects):
    return sorted(project["name"] for project in projects)


SCOPES = pytest.mark.parametrize("scope", [members_only, not_secret])


@SCOPES
@pytest.mark.parametrize("field", ["orgs", "orgRows"])
def test_a_to_many_relation_keeps_the_whole_scope(session, scope, field):
    _orm, schema = build(scope)
    data = run(schema, session, f"{{ {field} {{ name projects {{ name }} }} }}")
    assert {org["name"]: names(org["projects"]) for org in data[field]} == {
        "acme": ["mine"],
        "initech": ["also mine"],
    }


@SCOPES
@pytest.mark.parametrize("field", ["orgs", "orgRows"])
def test_a_relation_read_through_using_keeps_the_whole_scope(session, scope, field):
    _orm, schema = build(scope)
    data = run(schema, session, f"{{ {field} {{ name projectNames }} }}")
    assert {org["name"]: org["projectNames"] for org in data[field]} == {
        "acme": ["mine"],
        "initech": ["also mine"],
    }


@SCOPES
@pytest.mark.parametrize("field", ["tasks", "taskRows"])
def test_a_to_one_relation_keeps_the_whole_scope(session, scope, field):
    _orm, schema = build(scope)
    data = run(schema, session, f"{{ {field} {{ title project {{ name }} }} }}")
    assert {task["title"]: task["project"] for task in data[field]} == {
        "on mine": {"name": "mine"},
        "on the merger": None,
        "on nothing": None,
    }


@SCOPES
@pytest.mark.parametrize("field", ["teams", "teamRows"])
def test_a_many_to_many_relation_keeps_the_whole_scope(session, scope, field):
    _orm, schema = build(scope)
    data = run(schema, session, f"{{ {field} {{ name projects {{ name }} }} }}")
    assert {team["name"]: names(team["projects"]) for team in data[field]} == {
        "core": ["mine"],
        "ops": ["also mine"],
    }


@SCOPES
async def test_a_many_to_many_relation_keeps_the_whole_scope_on_an_async_session(
    scope,
):
    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _orm, schema = build(scope)
    try:
        async with AsyncSession(engine) as session:
            await session.run_sync(seed)
            await session.commit()
            session.expunge_all()
            result = await schema.execute(
                "{ teams { name projects { name } } }",
                context_value={"session": session, "me": ME},
            )
    finally:
        await engine.dispose()
    assert result.errors is None, result.errors
    assert {team["name"]: names(team["projects"]) for team in result.data["teams"]} == {
        "core": ["mine"],
        "ops": ["also mine"],
    }


def test_the_whole_scope_is_read_once_per_relation_not_per_parent(session):
    _orm, schema = build()
    statements = []
    engine = session.get_bind()
    listener = lambda *args: statements.append(args[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        run(
            schema,
            session,
            "{ orgs { projects { name } } tasks { project { name } } "
            "teams { projects { name } } }",
        )
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    # Each root and each relation one statement apiece, and the association
    # table one more.
    assert len(statements) == 7, statements


def test_a_failed_association_read_names_no_table(session):
    _orm, schema = build()
    team_projects.drop(session.get_bind())
    session.expunge_all()
    result = schema.execute_sync(
        "{ teams { projects { name } } }",
        context_value={"session": session, "me": ME},
    )
    assert result.errors is not None
    assert result.errors[0].message == "Invalid filter expression"


@pytest.mark.parametrize(
    ("query", "relation"),
    [
        ("{ orgs { liveProjects { name } } }", "LiveOrg.live_projects"),
        ("{ teams { liveProjects { name } } }", "LiveTeam.live_projects"),
        ("{ teams { releases { number } } }", "LiveTeam.releases"),
    ],
)
def test_a_relation_whose_join_says_more_refuses_a_scope_it_cannot_carry(
    session, query, relation
):
    orm, _schema = build()
    ProjectType = orm.backend._graphql_type_registry[Project]

    @orm.type(Release)
    class ReleaseType:
        number: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.join(
                Membership, Membership.project_id == Release.project_id
            ).where(Membership.user_id == info.context["me"])

    @orm.type(Org, name="LiveOrg")
    class LiveOrgType:
        name: auto
        live_projects: list[ProjectType]

    @orm.type(Team, name="LiveTeam")
    class LiveTeamType:
        name: auto
        live_projects: list[ProjectType]
        releases: list[ReleaseType]

    @strawberry.type
    class Query:
        orgs: list[LiveOrgType] = orm.field.eager()
        teams: list[LiveTeamType] = orm.field.eager()

    session.expunge_all()
    result = orm.schema(query=Query).execute_sync(
        query, context_value={"session": session, "me": ME}
    )
    assert result.errors is not None
    message = result.errors[0].message
    assert relation in message
    assert ".where()" in message
