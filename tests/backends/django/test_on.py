"""``on=`` serves a field from a relation under another name.

Before it existed the GraphQL field had to be called exactly what the relation
was called, so a relation could back only one field. Two filtered views of one
relation had nowhere to live except a per-row resolver.
"""

import pytest
import strawberry
from django.test.utils import CaptureQueriesContext

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


def _orm():
    return StrawberryORM.for_django(warn_missing_scope=False, lazy_resolution="off")


@pytest.mark.django_db
class TestVia:
    def _schema(self, orm, User, Post):
        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            published: list[PostType] = orm.field.eager(
                on="posts", scope=lambda qs, info: qs.filter(is_published=True)
            )
            drafts: list[PostType] = orm.field.eager(
                on="posts", scope=lambda qs, info: qs.filter(is_published=False)
            )

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.eager()

        return orm.schema(query=Query)

    QUERY = "{ users { name published { title } drafts { title } } }"

    def _run(self, schema):
        result = schema.execute_sync(self.QUERY, context_value={})
        assert result.errors is None, result.errors
        return {
            u["name"]: (
                sorted(p["title"] for p in u["published"]),
                sorted(p["title"] for p in u["drafts"]),
            )
            for u in result.data["users"]
        }

    def test_one_relation_backs_two_filtered_views(self, seed, User, Post):
        by_user = self._run(self._schema(_orm(), User, Post))

        published, drafts = by_user["Alice"]
        assert "Hello World" in published
        assert "Draft Post" not in published
        assert published != drafts, "both views returned the same rows"

    def test_each_view_keeps_its_own_scope(self, seed, User, Post):
        by_user = self._run(self._schema(_orm(), User, Post))

        all_published = {t for pub, _ in by_user.values() for t in pub}
        all_drafts = {t for _, dr in by_user.values() for t in dr}
        assert not (all_published & all_drafts), (
            "a post appeared under both views, so one scope leaked into the other"
        )

    def test_both_views_load_without_a_query_per_row(self, seed, User, Post):
        """Two prefetches for two views, not one per user per view."""
        from django.db import connection

        schema = self._schema(_orm(), User, Post)
        with CaptureQueriesContext(connection) as ctx:
            self._run(schema)
        assert len(ctx.captured_queries) == 3, [q["sql"] for q in ctx.captured_queries]

    def test_on_naming_a_relation_that_does_not_exist_is_rejected(self, User, Post):
        orm = _orm()

        @orm.type(Post)
        class PostType:
            id: auto

        with pytest.raises(ValueError, match="has no relation 'postz'"):

            @orm.type(User)
            class UserType:
                id: auto
                published: list[PostType] = orm.field.eager(on="postz")

    def test_the_scope_holds_without_the_optimizer(self, seed, User, Post):
        """The prefetch carries the scope; with no prefetch it must still apply."""
        import strawberry as sb

        orm = _orm()

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            published: list[PostType] = orm.field.eager(
                on="posts", scope=lambda qs, info: qs.filter(is_published=True)
            )

        @sb.type
        class Query:
            @sb.field
            def users(self) -> list[UserType]:
                return User.objects.all()

        # No optimizer extension, so nothing prefetches under to_attr.
        result = sb.Schema(query=Query).execute_sync(
            "{ users { name published { title } } }", context_value={}
        )
        assert result.errors is None, result.errors
        titles = {p["title"] for u in result.data["users"] for p in u["published"]}
        assert "Draft Post" not in titles, (
            "unscoped rows leaked when the optimizer was not running"
        )
