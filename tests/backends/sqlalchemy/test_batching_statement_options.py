"""Relation batching keeps apart statements that differ only in their options.

A batch runs one parent's statement for every parent whose statement signs
the same. A ``do_orm_execute`` hook can read a statement's execution options
or a ``UserDefinedOption`` to change what it reads, and a loader option shapes
the loads that follow, yet none of them shows in the compiled SQL. Siblings
that differ in one are therefore not batched together.
"""

import pytest
import strawberry
from sqlalchemy import event, select
from sqlalchemy.orm import selectinload, with_loader_criteria
from sqlalchemy.orm.interfaces import UserDefinedOption

from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class ShowDrafts(UserDefinedOption):
    pass


def _shows_drafts(state):
    shown = state.execution_options.get("show_drafts", False) or any(
        isinstance(option, ShowDrafts) and option.payload
        for option in state.statement._with_options
    )
    return shown


@pytest.fixture
def hide_drafts(sa_session):
    """Hide drafts from a statement unless it asks for them, as an app might."""

    def hook(state):
        if state.is_select and not _shows_drafts(state):
            state.statement = state.statement.options(
                with_loader_criteria(SAPost, SAPost.is_published.is_(True))
            )

    event.listen(sa_session, "do_orm_execute", hook)
    yield
    event.remove(sa_session, "do_orm_execute", hook)


def _schema(orm, mark):
    @orm.type(SAPost)
    class PostType:
        title: auto

    @orm.type(SAUser)
    class UserType:
        name: auto

        @orm.field.lazy
        def writing(self, info: strawberry.Info) -> list[PostType]:
            stmt = select(SAPost).where(SAPost.author_id == self.id)
            return mark(stmt.order_by(SAPost.id), self.id == info.context["viewer"])

    @strawberry.type
    class Query:
        @strawberry.field
        def users(self, info: strawberry.Info) -> list[UserType]:
            return select(SAUser).order_by(SAUser.id)

    return orm.schema(query=Query)


def _posts(schema, sa_session, viewer):
    result = schema.execute_sync(
        "{ users { name writing { title } } }",
        context_value={"session": sa_session, "viewer": viewer},
    )
    assert result.errors is None, result.errors
    return {
        user["name"]: [post["title"] for post in user["writing"]]
        for user in result.data["users"]
    }


@pytest.mark.parametrize(
    "mark",
    [
        pytest.param(
            lambda stmt, mine: stmt.execution_options(show_drafts=mine),
            id="execution_options",
        ),
        pytest.param(
            lambda stmt, mine: stmt.options(ShowDrafts(mine)), id="user_defined_option"
        ),
    ],
)
def test_each_parent_keeps_the_options_its_statement_carries(
    orm, sa_session, seed, hide_drafts, mark
):
    """Alice, the first parent, sees her drafts; Bob's stay hidden from her."""
    schema = _schema(orm, mark)
    assert _posts(schema, sa_session, viewer=1) == {
        "Alice": ["Hello World", "GraphQL Guide"],
        "Bob": [],
        "Charlie": ["Rust Adventures"],
    }
    assert _posts(schema, sa_session, viewer=2)["Bob"] == ["Draft Post"]


def test_statements_with_the_same_options_are_still_batched(
    orm, sa_session, seed, hide_drafts
):
    schema = _schema(orm, lambda stmt, mine: stmt.execution_options(show_drafts=True))
    statements: list[str] = []
    event.listen(
        sa_session.bind,
        "before_cursor_execute",
        lambda conn, cursor, statement, *args: statements.append(statement),
    )
    assert _posts(schema, sa_session, viewer=1)["Bob"] == ["Draft Post"]
    assert len(statements) == 2, statements


def _signature(stmt):
    return SQLAlchemyBackend(dialect="sqlite").query_signature(stmt)


def test_a_loader_option_signs_the_values_it_binds():
    """The values bound in a ``selectinload(...and_())`` are not in the SQL."""

    def comments_by(author_id):
        return select(SAPost).options(
            selectinload(SAPost.comments.and_(SAComment.author_id == author_id))
        )

    assert _signature(comments_by(1)) == _signature(comments_by(1))
    assert _signature(comments_by(1)) != _signature(comments_by(2))
    assert _signature(comments_by(1)) != _signature(select(SAPost))


def test_execution_options_sign_by_value_or_by_identity():
    stmt = select(SAPost)
    token, other = object(), object()
    translate = {"main": "tenant_a"}

    assert _signature(stmt.execution_options(yield_per=10)) == _signature(
        stmt.execution_options(yield_per=10)
    )
    assert _signature(stmt.execution_options(show_drafts=1)) != _signature(
        stmt.execution_options(show_drafts=True)
    )
    assert _signature(stmt.execution_options(schema_translate_map=translate)) == (
        _signature(stmt.execution_options(schema_translate_map=dict(translate)))
    )
    assert _signature(
        stmt.execution_options(schema_translate_map={"main": "tenant_b"})
    ) != _signature(stmt.execution_options(schema_translate_map=translate))
    assert _signature(stmt.execution_options(viewer=token)) == _signature(
        stmt.execution_options(viewer=token)
    )
    assert _signature(stmt.execution_options(viewer=token)) != _signature(
        stmt.execution_options(viewer=other)
    )


def test_an_option_with_nothing_to_sign_it_by_is_not_batched():
    assert _signature(select(SAPost).options(ShowDrafts(True))) is None
