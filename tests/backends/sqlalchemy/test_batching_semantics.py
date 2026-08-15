"""Batching semantics for the SQLAlchemy backend."""

import pytest
import strawberry
from sqlalchemy import event, select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_semantics import AbstractTestBatchingSemantics
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class TestBatchingSemantics(AbstractTestBatchingSemantics):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session):
        self._sa_session = sa_session

    def schema_for(self, shape, *, batching=True):
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

        @orm.type(SAUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts(self) -> list[PT]:
                base = select(SAPost).where(SAPost.author_id == self.id)
                if shape == "ordered":
                    return base.order_by(SAPost.title.desc())  # type: ignore[return-value]
                if shape == "raises_for_bob" and self.name == "Bob":
                    raise RuntimeError("no posts for Bob")
                if shape in ("filtered", "aliased", "raises_for_bob"):
                    return base.where(SAPost.is_published.is_(True))  # type: ignore[return-value]
                return base  # type: ignore[return-value]

            @strawberry.field
            def all_posts(self) -> list[PT]:
                return select(SAPost).where(SAPost.author_id == self.id)  # type: ignore[return-value]

            @strawberry.field
            def maybe_posts(self) -> list[PT] | None:
                if shape == "raises_for_bob" and self.name == "Bob":
                    raise RuntimeError("no posts for Bob")
                return select(SAPost).where(SAPost.author_id == self.id)  # type: ignore[return-value]

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
