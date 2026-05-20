"""Abstract type generation tests: orm.type/filter/order/input/partial/ref."""

import strawberry

from strawberry_orm.types import auto


class AbstractTestQueryTypeGeneration:
    def test_type_returns_decorator(self, orm, User):
        decorator = orm.type(User)
        assert callable(decorator)

    def test_type_decorator_produces_strawberry_type(self, orm, User):
        @orm.type(User)
        class UserType:
            id: auto
            name: auto

        assert hasattr(UserType, "__strawberry_definition__")

    def test_auto_resolves_to_python_types(self, orm, User):
        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            email: auto

        definition = UserType.__strawberry_definition__
        field_map = {f.name: f for f in definition.fields}
        assert "id" in field_map
        assert "name" in field_map
        assert "email" in field_map

    def test_type_stores_orm_model(self, orm, User):
        @orm.type(User)
        class UserType:
            id: auto
            name: auto

        assert UserType.__orm_model__ is User

    def test_filter_is_callable(self, orm, User):
        result = orm.filter(User)
        assert callable(result)

    def test_order_is_callable(self, orm, User):
        result = orm.order(User)
        assert callable(result)

    def test_input_is_callable(self, orm, User):
        result = orm.input(User)
        assert callable(result)

    def test_optimizer_extension_is_a_class(self, orm):
        ext = orm.optimizer_extension()
        assert isinstance(ext, type)

    def test_ref_update_only(self, orm, Tag):
        TagRef = orm.ref(Tag)
        definition = TagRef.__strawberry_definition__
        field_names = [f.name for f in definition.fields]
        assert "update" in field_names
        assert "id" not in field_names

    def test_ref_with_create(self, orm, Tag):
        @strawberry.input
        class CreateTagInput:
            name: str

        TagRef = orm.ref(Tag, create=CreateTagInput)
        definition = TagRef.__strawberry_definition__
        field_names = [f.name for f in definition.fields]
        assert "update" in field_names
        assert "create" in field_names

    def test_ref_with_unlink_and_delete(self, orm, Tag):
        TagRef = orm.ref(Tag, unlink=True, delete=True)
        definition = TagRef.__strawberry_definition__
        field_names = [f.name for f in definition.fields]
        assert "update" in field_names
        assert "unlink" in field_names
        assert "delete" in field_names


class AbstractTestQueryInputGeneration:
    def test_input_produces_strawberry_input(self, orm, User):
        UserInput = orm.input(User)
        assert hasattr(UserInput, "__strawberry_definition__")

    def test_input_fields_are_all_optional(self, orm, User):
        UserInput = orm.input(User)
        definition = UserInput.__strawberry_definition__
        for field in definition.fields:
            assert field.default is strawberry.UNSET, (
                f"Field {field.name} should default to UNSET"
            )

    def test_input_excludes_relationships(self, orm, User):
        UserInput = orm.input(User)
        definition = UserInput.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "posts" not in field_names
        assert "comments" not in field_names

    def test_input_includes_scalar_columns(self, orm, User):
        UserInput = orm.input(User)
        definition = UserInput.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "name" in field_names
        assert "email" in field_names


class AbstractTestQueryPartialGeneration:
    def test_partial_produces_strawberry_input(self, orm, User):
        UserPartial = orm.partial(User)
        assert hasattr(UserPartial, "__strawberry_definition__")

    def test_partial_has_auto_name(self, orm, User):
        UserPartial = orm.partial(User)
        definition = UserPartial.__strawberry_definition__
        assert "Partial" in definition.name or "partial" in definition.name.lower()

    def test_partial_fields_are_optional(self, orm, User):
        UserPartial = orm.partial(User)
        definition = UserPartial.__strawberry_definition__
        for field in definition.fields:
            assert field.default is strawberry.UNSET


class AbstractTestQueryIncludeExclude:
    def test_type_include(self, orm, User):
        @orm.type(User, include=["id", "name"])
        class UserLimited:
            id: auto
            name: auto

        definition = UserLimited.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "id" in field_names
        assert "name" in field_names

    def test_type_exclude(self, orm, User):
        @orm.type(User, exclude=["email", "created_at"])
        class UserNoEmail:
            id: auto
            name: auto

        definition = UserNoEmail.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "id" in field_names
        assert "name" in field_names

    def test_filter_include(self, orm, User):
        UserFilter = orm.filter(User, include=["id", "name"])
        field_type = UserFilter._field_type
        definition = field_type.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "id" in field_names
        assert "name" in field_names
        assert "email" not in field_names

    def test_filter_exclude(self, orm, User):
        UserFilter = orm.filter(User, exclude=["email", "created_at"])
        field_type = UserFilter._field_type
        definition = field_type.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "id" in field_names
        assert "name" in field_names
        assert "email" not in field_names

    def test_order_include(self, orm, User):
        UserOrder = orm.order(User, include=["name"])
        field_type = UserOrder._field_type
        definition = field_type.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "name" in field_names
        assert "email" not in field_names

    def test_order_exclude(self, orm, User):
        UserOrder = orm.order(User, exclude=["email", "created_at"])
        field_type = UserOrder._field_type
        definition = field_type.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "name" in field_names
        assert "email" not in field_names

    def test_input_include(self, orm, User):
        UserInput = orm.input(User, include=["name"])
        definition = UserInput.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "name" in field_names
        assert "email" not in field_names

    def test_input_exclude(self, orm, User):
        UserInput = orm.input(User, exclude=["email", "created_at"])
        definition = UserInput.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "name" in field_names
        assert "email" not in field_names


class AbstractTestQueryCustomName:
    def test_type_custom_name(self, orm, User):
        @orm.type(User, name="PersonType")
        class UT:
            id: auto
            name: auto

        definition = UT.__strawberry_definition__
        assert definition.name == "PersonType"

    def test_input_custom_name(self, orm, User):
        UserInput = orm.input(User, name="PersonInput")
        definition = UserInput.__strawberry_definition__
        assert definition.name == "PersonInput"

    def test_filter_object_type_name(self, orm, User, Post):
        orm.filter(User)
        PostFilter = orm.filter(Post)
        assert hasattr(PostFilter, "_object_type")
        definition = PostFilter._object_type.__strawberry_definition__
        assert definition.name == "PostFilterObject"

    def test_type_name_model_object_does_not_collide_with_filter(self, orm, User, Post):
        UserFilter = orm.filter(User)
        PostFilter = orm.filter(Post)

        @orm.type(User, name="UserObject", filters=UserFilter)
        class UserObjectType:
            id: auto
            name: auto

        @orm.type(Post, name="PostObject", filters=PostFilter)
        class PostObjectType:
            id: auto
            title: auto
            author: UserObjectType

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self) -> list[PostObjectType]:
                return []

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        assert UserObjectType.__strawberry_definition__.name == "UserObject"
        assert PostObjectType.__strawberry_definition__.name == "PostObject"
        assert PostFilter._object_type.__strawberry_definition__.name == "PostFilterObject"
