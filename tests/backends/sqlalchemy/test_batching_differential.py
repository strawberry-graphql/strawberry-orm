"""Differential batching tests for the SQLAlchemy backend."""

import pytest
import strawberry
from sqlalchemy import or_, select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_differential import (
    AbstractTestBatchingDifferential,
    build_user_post_types,
)
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


def _resolver_for(shape, session):
    base = select(SAPost)
    if shape == "plain":
        return lambda parent: base.where(SAPost.author_id == parent.id)
    if shape == "filtered":
        return lambda parent: base.where(
            SAPost.author_id == parent.id, SAPost.is_published.is_(True)
        )
    if shape == "branching":
        return lambda parent: (
            base.where(SAPost.author_id == parent.id)
            if parent.name == "Alice"
            else base.where(
                SAPost.author_id == parent.id, SAPost.is_published.is_(True)
            )
        )
    if shape == "per_parent_value":
        return lambda parent: base.where(
            SAPost.author_id == parent.id, SAPost.id >= parent.id
        )
    if shape == "ordered":
        return lambda parent: base.where(SAPost.author_id == parent.id).order_by(
            SAPost.title.desc()
        )
    if shape == "excluded":
        return lambda parent: base.where(
            SAPost.author_id == parent.id, SAPost.title != "Draft Post"
        )
    if shape == "negated":
        return lambda parent: base.where(
            SAPost.author_id == parent.id,
            ~SAPost.title.in_(["Draft Post", "nothing"]),
        )
    if shape == "or_clause":
        return lambda parent: base.where(
            or_(SAPost.author_id == parent.id, SAPost.title == "nothing")
        )
    if shape == "sliced":
        return lambda parent: base.where(SAPost.author_id == parent.id).limit(1)
    if shape == "materialized":
        return lambda parent: list(
            session.scalars(base.where(SAPost.author_id == parent.id))
        )
    raise AssertionError(f"unknown shape {shape}")  # pragma: no cover


@pytest.fixture
def extra_users(sa_session):
    def _add(count):
        for index in range(count):
            user = SAUser(name=f"Extra{index}", email=f"extra{index}@example.com")
            sa_session.add(user)
            sa_session.flush()
            sa_session.add(
                SAPost(
                    title=f"Extra post {index}",
                    body="body",
                    is_published=True,
                    author_id=user.id,
                )
            )
        sa_session.flush()

    return _add


class TestBatchingDifferential(AbstractTestBatchingDifferential):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session):
        self._sa_session = sa_session

    def build_schema(self, shape, *, batching):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )
        UT = build_user_post_types(
            orm, SAUser, SAPost, _resolver_for(shape, self._sa_session)
        )

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def random_schema(self, spec, *, batching):
        def resolver(parent):
            stmt = select(SAPost).where(SAPost.author_id == parent.id)
            if spec["published_only"] or (
                spec["branch_on_name"] and parent.name != "Alice"
            ):
                stmt = stmt.where(SAPost.is_published.is_(True))
            if spec["exclude_draft"]:
                stmt = stmt.where(SAPost.title != "Draft Post")
            if spec["extra_predicate"] == "id_gt_0":
                stmt = stmt.where(SAPost.id > 0)
            elif spec["extra_predicate"] == "title_not_null":
                stmt = stmt.where(SAPost.title.is_not(None))
            if spec["order"] == "title":
                stmt = stmt.order_by(SAPost.title)
            elif spec["order"] == "-title":
                stmt = stmt.order_by(SAPost.title.desc())
            elif spec["order"] == "id":
                stmt = stmt.order_by(SAPost.id)
            return stmt

        return self._schema_from(resolver, batching=batching)

    def nested_schema(self, *, batching):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )

        @orm.type(SAComment)
        class CT:
            id: auto
            body: auto

        @orm.type(SAPost)
        class PT:
            id: auto
            title: auto

            @strawberry.field
            def comments(self) -> list[CT]:
                return select(SAComment).where(SAComment.post_id == self.id)  # type: ignore[return-value]

        @orm.type(SAUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts(self) -> list[PT]:
                return select(SAPost).where(SAPost.author_id == self.id)  # type: ignore[return-value]

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def _schema_from(self, resolver, *, batching):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )
        UT = build_user_post_types(orm, SAUser, SAPost, resolver)

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def execute(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self._sa_session})

    def count_queries(self, schema, query):
        from sqlalchemy import event

        statements: list[str] = []

        def _before(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        engine = self._sa_session.bind
        event.listen(engine, "before_cursor_execute", _before)
        try:
            result = schema.execute_sync(
                query, context_value={"session": self._sa_session}
            )
        finally:
            event.remove(engine, "before_cursor_execute", _before)
        assert result.errors is None, result.errors
        return len(statements)
