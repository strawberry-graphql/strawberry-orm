"""Schema tags on what is not backend-specific: arguments, inputs, roots, errors."""

from collections.abc import Iterable
from typing import Annotated

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import TAGS_KEY, PayloadPolicy, StrawberryORM, auto, tags
from strawberry_orm.relay import ORMListConnection
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


def _field(type_: type, name: str):
    return next(
        f for f in type_.__strawberry_definition__.fields if f.python_name == name
    )


class TestTagValues:
    def test_tags_are_field_metadata(self):
        assert tags("a", "b") == {TAGS_KEY: frozenset({"a", "b"})}

    @pytest.mark.parametrize("names", [(), ("",), (1,)])
    def test_tags_need_names(self, names):
        with pytest.raises(ValueError, match=r"tags\(\) needs one or more tag names"):
            tags(*names)

    def test_tags_join_the_tags_already_in_metadata(self, orm):
        def resolver(self, info: strawberry.Info) -> int:
            return 1

        field = orm.field.lazy(tags="b", metadata={**tags("a"), "owner": "team"})(
            resolver
        )
        assert field.metadata == {TAGS_KEY: frozenset({"a", "b"}), "owner": "team"}

    def test_a_schema_needs_tag_names(self, orm):
        @strawberry.type
        class Query:
            ok: bool = True

        with pytest.raises(ValueError, match=r"orm.schema\(tags=\) needs"):
            orm.schema(query=Query, tags=[])


@strawberry.input
class SignUp:
    name: str
    referrer: str | None = strawberry.field(default=None, metadata=tags("internal"))


@strawberry.type
class Query:
    @strawberry.field
    def greet(
        self,
        name: str,
        loud: Annotated[bool, strawberry.argument(metadata=tags("internal"))] = False,
    ) -> str:
        return name.upper() if loud else name

    @strawberry.field
    def sign_up(self, input: SignUp) -> str:
        return input.referrer or "direct"


@strawberry.type
class Mutation:
    @strawberry.mutation
    def ping(self) -> bool:
        return True

    @strawberry.mutation(metadata=tags("internal"))
    def purge(self) -> bool:
        return True


class TestRootsArgumentsAndInputs:
    def test_mutations_are_tagged_like_fields(self, orm):
        public = str(orm.schema(query=Query, mutation=Mutation, tags="public"))
        assert "ping: Boolean!" in public
        assert "purge" not in public

    def test_a_hidden_argument_takes_its_default(self, orm):
        public = orm.schema(query=Query, tags="public")
        assert "greet(name: String!): String!" in str(public)
        result = public.execute_sync('{ greet(name: "ada") }')
        assert result.data == {"greet": "ada"}

    def test_a_kept_argument_is_still_there(self, orm):
        internal = orm.schema(query=Query, tags="internal")
        result = internal.execute_sync('{ greet(name: "ada", loud: true) }')
        assert result.data == {"greet": "ADA"}

    def test_a_field_without_hidden_arguments_is_used_as_is(self, orm):
        public = orm.schema(query=Query, tags="public")
        definition = Query.__strawberry_definition__
        [sign_up] = [f for f in public.get_fields(definition) if f.name == "sign_up"]
        assert sign_up is _field(Query, "sign_up")

    def test_a_field_with_hidden_arguments_is_copied_once(self, orm):
        greet = _field(Query, "greet")
        greet._orm_marker = "kept"
        try:
            public = orm.schema(query=Query, tags="public")
            definition = Query.__strawberry_definition__
            first, second = (
                next(f for f in public.get_fields(definition) if f.name == "greet")
                for _ in range(2)
            )
        finally:
            del greet._orm_marker
        assert first is second
        assert first is not greet
        assert first._orm_marker == "kept"

    def test_a_hidden_input_field_takes_its_default(self, orm):
        public = orm.schema(query=Query, tags="public")
        assert "referrer" not in str(public)
        result = public.execute_sync('{ signUp(input: { name: "ada" }) }')
        assert result.data == {"signUp": "direct"}

    def test_a_required_argument_cannot_be_hidden(self, orm):
        @strawberry.type
        class Query:
            @strawberry.field
            def greet(
                self, name: Annotated[str, strawberry.argument(metadata=tags("a"))]
            ) -> str:
                return name

        with pytest.raises(TypeError, match=r"Query.greet\(name:\) is tagged a and"):
            orm.schema(query=Query, tags="b")

    def test_a_required_input_field_cannot_be_hidden(self, orm):
        @strawberry.input
        class Secretive:
            code: str = strawberry.field(metadata=tags("a"))

        @strawberry.type
        class Query:
            @strawberry.field
            def take(self, input: Secretive) -> bool:
                return True

        with pytest.raises(TypeError, match="Secretive.code is tagged a and has no"):
            orm.schema(query=Query, tags="b")

    def test_a_type_with_every_field_hidden_is_rejected(self, orm):
        @strawberry.type
        class Hidden:
            secret: str = strawberry.field(metadata=tags("a"))

        @strawberry.type
        class Query:
            hidden: Hidden

        with pytest.raises(TypeError, match="Every field of Hidden is tagged out"):
            orm.schema(query=Query, tags="b")


class TestSchemaClass:
    def test_the_schema_is_built_from_the_class_given(self, orm):
        class Custom(strawberry.Schema):
            def get_fields(self, type_definition):
                fields = super().get_fields(type_definition)
                return [f for f in fields if f.name != "greet"]

        untagged = orm.schema(query=Query, schema_class=Custom)
        tagged = orm.schema(query=Query, schema_class=Custom, tags="public")
        assert isinstance(untagged, Custom) and isinstance(tagged, Custom)
        assert "greet" not in str(untagged)
        assert "greet" not in str(tagged)
        assert "referrer" not in str(tagged)


class TestHelpers:
    def test_lazy_fields_take_tags_with_and_without_filters(self, orm):
        @orm.type(Post)
        class PostType:
            id: auto

        @orm.type(User)
        class UserType:
            id: auto

            @orm.field.lazy(tags="a")
            def first(self, info: strawberry.Info) -> list[PostType]:
                return self.posts

            @orm.field.lazy(tags="a", filters=orm.filter(Post))
            def filtered(self, info: strawberry.Info) -> list[PostType]:
                return self.posts

        assert _field(UserType, "first").metadata[TAGS_KEY] == {"a"}
        assert _field(UserType, "filtered").metadata[TAGS_KEY] == {"a"}

    def test_a_root_eager_field_and_a_connection_take_tags(self, orm):
        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]

        @strawberry.type
        class Query:
            users: list[UserNode] = orm.field.eager(tags="a")
            page: ORMListConnection[UserNode] = orm.connection(
                tags="a", metadata={"owner": "team"}
            )

        assert _field(Query, "users").metadata[TAGS_KEY] == {"a"}
        assert _field(Query, "page").metadata == {
            TAGS_KEY: frozenset({"a"}),
            "owner": "team",
        }

    def test_a_guarded_field_keeps_its_tags(self, orm):
        class Allow(strawberry.BasePermission):
            def has_permission(self, source, info, **kwargs):
                return True

        @orm.type(User)
        class UserType:
            id: auto
            email: auto = orm.field.eager(permission_classes=[Allow], tags="a")

        email = _field(UserType, "email")
        assert email.metadata[TAGS_KEY] == {"a"}
        assert email.permission_classes == [Allow]

    def test_a_field_that_sets_more_than_metadata_is_left_alone(self, orm):
        @orm.type(User)
        class UserType:
            id: auto
            name: auto = strawberry.field(description="Shown", metadata=tags("a"))

        name = _field(UserType, "name")
        assert name.description == "Shown"
        assert name.metadata[TAGS_KEY] == {"a"}

    def test_node_takes_tags(self, orm):
        assert orm.node(tags="a").metadata[TAGS_KEY] == {"a"}
        assert not orm.node().metadata

    def test_payload_fields_take_tags(self):
        @strawberry.type
        class Problem:
            message: str

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            payload=PayloadPolicy(errors=Problem, on_error=lambda exc: None),
        )

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]

        @orm.payload.query(tags="a")
        def me(info: strawberry.Info) -> UserNode:
            raise NotImplementedError

        @orm.payload.mutation(tags="a", metadata={"owner": "team"})
        def rename(info: strawberry.Info) -> UserNode:
            raise NotImplementedError

        @orm.payload.connection(tags="a")
        def people(info: strawberry.Info) -> Iterable[UserNode]:
            raise NotImplementedError

        assert me.metadata[TAGS_KEY] == {"a"}
        assert rename.metadata == {TAGS_KEY: frozenset({"a"}), "owner": "team"}
        assert people.metadata[TAGS_KEY] == {"a"}
