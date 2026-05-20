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
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
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
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

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
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

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
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        TagRef = orm.ref(SAUser, delete=True)
        assert "update" in TagRef.__dataclass_fields__
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
        OrderField = UserOrder._field_type  # type: ignore[attr-defined]
        ordered = orm.apply_ordering(
            filtered,
            [UserOrder(field=OrderField(name=Ordering.ASC))],
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
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @strawberry.input
        class BadFilter:
            exact: str | None = None

        with pytest.raises(ValueError, match="Cannot infer model"):

            @strawberry.type
            class Query:
                users: list[str] = orm.field(filters=BadFilter)

    def test_backend_default_mutation_field_factories_return_fields(self):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
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


class TestBareOrmFieldDecorator:
    """Tests for ``@orm.field`` used as a bare decorator (no parentheses)."""

    def test_resolves_single_related_model(self, sa_session):
        """Forward FK: ``self.author`` returns a single related ORM instance."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(SAUser)
        class AuthorType:
            id: auto
            name: auto

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @orm.field
            def author(self) -> AuthorType:
                return self.author

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ posts { title author { name } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "posts": [
                {"title": "Hello World", "author": {"name": "Alice"}},
                {"title": "GraphQL Guide", "author": {"name": "Alice"}},
                {"title": "Draft Post", "author": {"name": "Bob"}},
                {"title": "Rust Adventures", "author": {"name": "Charlie"}},
            ]
        }

    def test_resolves_list_of_related_models(self, sa_session):
        """Reverse FK: ``self.posts`` returns a list of related ORM instances."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto

            @orm.field
            def posts(self) -> list[PostType]:
                return self.posts

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        alice = next(u for u in result.data["users"] if u["name"] == "Alice")
        assert sorted(p["title"] for p in alice["posts"]) == [
            "GraphQL Guide",
            "Hello World",
        ]

    def test_resolves_computed_scalar(self, sa_session):
        """Bare ``@orm.field`` works for computed scalar values."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto

            @orm.field
            def name_upper(self) -> str:
                return self.name.upper()

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name nameUpper } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice", "nameUpper": "ALICE"},
                {"name": "Bob", "nameUpper": "BOB"},
                {"name": "Charlie", "nameUpper": "CHARLIE"},
            ]
        }

    def test_resolver_receives_info(self, sa_session):
        """Bare ``@orm.field`` passes ``info`` to resolvers that accept it."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto

            @orm.field
            def context_check(self, info: strawberry.types.Info) -> bool:
                return "session" in info.context

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name contextCheck } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert all(u["contextCheck"] is True for u in result.data["users"])

    def test_resolves_queryset_on_root_query(self, sa_session):
        """Bare ``@orm.field`` on a root query method returning a queryset."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto

        @strawberry.type
        class Query:
            @orm.field
            def users(self) -> list[UserType]:
                return orm.get_default_queryset(SAUser)

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        names = sorted(u["name"] for u in result.data["users"])
        assert names == ["Alice", "Bob", "Charlie"]

    def test_parenthesized_form_still_works(self, sa_session):
        """``@orm.field()`` with parentheses continues to work unchanged."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto

        @strawberry.type
        class Query:
            @orm.field()
            def users(self) -> list[UserType]:
                return orm.get_default_queryset(SAUser)

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        names = sorted(u["name"] for u in result.data["users"])
        assert names == ["Alice", "Bob", "Charlie"]

    def test_nested_related_model_chain(self, sa_session):
        """Nested ``@orm.field`` resolvers: post -> author, comment -> post -> author."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(SAUser)
        class AuthorType:
            id: auto
            name: auto

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @orm.field
            def author(self) -> AuthorType:
                return self.author

        @orm.type(SAComment)
        class CommentType:
            id: auto
            body: auto

            @orm.field
            def post(self) -> PostType:
                return self.post

        @strawberry.type
        class Query:
            comments: list[CommentType] = orm.field()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ comments { body post { title author { name } } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        nice_post = next(
            c for c in result.data["comments"] if c["body"] == "Nice post!"
        )
        assert nice_post["post"]["title"] == "Hello World"
        assert nice_post["post"]["author"]["name"] == "Alice"
