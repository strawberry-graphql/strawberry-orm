"""One-to-one query shapes that exercise optimizer relation branches."""

import pytest
import strawberry
from django.db import connection, models

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


class RuntimeProfile(models.Model):
    bio = models.CharField(max_length=200)
    is_public = models.BooleanField(default=True)
    user = models.OneToOneField(
        DjUser, on_delete=models.CASCADE, related_name="profile"
    )

    class Meta:
        app_label = "testapp"
        db_table = "test_profile_runtime"


@pytest.fixture
def profile_model():
    existing_tables = connection.introspection.table_names()
    if RuntimeProfile._meta.db_table not in existing_tables:
        with connection.schema_editor() as editor:
            editor.create_model(RuntimeProfile)

    RuntimeProfile.objects.all().delete()
    RuntimeProfile.objects.bulk_create(
        [
            RuntimeProfile(user_id=1, bio="Alice Bio", is_public=True),
            RuntimeProfile(user_id=2, bio="Bob Bio", is_public=False),
            RuntimeProfile(user_id=3, bio="Charlie Bio", is_public=True),
        ]
    )
    yield RuntimeProfile


class TestQueryOneToOneRuntime:
    def test_root_users_can_select_reverse_one_to_one(self, seed, profile_model):
        orm = StrawberryORM.for_django()

        @orm.type(profile_model)
        class ProfileType:
            id: auto
            bio: auto

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto
            profile: ProfileType | None

            @strawberry.field
            def profile(self) -> ProfileType | None:
                try:
                    return type(self).profile.__get__(self, type(self))
                except profile_model.DoesNotExist:
                    return None

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.auto()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync("{ users { name profile { bio } } }")
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice", "profile": {"bio": "Alice Bio"}},
                {"name": "Bob", "profile": {"bio": "Bob Bio"}},
                {"name": "Charlie", "profile": {"bio": "Charlie Bio"}},
            ]
        }

    def test_prefetched_reverse_relations_can_select_nested_one_to_one(
        self, seed, profile_model
    ):
        orm = StrawberryORM.for_django()

        @orm.type(profile_model)
        class ProfileType:
            id: auto
            bio: auto

        @orm.type(DjUser)
        class AuthorType:
            id: auto
            name: auto
            profile: ProfileType | None

            @strawberry.field
            def profile(self) -> ProfileType | None:
                try:
                    return type(self).profile.__get__(self, type(self))
                except profile_model.DoesNotExist:
                    return None

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            author: AuthorType

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.auto()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title author { name profile { bio } } } } }"
        )
        assert result.errors is None
        assert result.data["users"][0]["posts"][0]["author"]["profile"] == {
            "bio": "Alice Bio"
        }
        assert result.data["users"][1]["posts"][0]["author"]["profile"] == {
            "bio": "Bob Bio"
        }

    def test_reverse_one_to_one_respects_custom_queryset(self, seed, profile_model):
        orm = StrawberryORM.for_django()

        @orm.type(profile_model)
        class ProfileType:
            id: auto
            bio: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(is_public=True)

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto
            profile: ProfileType | None

            @strawberry.field
            def profile(self) -> ProfileType | None:
                try:
                    return type(self).profile.__get__(self, type(self))
                except profile_model.DoesNotExist:
                    return None

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.auto()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync("{ users { name profile { bio } } }")
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice", "profile": {"bio": "Alice Bio"}},
                {"name": "Bob", "profile": None},
                {"name": "Charlie", "profile": {"bio": "Charlie Bio"}},
            ]
        }
