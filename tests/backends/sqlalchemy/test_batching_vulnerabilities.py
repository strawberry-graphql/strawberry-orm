"""Adversarial batching tests for the SQLAlchemy backend."""

import pytest
import strawberry
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_vulnerabilities import (
    AbstractTestBatchingVulnerabilities,
)
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class TestBatchingVulnerabilities(AbstractTestBatchingVulnerabilities):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session):
        self._sa_session = sa_session

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
                return (  # type: ignore[return-value]
                    select(SAComment)
                    .where(SAComment.post_id == self.id)
                    .order_by(SAComment.id)
                )

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

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def give_every_user_a_comment(self):
        session = self._sa_session
        author = session.scalars(select(SAUser).where(SAUser.name == "Alice")).one()
        others = session.scalars(select(SAUser).where(SAUser.name != "Alice")).all()
        for user in others:
            post = session.scalars(
                select(SAPost).where(SAPost.author_id == user.id)
            ).first()
            session.add(
                SAComment(
                    body=f"comment for {user.name}",
                    post_id=post.id,
                    author_id=author.id,
                )
            )
        session.flush()

    def parent_scoped_query(self):
        from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend

        backend = SQLAlchemyBackend(dialect="sqlite", warn_missing_scope=False)
        return backend, select(SAPost).where(SAPost.author_id == 1)

    def row_ids(self, query):
        return sorted(row.id for row in self._sa_session.scalars(query))

    def tenant_schema(self, *, batching):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )

        @orm.type(SAPost)
        class PT:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                if info.context["published_only"]:
                    return qs.where(SAPost.is_published.is_(True))
                return qs

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

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def tenant_expectations(self):
        published = {"Hello World", "GraphQL Guide", "Rust Adventures"}
        return [
            (True, published),
            (False, published | {"Draft Post"}),
        ]

    def execute_with_context(self, schema, query, published_only):
        return schema.execute_sync(
            query,
            context_value={
                "session": self._sa_session,
                "published_only": published_only,
            },
        )

    def execute(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self._sa_session})
