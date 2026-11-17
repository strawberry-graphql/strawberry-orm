"""An ``on=`` view reads the rows its relation lists, or is refused (SQLAlchemy).

The view is a statement per parent that the batcher collapses, built from the
relation's join keys. A relation those keys do not describe - one whose
``primaryjoin`` says more, one joined on a column other than the parent's
primary key, or a to-one relation - would list other rows than the relation
does, or cost a query per parent, so it is refused when the type is defined.
"""

import pytest
import strawberry
from sqlalchemy import Boolean, ForeignKey, String, create_engine, event
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "on_keys_author"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[int] = mapped_column(unique=True)
    name: Mapped[str] = mapped_column(String(50))
    books: Mapped[list["Book"]] = relationship(back_populates="author")


class Book(Base):
    __tablename__ = "on_keys_book"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    author_code: Mapped[int] = mapped_column(ForeignKey("on_keys_author.code"))
    author: Mapped[Author] = relationship(back_populates="books")


class Project(Base):
    __tablename__ = "on_keys_project"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    tasks: Mapped[list["Task"]] = relationship(viewonly=True)
    open_tasks: Mapped[list["Task"]] = relationship(
        primaryjoin="and_(Task.project_id == Project.id, Task.done == False)",
        viewonly=True,
    )


class Task(Base):
    __tablename__ = "on_keys_task"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    project_id: Mapped[int] = mapped_column(ForeignKey("on_keys_project.id"))


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Project(id=1, name="Launch"),
                Project(id=2, name="Second"),
                Task(id=1, title="Open task", project_id=1),
                Task(id=2, title="Done task", done=True, project_id=1),
                Task(id=3, title="Done elsewhere", done=True, project_id=2),
            ]
        )
        session.commit()
        yield session


def _orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


def test_a_view_of_a_relation_with_a_narrowed_join_is_refused():
    orm = _orm()

    @orm.type(Task)
    class TaskType:
        title: auto

    with pytest.raises(ValueError, match="says more than the key comparison"):

        @orm.type(Project)
        class ProjectType:
            name: auto
            todo: list[TaskType] = orm.field.eager(on="open_tasks")


def test_a_view_of_a_relation_joined_on_another_column_is_refused():
    orm = _orm()

    @orm.type(Book)
    class BookType:
        title: auto

    with pytest.raises(ValueError, match="joins on Author.code rather than"):

        @orm.type(Author)
        class AuthorType:
            name: auto
            catalogue: list[BookType] = orm.field.eager(on="books")


@pytest.mark.parametrize("many", [False, True])
def test_a_view_of_a_to_one_relation_is_refused(many):
    orm = _orm()

    @orm.type(Author)
    class AuthorType:
        name: auto

    with pytest.raises(ValueError, match="is a to-one relation"):
        if many:

            @orm.type(Book)
            class BookType:
                title: auto
                writers: list[AuthorType] = orm.field.eager(on="author")

        else:

            @orm.type(Book)
            class BookType:
                title: auto
                writer: AuthorType | None = orm.field.eager(on="author")


def test_a_view_lists_the_rows_of_its_relation_in_one_statement(session):
    orm = _orm()

    @orm.type(Task)
    class TaskType:
        title: auto

    @orm.type(Project)
    class ProjectType:
        name: auto
        tasks: list[TaskType]
        finished: list[TaskType] = orm.field.eager(
            on="tasks", scope=lambda stmt, info: stmt.where(Task.done.is_(True))
        )

    @strawberry.type
    class Query:
        projects: list[ProjectType] = orm.field.eager()

    statements: list[str] = []
    event.listen(
        session.bind,
        "before_cursor_execute",
        lambda conn, cursor, statement, *args: statements.append(statement),
    )
    result = orm.schema(query=Query).execute_sync(
        "{ projects { name finished { title } } }", context_value={"session": session}
    )
    assert result.errors is None, result.errors
    assert result.data["projects"] == [
        {"name": "Launch", "finished": [{"title": "Done task"}]},
        {"name": "Second", "finished": [{"title": "Done elsewhere"}]},
    ]
    assert len(statements) == 2, statements
