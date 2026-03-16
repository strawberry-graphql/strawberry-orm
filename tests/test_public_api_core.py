"""Public API tests for wrapper methods and shared field helpers."""

import pytest
import strawberry
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from strawberry.permission import BasePermission

from strawberry_orm import Ordering, StrawberryORM, StringLookup, make_field
from strawberry_orm.backends._base import BaseBackend
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.fixtures import seed as sa_seed_fixture
from tests.backends.sqlalchemy.models import Base as SABase
from tests.backends.sqlalchemy.models import User as SAUser


class DenyPermission(BasePermission):
    message = "denied"

    def has_permission(self, source, info, **kwargs):
        return False


@pytest.fixture
def sa_session():
    engine = create_engine("sqlite:///:memory:")
    SABase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        sa_seed_fixture.__wrapped__(session)
        yield session
    finally:
        session.close()


class TestPublicApiCore:
    def test_make_field_applies_permission_classes(self, sa_session):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto = make_field(permission_classes=[DenyPermission])

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { id name } }",
            context_value={"session": sa_session},
        )
        assert result.errors is not None
        assert "denied" in str(result.errors[0]).lower()

    def test_make_field_without_permissions_registers_hints(self, sa_session):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto = make_field(description="Name field", only=["name"])

        hints = orm.backend._store.get("UserType", "name")
        assert hints is not None
        assert hints.only == ["name"]

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ]
        }

    def test_wrapper_methods_delegate_to_backend(self, sa_session):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")

        TagRef = orm.ref(SAUser, delete=True)
        assert "id" in TagRef.__dataclass_fields__
        assert "delete" in TagRef.__dataclass_fields__

        query = orm.get_default_queryset(SAUser)
        assert orm.is_query_object(query) is True

        UserFilter = orm.filter(SAUser)
        UserOrder = orm.order(SAUser)
        UserField = UserFilter._field_type  # type: ignore[attr-defined]
        filtered = orm.apply_filters(
            query,
            UserFilter(field=UserField(name=StringLookup(exact="Alice"))),
            SAUser,
        )
        ordered = orm.apply_ordering(
            filtered,
            [UserOrder(name=Ordering.ASC)],
            SAUser,
        )
        data = sa_session.execute(ordered).scalars().all()
        assert [user.name for user in data] == ["Alice"]

        ext = orm.optimizer_extension()
        assert isinstance(ext, type)

        @strawberry.type
        class Query:
            ping: str = orm.node(resolver=lambda: "pong")

        schema = strawberry.Schema(query=Query)
        result = schema.execute_sync("{ ping }")
        assert result.errors is None
        assert result.data == {"ping": "pong"}

    def test_invalid_filter_type_errors_during_auto_field_setup(self):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")

        @strawberry.input
        class BadFilter:
            exact: str | None = None

        with pytest.raises(ValueError, match="Cannot infer model"):

            @strawberry.type
            class Query:
                users: list[str] = orm.field(filters=BadFilter)

    def test_backend_default_mutation_field_factories_return_fields(self):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")
        base_backend = BaseBackend()

        @strawberry.input
        class NameInput:
            name: str

        create_field = orm.backend.create(NameInput, description="Create name")
        update_field = orm.backend.update(NameInput, description="Update name")
        delete_field = orm.backend.delete(description="Delete name")
        connection_field = orm.backend.connection(description="Connection field")
        plain_field = orm.backend.field(description="Plain field")
        materialized = base_backend.materialize_query("query", info=None)

        assert create_field.description == "Create name"
        assert update_field.description == "Update name"
        assert delete_field.description == "Delete name"
        assert connection_field.description == "Connection field"
        assert plain_field.description == "Plain field"
        assert materialized == "query"
