"""What one execution learned about scoping is not trusted by the next.

The library remembers that a query, a row or a relation cache was already
scoped, so it does not check it again. That holds for the request that scoped
it and no other: a statement kept at module level, rows cached between
requests and an identity map shared by several requests all outlive it, and an
admin's request says nothing about what the next user may read.

A backend provides ``orm`` (a fresh ORM) and ``models`` (``User``, ``Post``),
and on the test class:

- ``run(schema, query, context)``, executing with *context* (a dict) against
  the seeded rows;
- ``subscribe(schema, query, context)``, every answer of a subscription run
  with *context* (a dict) against the seeded rows;
- ``published_only(qs)``, keeping the published posts;
- ``all_posts()``, an unexecuted query over every post, in id order;
- ``posts_of(user)``, an unexecuted query over the user's posts, in id order;
- ``load_posts()`` and ``load_users()``, every post or user, read before any
  execution, in id order.

Seed: Bob wrote the only draft, "Draft Post".
"""

from types import SimpleNamespace

import strawberry
from strawberry import relay

from strawberry_orm import auto
from strawberry_orm.lazy_resolution import current_execution, query_is_scoped
from strawberry_orm.relay import ORMListConnection

ALL_TITLES = ["Hello World", "GraphQL Guide", "Draft Post", "Rust Adventures"]
PUBLISHED_TITLES = ["Hello World", "GraphQL Guide", "Rust Adventures"]


def _titles(data, field):
    """Every title the users' *field* returned, page or list, in seed order."""
    found = []
    for user in data["users"]:
        value = user[field]
        if isinstance(value, dict):
            value = [edge["node"] for edge in value["edges"]]
        found.extend(post["title"] for post in value)
    return sorted(found, key=ALL_TITLES.index)


class _Trust:
    async def execute(self, schema, query, **context):
        result = await self.run(schema, query, context)
        assert result.errors is None, result.errors
        return result.data

    def post_type(self, orm, models, **fields):
        published_only = self.published_only

        @classmethod
        def scope_rows(cls, qs, info):
            if info.context.get("admin"):
                return qs
            return published_only(qs)

        namespace = {
            "__annotations__": {"id": auto, "title": auto, **fields},
            "scope_rows": scope_rows,
        }
        return orm.type(models.Post)(type("PostType", (), namespace))

    def event_parts(self, orm, models):
        """A post type that checks no row itself, and a field scope keeping
        the drafts for an admin: the field's read is all that hides them."""
        published_only = self.published_only

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

        def admin_or_published(qs, info):
            return qs if info.context.get("admin") else published_only(qs)

        return PostType, admin_or_published

    async def _each_event_rescoped(self, orm, UserType, field):
        """A subscription hands back the same users twice, to an admin and then
        to a caller who is not: what the first event read is not the second's."""
        cached = await self.load_users()

        @strawberry.type
        class Query:
            @strawberry.field
            def ok(self) -> bool:
                return True

        @strawberry.type
        class Subscription:
            @strawberry.subscription
            async def users(self, info: strawberry.Info) -> list[UserType]:
                for admin in (True, False):
                    info.context["admin"] = admin
                    yield cached

        schema = orm.schema(query=Query, subscription=Subscription)
        selection = f"{field} {{ title }}"
        if field == "posts":
            selection = "posts { edges { node { title } } }"
        answers = await self.subscribe(
            schema, f"subscription {{ users {{ name {selection} }} }}", {}
        )
        assert [answer.errors for answer in answers] == [None, None]
        assert [_titles(answer.data, field) for answer in answers] == [
            ALL_TITLES,
            PUBLISHED_TITLES,
        ]

    async def _as_admin_then_not(self, schema, query, read):
        for admin, expected in (
            (False, PUBLISHED_TITLES),
            (True, ALL_TITLES),
            (False, PUBLISHED_TITLES),
        ):
            data = await self.execute(schema, query, admin=admin)
            assert read(data) == expected, (admin, data)


class AbstractTestTrustAcrossExecutions(_Trust):
    async def test_a_shared_query_an_admin_read_is_scoped_for_the_next_user(
        self, orm, models
    ):
        PostType = self.post_type(orm, models)
        shared = self.all_posts()

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self) -> list[PostType]:
                return shared

        await self._as_admin_then_not(
            orm.schema(query=Query),
            "{ posts { title } }",
            lambda data: [post["title"] for post in data["posts"]],
        )

    async def test_a_scope_that_keeps_everything_marks_nothing(self, orm, models):
        """With no execution open a mark has nothing to expire with, so the
        caller's own query, which the scope handed back, is never marked."""
        PostType = self.post_type(orm, models)

        @orm.type(models.User)
        class UserType:
            id: auto
            posts: list[PostType]

        backend = orm._backend
        admin = SimpleNamespace(context={"admin": True})
        shared = self.all_posts()
        assert backend.apply_type_scope(shared, models.Post, admin) is shared
        restrict = backend.relation_scope(models.User, "posts", admin)
        assert restrict(shared, admin) is shared
        assert not query_is_scoped(shared)

        narrowed = backend.apply_type_scope(
            shared, models.Post, SimpleNamespace(context={})
        )
        assert narrowed is not shared
        assert query_is_scoped(narrowed)

    async def test_rows_an_earlier_execution_checked_are_checked_again(
        self, orm, models
    ):
        """Loading the authors marks each row as prepared, which skips its
        scope check, and checking it marks it as scoped, which does too."""

        @orm.type(models.User)
        class AuthorType:
            id: auto
            name: auto

        PostType = self.post_type(orm, models, author=AuthorType | None)
        cached = await self.load_posts()

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self) -> list[PostType]:
                return cached

        await self._as_admin_then_not(
            orm.schema(query=Query),
            "{ posts { title author { name } } }",
            lambda data: [post["title"] for post in data["posts"]],
        )

    async def test_a_relation_an_earlier_execution_loaded_is_scoped_again(
        self, orm, models
    ):
        PostType = self.post_type(orm, models)
        cached = await self.load_users()

        @orm.type(models.User)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self) -> list[UserType]:
                return cached

        await self._as_admin_then_not(
            orm.schema(query=Query),
            "{ users { name posts { title } } }",
            lambda data: sorted(
                (post["title"] for user in data["users"] for post in user["posts"]),
                key=ALL_TITLES.index,
            ),
        )

    async def test_connection_states_stay_off_a_shared_context(self, orm, models):
        @orm.type(models.User, group=orm.group(models.User))
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection()

        schema = orm.schema(query=Query)
        query = (
            "{ users(first: 10, groupBy: [{ field: { name: true } }]) "
            "{ groups { key { name } aggregates { count } } } }"
        )
        shared: dict = {}
        for _ in range(2):
            data = await self.run(schema, query, shared)
            assert data.errors is None, data.errors
            assert {
                group["key"]["name"]: group["aggregates"]["count"]
                for group in data.data["users"]["groups"]
            } == {"Alice": 1, "Bob": 1, "Charlie": 1}
            # Another execution sharing the context could read them.
            assert "_orm_connections" not in shared

    async def test_each_subscription_event_is_an_execution_of_its_own(
        self, orm, models
    ):
        cached = await self.load_users()
        seen = []

        @orm.type(models.User)
        class UserType:
            id: auto
            name: auto

            @strawberry.field
            def execution(self) -> bool:
                seen.append(current_execution())
                return True

        @strawberry.type
        class Query:
            @strawberry.field
            def ok(self) -> bool:
                return True

        @strawberry.type
        class Subscription:
            @strawberry.subscription
            async def users(self) -> list[UserType]:
                yield cached[:1]
                yield cached[1:2]

        schema = orm.schema(query=Query, subscription=Subscription)
        events = await schema.subscribe("subscription { users { name execution } }")
        answers = [event async for event in events]
        assert [answer.errors for answer in answers] == [None, None]
        assert [answer.data["users"][0]["name"] for answer in answers] == [
            "Alice",
            "Bob",
        ]
        first, second = seen
        assert first is not None and second is not None
        assert first is not second

    async def test_a_batch_one_event_read_is_not_served_to_the_next(self, orm, models):
        PostType, admin_or_published = self.event_parts(orm, models)
        posts_of = self.posts_of

        @orm.type(models.User)
        class UserType:
            id: auto
            name: auto

            @orm.field.lazy
            def writing(self, info: strawberry.Info) -> list[PostType]:
                return admin_or_published(posts_of(self), info)

        await self._each_event_rescoped(orm, UserType, "writing")

    async def test_a_page_one_event_cut_is_not_served_to_the_next(self, orm, models):
        PostType, admin_or_published = self.event_parts(orm, models)

        @orm.type(models.User)
        class UserType:
            id: auto
            name: auto
            posts: ORMListConnection[PostType] = orm.connection.eager(
                scope=admin_or_published
            )

        await self._each_event_rescoped(orm, UserType, "posts")


class AbstractTestOnViewsAcrossExecutions(_Trust):
    async def test_a_view_one_event_read_is_not_served_to_the_next(self, orm, models):
        PostType, admin_or_published = self.event_parts(orm, models)

        @orm.type(models.User)
        class UserType:
            id: auto
            name: auto
            recent: list[PostType] = orm.field.eager(
                on="posts", scope=admin_or_published
            )

        await self._each_event_rescoped(orm, UserType, "recent")
