"""``orm.payload`` (Django)."""

import pytest
import strawberry
from django.db import connection
from django.test.utils import CaptureQueriesContext
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.payload import (  # noqa: F401 - Errors is resolved by name
    AbstractTestPayload,
    Errors,
    policy,
)
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


@pytest.fixture
def orm_with_policy():
    return StrawberryORM.for_django(warn_missing_scope=False, payload=policy())


@pytest.fixture
def payload():
    def _build(kind, *, fail=None, handles=None, derive=False, by_name=False):
        orm = StrawberryORM.for_django(
            warn_missing_scope=False,
            payload=policy(
                types=__name__ if by_name else None,
                **({"handles": handles} if handles else {}),
            ),
        )

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(DjUser)
        class UserType(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: list[PostType]

        globals()["NamedUserType"] = UserType

        if kind == "query" and by_name:

            @strawberry.type
            class ByName:
                @orm.payload.query
                def users(self) -> list["NamedUserType"]:  # noqa: F821 - resolved via PayloadPolicy.types
                    if fail is not None:
                        raise fail
                    return list(DjUser.objects.order_by("id"))

            return orm.schema(query=ByName)

        if kind == "query":

            @strawberry.type
            class Root:
                @orm.payload.query
                def users(self) -> list[UserType]:
                    if fail is not None:
                        raise fail
                    return list(DjUser.objects.order_by("id"))

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
                    user = DjUser.objects.order_by("id").first()
                    user.name = name
                    return user

            return orm.schema(query=Empty, mutation=Mutate)

        if derive:

            @strawberry.type
            class DerivedConn:
                # No connection type: it follows from the annotation.
                @orm.payload.connection()
                def users(self) -> list[UserType]:
                    return DjUser.objects.order_by("id")

            return orm.schema(query=DerivedConn)

        @strawberry.type
        class ConnRoot:
            @orm.payload.connection(ORMListConnection[UserType])
            def users(self):
                if fail is not None:
                    raise fail
                return DjUser.objects.order_by("id")

        return orm.schema(query=ConnRoot)

    return _build


@pytest.fixture
def execute():
    def _execute(schema, query, *, count=False, operation=None):
        if not count:
            return schema.execute_sync(query, context_value={})
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(query, context_value={})
        return result, len(ctx)

    return _execute


@pytest.mark.django_db
class TestPayload(AbstractTestPayload):
    pass
