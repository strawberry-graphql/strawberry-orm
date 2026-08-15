"""``orm.optimize`` on materialized rows (Django)."""

import pytest
import strawberry
from django.db import connection
from django.test.utils import CaptureQueriesContext

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.optimize_api import (
    PAYLOAD_QUERY,
    AbstractTestOptimizeAPI,
    build_selection_info,
)


def _build_schema(User, Post, *, optimize, scoped, at, shape="list", mutate=None):
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    if scoped:

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.filter(is_published=True)

    else:

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

    @orm.type(User)
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
            queryset = User.objects.order_by("id")
            if shape == "query":
                rows = queryset
            elif shape == "one":
                rows = queryset.first()
            else:
                rows = list(queryset)
            if mutate is not None:
                rows = mutate(rows)
            if optimize:
                rows = orm.optimize(rows, info, at=at)
            return Payload(data=rows, errors=None)

    return orm.schema(query=Query)


@pytest.fixture
def run_payload(User, Post):
    def _run(*, optimize, scoped=False, at="data", shape="list"):
        schema = _build_schema(
            User, Post, optimize=optimize, scoped=scoped, at=at, shape=shape
        )
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(PAYLOAD_QUERY, context_value={})
        assert result.errors is None, result.errors
        return result.data["users"], len(ctx)

    return _run


@pytest.fixture
def selection_info():
    return build_selection_info()


@pytest.fixture
def run_dirty_scalar(User, Post):
    def _keep_alice_renamed(rows):
        alice = [row for row in rows if row.name == "Alice"]
        alice[0].name = "Alice renamed"
        return alice

    def _run():
        schema = _build_schema(
            User,
            Post,
            optimize=True,
            scoped=True,
            at="data",
            mutate=_keep_alice_renamed,
        )
        result = schema.execute_sync(
            """
            { users { errors data { name posts(first: 1) { title } } } }
            """.replace("posts(first: 1)", "posts"),
            context_value={},
        )
        assert result.errors is None, result.errors
        data = result.data["users"]
        # Trim to the first post so the expectation stays about the scalar.
        for row in data["data"]:
            row["posts"] = row["posts"][:1]
        return data

    return _run


@pytest.mark.django_db
class TestOptimizeAPI(AbstractTestOptimizeAPI):
    pass
