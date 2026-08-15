"""``orm.optimize`` on materialized rows (Tortoise)."""

import inspect
from contextlib import contextmanager

import pytest
import pytest_asyncio
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.optimize_api import (
    PAYLOAD_QUERY,
    AbstractTestOptimizeAPIAsync,
    build_selection_info,
)
from tests.backends.tortoise.models import Post as TPost
from tests.backends.tortoise.models import User as TUser


@contextmanager
def _count_queries():
    """Count statements by wrapping the default client's execute methods."""
    from tortoise import connections

    client = connections.get("default")
    counter = {"n": 0}
    originals = {}

    def _wrap(original):
        async def counting(*args, **kwargs):
            counter["n"] += 1
            return await original(*args, **kwargs)

        return counting

    for name in ("execute_query", "execute_query_dict"):
        original = getattr(client, name, None)
        if original is None:  # pragma: no cover - both exist on the sqlite client
            continue
        originals[name] = original
        setattr(client, name, _wrap(original))
    try:
        yield counter
    finally:
        for name, original in originals.items():
            setattr(client, name, original)


def _build_schema(*, optimize, scoped, at, shape="list", mutate=None):
    orm = StrawberryORM.for_tortoise(warn_missing_scope=False)

    if scoped:

        @orm.type(TPost)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.filter(is_published=True)

    else:

        @orm.type(TPost)
        class PostType:
            id: auto
            title: auto

    @orm.type(TUser)
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
        async def users(self, info: strawberry.types.Info) -> Payload:
            queryset = TUser.all().order_by("id")
            if shape == "query":
                rows = queryset
            else:
                materialized = list(await queryset)
                rows = materialized[0] if shape == "one" else materialized
            if mutate is not None:
                rows = mutate(rows)
            if optimize:
                rows = orm.optimize(rows, info, at=at)
                if inspect.isawaitable(rows):
                    rows = await rows
            return Payload(data=rows, errors=None)

    return orm.schema(query=Query)


@pytest_asyncio.fixture
async def run_payload():
    async def _run(*, optimize, scoped=False, at="data", shape="list"):
        schema = _build_schema(optimize=optimize, scoped=scoped, at=at, shape=shape)
        with _count_queries() as counter:
            result = await schema.execute(PAYLOAD_QUERY, context_value={})
        assert result.errors is None, result.errors
        return result.data["users"], counter["n"]

    return _run


@pytest.fixture
def selection_info():
    return build_selection_info()


@pytest_asyncio.fixture
async def run_dirty_scalar():
    def _keep_alice_renamed(rows):
        alice = [row for row in rows if row.name == "Alice"]
        alice[0].name = "Alice renamed"
        return alice

    async def _run():
        schema = _build_schema(
            optimize=True, scoped=True, at="data", mutate=_keep_alice_renamed
        )
        result = await schema.execute(PAYLOAD_QUERY, context_value={})
        assert result.errors is None, result.errors
        data = result.data["users"]
        for row in data["data"]:
            row["posts"] = row["posts"][:1]
        return data

    return _run


@pytest.mark.asyncio
class TestOptimizeAPI(AbstractTestOptimizeAPIAsync):
    pass
