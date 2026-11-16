"""Rows the app keeps between requests, loaded onto by two requests at once.

Each request loads its relations onto the same instances, so one load's caches
can be replaced by the other's before it marks them. A load marks only caches
whose rows it read itself, and a mark stops holding once the relation holds
something else.

Seed: Bob's "Draft Post" is the only draft.
"""

import django.db.models
import strawberry

from strawberry_orm import StrawberryORM, auto
from strawberry_orm.backends.django import _held
from strawberry_orm.lazy_resolution import (
    execution_scope,
    mark_relations_scoped,
    relation_cache_is_scoped,
)
from tests.backends.django.models import Post, User

QUERY = "{ users { name posts { title } } }"


def _schema(cached):
    orm = StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)

    @orm.type(Post)
    class PostType:
        title: auto

        @classmethod
        def scope_rows(cls, qs, info):
            if info.context["role"] == "admin":
                return qs
            return qs.filter(is_published=True)

    @orm.type(User)
    class UserType:
        name: auto
        posts: list[PostType]

    @strawberry.type
    class Query:
        @strawberry.field
        def users(self) -> list[UserType]:
            return cached

    return orm.schema(query=Query)


def _titles(result):
    assert result.errors is None, result.errors
    return sorted(
        post["title"] for user in result.data["users"] for post in user["posts"]
    )


class TestSharedRows:
    def test_a_load_crossed_by_another_request_marks_only_its_own_rows(
        self, seed, monkeypatch
    ):
        cached = list(User.objects.order_by("id"))
        schema = _schema(cached)
        real = django.db.models.prefetch_related_objects
        admin = []

        def interleaved(rows, *lookups):
            real(rows, *lookups)
            if not admin:
                # The admin's request loads onto the same rows, replacing the
                # guest's caches, before the guest's load marks them.
                admin.append(None)
                admin[0] = schema.execute_sync(QUERY, context_value={"role": "admin"})

        monkeypatch.setattr(django.db.models, "prefetch_related_objects", interleaved)
        guest = schema.execute_sync(QUERY, context_value={"role": "guest"})

        assert "Draft Post" in _titles(admin[0])
        assert _titles(guest) == ["GraphQL Guide", "Hello World", "Rust Adventures"]

    def test_a_mark_stops_holding_once_the_relation_is_replaced(self, seed):
        user = User.objects.get(name="Bob")
        with execution_scope():
            django.db.models.prefetch_related_objects([user], "posts")
            mark_relations_scoped([user], ["posts"], held=_held)
            assert relation_cache_is_scoped(user, "posts", held=_held)

            user._prefetched_objects_cache.pop("posts")
            django.db.models.prefetch_related_objects([user], "posts")
            assert not relation_cache_is_scoped(user, "posts", held=_held)
