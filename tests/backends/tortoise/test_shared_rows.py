"""Rows the app keeps between requests, loaded onto by two requests at once.

Each request loads its relations onto the same instances, so one load's caches
can be replaced by the other's before it marks them. A load that crossed
another marks nothing, and a mark stops holding once the relation holds
something else.

Seed: Bob's "Draft Post" is the only draft.
"""

import strawberry

from strawberry_orm import StrawberryORM, auto
from strawberry_orm.backends import tortoise as tortoise_backend
from strawberry_orm.lazy_resolution import (
    execution_scope,
    mark_relations_scoped,
    relation_cache_is_scoped,
)
from tests.backends.tortoise.models import Post, User

QUERY = "{ users { name posts { title } } }"


def _schema(cached):
    orm = StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)

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
        async def users(self) -> list[UserType]:
            return cached

    return orm.schema(query=Query)


def _titles(result):
    assert result.errors is None, result.errors
    return sorted(
        post["title"] for user in result.data["users"] for post in user["posts"]
    )


class TestSharedRows:
    async def test_a_load_crossed_by_another_request_marks_nothing(
        self, seed, monkeypatch
    ):
        cached = list(await User.all().order_by("id"))
        schema = _schema(cached)
        real = tortoise_backend._prefetch_onto
        admin = []

        async def interleaved(model, rows, lookups):
            await real(model, rows, lookups)
            if not admin:
                # The admin's request loads onto the same rows, replacing the
                # guest's caches, before the guest's load marks them.
                admin.append(None)
                admin[0] = await schema.execute(QUERY, context_value={"role": "admin"})

        monkeypatch.setattr(tortoise_backend, "_prefetch_onto", interleaved)
        guest = await schema.execute(QUERY, context_value={"role": "guest"})

        assert "Draft Post" in _titles(admin[0])
        assert _titles(guest) == ["GraphQL Guide", "Hello World", "Rust Adventures"]

    async def test_a_mark_stops_holding_once_the_relation_is_replaced(self, seed):
        user = await User.get(name="Bob")
        with execution_scope():
            await tortoise_backend._prefetch_onto(User, [user], ["posts"])
            mark_relations_scoped([user], ["posts"], held=tortoise_backend._held)
            assert relation_cache_is_scoped(user, "posts", held=tortoise_backend._held)

            await tortoise_backend._prefetch_onto(User, [user], ["posts"])
            assert not relation_cache_is_scoped(
                user, "posts", held=tortoise_backend._held
            )
