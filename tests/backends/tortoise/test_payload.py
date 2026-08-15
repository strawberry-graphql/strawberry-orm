"""``orm.payload`` (Tortoise)."""

from contextlib import contextmanager

import pytest
import pytest_asyncio
import strawberry
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.payload import AbstractTestPayloadAsync, policy
from tests.backends.tortoise.models import Post as TPost
from tests.backends.tortoise.models import User as TUser


@contextmanager
def _count_queries():
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


@pytest.fixture
def orm_with_policy():
    return StrawberryORM.for_tortoise(warn_missing_scope=False, payload=policy())


@pytest.fixture
def payload():
    def _build(kind, *, fail=None, handles=None, derive=False, by_name=False):
        orm = StrawberryORM.for_tortoise(
            warn_missing_scope=False,
            payload=policy(
                types=__name__ if by_name else None,
                **({"handles": handles} if handles else {}),
            ),
        )

        @orm.type(TPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(TUser)
        class UserType(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: list[PostType]

        globals()["NamedUserType"] = UserType

        if kind == "query" and by_name:

            @strawberry.type
            class ByName:
                @orm.payload.query
                async def users(self) -> list["NamedUserType"]:  # noqa: F821 - resolved via PayloadPolicy.types
                    return list(await TUser.all().order_by("id"))

            return orm.schema(query=ByName)

        if kind == "query":

            @strawberry.type
            class Root:
                @orm.payload.query
                async def users(self) -> list[UserType]:
                    if fail is not None:
                        raise fail
                    return list(await TUser.all().order_by("id"))

            return orm.schema(query=Root)

        if kind == "mutation":

            @strawberry.type
            class Empty:
                ok: bool = True

            @strawberry.type
            class Mutate:
                @orm.payload.mutation
                async def users(self, name: str) -> UserType | None:
                    if fail is not None:
                        raise fail
                    user = await TUser.all().order_by("id").first()
                    user.name = name
                    return user

            return orm.schema(query=Empty, mutation=Mutate)

        if derive:

            @strawberry.type
            class DerivedConn:
                # No connection type: it follows from the annotation.
                @orm.payload.connection()
                def users(self) -> list[UserType]:
                    return TUser.all().order_by("id")

            return orm.schema(query=DerivedConn)

        @strawberry.type
        class ConnRoot:
            @orm.payload.connection(ORMListConnection[UserType])
            def users(self):
                if fail is not None:
                    raise fail
                return TUser.all().order_by("id")

        return orm.schema(query=ConnRoot)

    return _build


@pytest_asyncio.fixture
async def execute():
    async def _execute(schema, query, *, count=False, operation=None):
        if not count:
            return await schema.execute(query, context_value={})
        with _count_queries() as counter:
            result = await schema.execute(query, context_value={})
        return result, counter["n"]

    return _execute


@pytest.mark.asyncio
class TestPayload(AbstractTestPayloadAsync):
    pass
