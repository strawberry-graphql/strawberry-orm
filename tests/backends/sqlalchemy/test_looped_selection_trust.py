"""A selection that loops back to rows it already read keeps its loads trusted.

``{ users { comments { post { author } } } }`` reaches Alice again as the
author of the posts she commented on, at a level of the plan that does not
load her comments. Each scoped relation is read by a statement of its own
and marked with exactly what it handed each row, so each relation is read
once for all the rows rather than again for each.

A scope that adds loader options of its own can fill a relation before the
plan reaches it, unscoped, and then the relation is still read again.
"""

import pytest
import strawberry
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import Comment, Post, User

orm = StrawberryORM.for_sqlalchemy(
    dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
)


def _options(info, model):
    return info.context["options"].get(model, ())


@orm.type(User)
class UserType:
    name: auto
    comments: list["CommentType"]
    posts: list["PostType"]

    @classmethod
    def scope_rows(cls, stmt, info):
        return stmt.where(User.name != "Charlie")


@orm.type(Post)
class PostType:
    title: auto
    author: UserType | None
    comments: list["CommentType"]

    @classmethod
    def scope_rows(cls, stmt, info):
        stmt = stmt.where(Post.is_published.is_(True))
        return stmt.options(*_options(info, Post))


@orm.type(Comment)
class CommentType:
    body: auto
    post: PostType | None
    author: UserType | None

    @classmethod
    def scope_rows(cls, stmt, info):
        stmt = stmt.where(Comment.parent_id.is_(None))
        return stmt.options(*_options(info, Comment))


@strawberry.type
class Query:
    users: list[UserType] = orm.field.eager()

    @strawberry.field
    def posts(self, info: strawberry.Info) -> list[PostType]:
        return select(Post).order_by(Post.id)


schema = orm.schema(query=Query)


def _run(sa_session, query, options=None):
    sa_session.expunge_all()
    result = schema.execute_sync(
        query, context_value={"session": sa_session, "options": options or {}}
    )
    assert result.errors is None, result.errors
    return result.data


@pytest.fixture
def notes(sa_session, seed):
    """Alice comments on her own posts, so the loop runs through each comment."""
    sa_session.add_all(
        Comment(id=10 + n, body=f"Note {n}", post_id=1 + n % 2, author_id=1)
        for n in range(6)
    )
    sa_session.commit()


def test_a_selection_looping_back_to_its_rows_reads_each_relation_once(
    sa_session, notes, statements
):
    with statements() as sent:
        data = _run(
            sa_session, "{ users { name comments { post { author { name } } } } }"
        )

    read = {
        user["name"]: [
            comment["post"]["author"]["name"] for comment in user["comments"]
        ]
        for user in data["users"]
    }
    assert read == {"Alice": ["Alice"] * 6, "Bob": ["Alice"]}
    assert len(sent) == 4, sent


def test_a_relation_the_root_scope_loaded_is_still_read_again(sa_session, seed):
    """The posts' scope, applied to the resolver's statement, loads Alice's
    comments, her reply too, as their author's; the plan then reaches her as a
    commenter, holding them already."""
    sa_session.add(Comment(id=10, body="Aside", post_id=2, author_id=1))
    sa_session.commit()

    data = _run(
        sa_session,
        "{ posts { comments { author { name comments { body } } } } }",
        {Post: [joinedload(Post.author).selectinload(User.comments)]},
    )

    alice = [
        comment["author"]
        for post in data["posts"]
        for comment in post["comments"]
        if comment["author"] and comment["author"]["name"] == "Alice"
    ]
    assert alice == [{"name": "Alice", "comments": [{"body": "Aside"}]}]


def test_a_relation_a_planned_scope_loaded_is_still_read_again(sa_session, seed):
    """Bob's comments are read under a scope that also loads their posts'
    comments, replies too, before the plan reads the comments of Alice's posts."""
    data = _run(
        sa_session,
        "{ users { name comments { body } posts { title comments { body } } } }",
        {Comment: [joinedload(Comment.post).selectinload(Post.comments)]},
    )

    alice = next(user for user in data["users"] if user["name"] == "Alice")
    assert {post["title"]: post["comments"] for post in alice["posts"]} == {
        "Hello World": [{"body": "Nice post!"}],
        "GraphQL Guide": [{"body": "Great guide"}],
    }
