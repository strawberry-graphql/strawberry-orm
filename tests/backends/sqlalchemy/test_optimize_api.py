"""``orm.optimize`` on materialized rows (SQLAlchemy)."""

import pytest
import strawberry
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.optimize_api import (
    PAYLOAD_QUERY,
    AbstractTestOptimizeAPI,
    build_selection_info,
)
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


def _build_schema(session, *, optimize, scoped, at, shape="list", mutate=None):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        session_getter=lambda info: info.context["session"],
        warn_missing_scope=False,
    )

    if scoped:

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(SAPost.is_published.is_(True))

    else:

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

    @orm.type(SAUser)
    class UserType:
        id: auto
        name: auto
        posts: list[PostType]

    if shape == "one":

        @strawberry.type
        class Payload:
            data: UserType | None
            errors: str | None

    else:

        @strawberry.type
        class Payload:
            data: list[UserType] | None
            errors: str | None

    @strawberry.type
    class Query:
        @strawberry.field
        def users(self, info: strawberry.types.Info) -> Payload:
            stmt = select(SAUser).order_by(SAUser.id)
            if shape == "query":
                rows = stmt
            else:
                materialized = list(session.execute(stmt).unique().scalars().all())
                rows = materialized[0] if shape == "one" else materialized
            if mutate is not None:
                rows = mutate(rows)
            if optimize:
                rows = orm.optimize(rows, info, at=at)
            return Payload(data=rows, errors=None)

    return orm.schema(query=Query)


@pytest.fixture
def run_payload(sa_session, query_counter):
    def _run(*, optimize, scoped=False, at="data", shape="list"):
        # A warm identity map would serve relations loaded by an earlier run.
        sa_session.expunge_all()
        schema = _build_schema(
            sa_session, optimize=optimize, scoped=scoped, at=at, shape=shape
        )
        before = len(query_counter)
        result = schema.execute_sync(
            PAYLOAD_QUERY, context_value={"session": sa_session}
        )
        assert result.errors is None, result.errors
        return result.data["users"], len(query_counter) - before

    return _run


@pytest.fixture
def selection_info():
    return build_selection_info()


@pytest.fixture
def run_dirty_scalar(sa_session):
    def _keep_alice_renamed(rows):
        alice = [row for row in rows if row.name == "Alice"]
        alice[0].name = "Alice renamed"
        return alice

    def _run():
        sa_session.expunge_all()
        schema = _build_schema(
            sa_session,
            optimize=True,
            scoped=True,
            at="data",
            mutate=_keep_alice_renamed,
        )
        result = schema.execute_sync(
            PAYLOAD_QUERY, context_value={"session": sa_session}
        )
        assert result.errors is None, result.errors
        data = result.data["users"]
        for row in data["data"]:
            row["posts"] = row["posts"][:1]
        return data

    return _run


class TestOptimizeAPI(AbstractTestOptimizeAPI):
    pass
