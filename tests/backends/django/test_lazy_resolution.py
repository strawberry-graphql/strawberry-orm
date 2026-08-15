"""Lazy relation resolution guardrails."""

import warnings

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


class TestLazyResolutionGuardrails:
    def test_default_lazy_resolution_warns_at_type_definition(self, User, Tag):
        orm = StrawberryORM.for_django()

        @orm.type(Tag)
        class TagType:
            id: auto
            name: auto

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            favorite_tag: TagType

        with pytest.warns(UserWarning, match="favorite_tag"):
            orm.type(User)(UserType)

    def test_warns_by_default_for_unresolved_relation_field(self, User, Tag):
        orm = StrawberryORM.for_django(lazy_resolution="warn")

        @orm.type(Tag)
        class TagType:
            id: auto
            name: auto

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            favorite_tag: TagType

        with pytest.warns(UserWarning, match="favorite_tag"):
            orm.type(User)(UserType)

    def test_off_suppresses_warnings(self, User, Tag):
        orm = StrawberryORM.for_django(lazy_resolution="off")

        @orm.type(Tag)
        class TagType:
            id: auto

        @orm.type(User)
        class UserType:
            id: auto
            favorite_tag: TagType

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            orm.type(User)(UserType)
        assert not any("favorite_tag" in str(w.message) for w in caught)

    def test_error_mode_raises(self, User, Tag):
        orm = StrawberryORM.for_django(lazy_resolution="error")

        @orm.type(Tag)
        class TagType:
            id: auto

        with pytest.raises(ValueError, match="favorite_tag"):

            @orm.type(User)
            class UserType:
                id: auto
                favorite_tag: TagType

    def test_invalid_lazy_resolution_raises(self):
        with pytest.raises(ValueError, match="lazy_resolution must be one of"):
            StrawberryORM.for_django(lazy_resolution="invalid")  # type: ignore[arg-type]

    def test_explicit_resolver_suppresses_warning(self, User, Post):
        orm = StrawberryORM.for_django(lazy_resolution="warn")

        @orm.type(User)
        class UserType:
            id: auto
            name: auto

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

            @strawberry.field
            def author(self) -> UserType:
                return self.author  # type: ignore[return-value]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            orm.type(Post)(PostType)
        assert not any("author" in str(w.message) for w in caught)


class TestLazyResolutionRuntime:
    def test_fk_not_cached_before_access(self, seed):
        post = DjPost.objects.first()
        assert post is not None
        author_field = post._meta.get_field("author")
        assert not author_field.is_cached(post)

    def _build_posts_schema(self, orm: StrawberryORM, *, extensions: list):
        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            author: UserType

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self) -> list[PostType]:
                return list(DjPost.objects.all())  # type: ignore[return-value]

        return strawberry.Schema(query=Query, extensions=extensions)

    def test_runtime_lazy_fk_warns_with_extension(self, seed):
        orm = StrawberryORM.for_django(lazy_resolution="off")
        schema = self._build_posts_schema(
            orm,
            extensions=[orm.lazy_resolution_extension(mode="warn")],
        )

        with pytest.warns(UserWarning, match="path: query \\{ posts \\{ author"):
            result = schema.execute_sync("{ posts { author { name } } }")

        assert result.errors is None
        assert len(result.data["posts"]) == 4

    def test_runtime_lazy_fk_errors_with_extension(self, seed):
        orm = StrawberryORM.for_django(lazy_resolution="off")
        schema = self._build_posts_schema(
            orm,
            extensions=[orm.lazy_resolution_extension(mode="error")],
        )

        result = schema.execute_sync("{ posts { author { name } } }")
        assert result.errors is not None
        assert "path: query { posts { author { name } } }" in str(
            result.errors[0].message
        )
        assert "fix: return a QuerySet instead of list" in str(result.errors[0].message)

    def test_runtime_no_warning_when_optimizer_prefetches(self, seed):
        orm = StrawberryORM.for_django(lazy_resolution="off")

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            author: UserType

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field.auto()

        schema = strawberry.Schema(
            query=Query,
            extensions=[
                orm.optimizer_extension(),
                orm.lazy_resolution_extension(mode="warn"),
            ],
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=UserWarning)
            result = schema.execute_sync("{ posts { author { name } } }")

        assert result.errors is None
        lazy_warnings = [
            w
            for w in caught
            if issubclass(w.category, UserWarning)
            and "Unoptimized relation loads detected" in str(w.message)
        ]
        assert lazy_warnings == []
