"""Batching edge cases for the SQLAlchemy backend."""

import pytest
import strawberry
from sqlalchemy import event, select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_edge_cases import AbstractTestBatchingEdgeCases
from tests.backends.sqlalchemy.custom_pk_fixtures import *  # noqa: F401,F403
from tests.backends.sqlalchemy.models import Book as SABook
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import Publisher as SAPublisher
from tests.backends.sqlalchemy.models import Tag as SATag
from tests.backends.sqlalchemy.models import User as SAUser


def _orm(batching):
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        batch_relations=batching,
    )


class TestBatchingEdgeCases(AbstractTestBatchingEdgeCases):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session):
        self._sa_session = sa_session

    def args_schema(self, *, batching):
        orm = _orm(batching)

        @orm.type(SAPost)
        class PT:
            id: auto
            title: auto

        @orm.type(SAUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts_matching(self, title: str) -> list[PT]:
                return select(SAPost).where(  # type: ignore[return-value]
                    SAPost.author_id == self.id, SAPost.title == title
                )

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def custom_pk_schema(self, *, batching):
        orm = _orm(batching)

        @orm.type(SABook)
        class BT:
            id: auto
            title: auto

        @orm.type(SAPublisher)
        class PubT:
            publisher_code: auto
            name: auto

            @strawberry.field
            def books(self) -> list[BT]:
                return (  # type: ignore[return-value]
                    select(SABook)
                    .where(SABook.publisher_code == self.publisher_code)
                    .order_by(SABook.id)
                )

        @strawberry.type
        class Query:
            publishers: list[PubT] = orm.field.auto()

        return orm.schema(query=Query)

    def self_ref_schema(self, *, batching):
        orm = _orm(batching)

        @orm.type(SAComment)
        class CT:
            id: auto
            body: auto

            @strawberry.field
            def replies(self) -> list["SACommentNode"]:  # noqa: F405
                return (  # type: ignore[return-value]
                    select(SAComment)
                    .where(SAComment.parent_id == self.id)
                    .order_by(SAComment.id)
                )

        # Strawberry resolves the forward reference against this module, so the
        # locally built class has to be reachable from module scope.
        globals()["SACommentNode"] = CT

        @strawberry.type
        class Query:
            comments: list[CT] = orm.field.auto()

        return orm.schema(query=Query)

    def join_schema(self, *, batching):
        orm = _orm(batching)

        @orm.type(SAPost)
        class PT:
            id: auto
            title: auto

        @orm.type(SAUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def tagged_posts(self) -> list[PT]:
                mine = (
                    select(SATag.id)
                    .join(SATag.posts)
                    .where(SAPost.author_id == self.id)
                )
                return (  # type: ignore[return-value]
                    select(SAPost)
                    .join(SAPost.tags)
                    .where(SATag.id.in_(mine))
                    .distinct()
                    .order_by(SAPost.id)
                )

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def share_a_tag_across_authors(self):
        session = self._sa_session
        python = session.scalars(select(SATag).where(SATag.name == "python")).one()
        bob = session.scalars(select(SAUser).where(SAUser.name == "Bob")).one()
        shared = SAPost(
            title="Bob shares python", body="b", is_published=True, author_id=bob.id
        )
        shared.tags.append(python)
        session.add(shared)
        session.flush()

    def two_path_schema(self, *, batching):
        return self._plain_schema(batching)

    def duplicate_schema(self, *, batching):
        orm = _orm(batching)
        session = self._sa_session
        _, UT = self._user_post_types(orm)

        @strawberry.type
        class Query:
            @strawberry.field
            def duplicated_users(self) -> list[UT]:
                users = list(session.scalars(select(SAUser).order_by(SAUser.id)))
                return users + users[:1]  # type: ignore[return-value]

        return orm.schema(query=Query)

    def empty_parents_schema(self, *, batching):
        orm = _orm(batching)
        _, UT = self._user_post_types(orm)

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self) -> list[UT]:
                return select(SAUser).where(SAUser.id < 0)  # type: ignore[return-value]

        return orm.schema(query=Query)

    def _user_post_types(self, orm):
        @orm.type(SAPost)
        class PT:
            id: auto
            title: auto

        @orm.type(SAUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts(self) -> list[PT]:
                return (  # type: ignore[return-value]
                    select(SAPost)
                    .where(SAPost.author_id == self.id)
                    .order_by(SAPost.id)
                )

        return PT, UT

    def _plain_schema(self, batching):
        orm = _orm(batching)
        _, UT = self._user_post_types(orm)

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def execute(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self._sa_session})

    def count_queries(self, schema, query):
        statements: list[str] = []

        def _before(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        engine = self._sa_session.bind
        event.listen(engine, "before_cursor_execute", _before)
        try:
            schema.execute_sync(query, context_value={"session": self._sa_session})
        finally:
            event.remove(engine, "before_cursor_execute", _before)
        return len(statements)
