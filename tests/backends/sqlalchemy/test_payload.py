"""``orm.payload`` (SQLAlchemy)."""

import pytest
import strawberry
from sqlalchemy import select
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.payload import (  # noqa: F401 - Errors is resolved by name
    AbstractTestPayload,
    Errors,
    policy,
)
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


def _orm(handles=None, by_name=False):
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        session_getter=lambda info: info.context["session"],
        warn_missing_scope=False,
        payload=policy(
            types=__name__ if by_name else None,
            **({"handles": handles} if handles else {}),
        ),
    )


@pytest.fixture
def orm_with_policy():
    return _orm()


@pytest.fixture
def payload(sa_session):
    def _build(kind, *, fail=None, handles=None, derive=False, by_name=False):
        sa_session.expunge_all()
        orm = _orm(handles, by_name)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(SAUser)
        class UserType(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: list[PostType]

        rows = select(SAUser).order_by(SAUser.id)

        globals()["NamedUserType"] = UserType

        if kind == "query" and by_name:

            @strawberry.type
            class ByName:
                @orm.payload.query
                def users(self) -> list["NamedUserType"]:  # noqa: F821 - resolved via PayloadPolicy.types
                    if fail is not None:
                        raise fail
                    return list(sa_session.execute(rows).unique().scalars().all())

            return orm.schema(query=ByName)

        if kind == "query":

            @strawberry.type
            class Root:
                @orm.payload.query
                def users(self) -> list[UserType]:
                    if fail is not None:
                        raise fail
                    return list(sa_session.execute(rows).unique().scalars().all())

            return orm.schema(query=Root)

        if kind == "mutation":

            @strawberry.type
            class Empty:
                ok: bool = True

            @strawberry.type
            class Mutate:
                @orm.payload.mutation
                def users(self, name: str) -> UserType | None:
                    if fail is not None:
                        raise fail
                    user = sa_session.execute(rows).unique().scalars().first()
                    user.name = name
                    return user

            return orm.schema(query=Empty, mutation=Mutate)

        if derive:

            @strawberry.type
            class DerivedConn:
                # No connection type: it follows from the annotation.
                @orm.payload.connection()
                def users(self) -> list[UserType]:
                    return rows

            return orm.schema(query=DerivedConn)

        @strawberry.type
        class ConnRoot:
            @orm.payload.connection(ORMListConnection[UserType])
            def users(self):
                if fail is not None:
                    raise fail
                return rows

        return orm.schema(query=ConnRoot)

    return _build


@pytest.fixture
def execute(sa_session, query_counter):
    def _execute(schema, query, *, count=False, operation=None):
        before = len(query_counter)
        result = schema.execute_sync(query, context_value={"session": sa_session})
        if not count:
            return result
        return result, len(query_counter) - before

    return _execute


class TestPayload(AbstractTestPayload):
    # Loading onto rows already in memory re-selects them by primary key.
    max_payload_queries = 3
