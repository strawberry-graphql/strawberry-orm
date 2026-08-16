"""A connection declared on a type is served by that parent's relation.

Built from the annotation alone it would query the whole table, because
``__set_name__`` runs before ``@orm.type`` knows what the parent is. The type
loop rebuilds it once the model is known.
"""

import pytest
import strawberry
from django.db import connection
from django.test.utils import CaptureQueriesContext
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


def _orm():
    return StrawberryORM.for_django(warn_missing_scope=False, lazy_resolution="off")


@pytest.mark.django_db
class TestRelationConnection:
    def _schema(self, orm, User, Post, *, scope_drafts=False):
        @orm.type(Post, filters=orm.filter(Post), order=orm.order(Post))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

            if scope_drafts:

                @classmethod
                def scope_rows(cls, qs, info):
                    return qs.filter(is_published=True)

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        @strawberry.type
        class Query:
            users: list[UserNode] = orm.field.eager()

        return orm.schema(query=Query)

    def _by_user(self, schema, query):
        result = schema.execute_sync(query, context_value={})
        assert result.errors is None, result.errors
        return {
            u["name"]: [e["node"]["title"] for e in u["posts"]["edges"]]
            for u in result.data["users"]
        }

    def test_each_parent_sees_only_its_own_rows(self, seed, User, Post):
        by_user = self._by_user(
            self._schema(_orm(), User, Post),
            "{ users { name posts(first: 10) { edges { node { title } } } } }",
        )
        assert by_user["Bob"] == ["Draft Post"]
        assert "Draft Post" not in by_user["Alice"]

    def test_the_related_types_scope_still_applies(self, seed, User, Post):
        """A connection must not be a way around scope_rows."""
        by_user = self._by_user(
            self._schema(_orm(), User, Post, scope_drafts=True),
            "{ users { name posts(first: 10) { edges { node { title } } } } }",
        )
        assert all("Draft Post" not in titles for titles in by_user.values())

    def test_pagination_is_per_parent(self, seed, User, Post):
        by_user = self._by_user(
            self._schema(_orm(), User, Post),
            "{ users { name posts(first: 1) { edges { node { title } } } } }",
        )
        assert all(len(titles) <= 1 for titles in by_user.values())
        assert any(titles for titles in by_user.values())

    def test_total_count_is_per_parent(self, seed, User, Post):
        schema = self._schema(_orm(), User, Post)
        result = schema.execute_sync(
            "{ users { name posts(first: 1) { totalCount } } }", context_value={}
        )
        assert result.errors is None, result.errors
        counts = {u["name"]: u["posts"]["totalCount"] for u in result.data["users"]}
        assert counts["Bob"] == 1, counts
        assert counts["Alice"] >= 2, counts

    def test_a_cursor_pages_within_each_parent(self, seed, User, Post):
        """after= has to skip within a parent's own page, not the whole window."""
        schema = self._schema(_orm(), User, Post)
        first = schema.execute_sync(
            "{ users { name posts(first: 1) { edges { cursor node { title } } } } }",
            context_value={},
        )
        assert first.errors is None, first.errors
        alice = next(u for u in first.data["users"] if u["name"] == "Alice")
        cursor = alice["posts"]["edges"][0]["cursor"]
        page_one = alice["posts"]["edges"][0]["node"]["title"]

        second = schema.execute_sync(
            f'{{ users {{ name posts(first: 1, after: "{cursor}") '
            "{ edges { node { title } } } } }",
            context_value={},
        )
        assert second.errors is None, second.errors
        alice2 = next(u for u in second.data["users"] if u["name"] == "Alice")
        assert [e["node"]["title"] for e in alice2["posts"]["edges"]] != [page_one]

    def test_without_a_page_size_there_is_nothing_to_window(self, seed, User, Post):
        """No first= means no per-parent limit, so the plain path is the answer."""
        by_user = self._by_user(
            self._schema(_orm(), User, Post),
            "{ users { name posts { edges { node { title } } } } }",
        )
        assert by_user["Bob"] == ["Draft Post"]

    def test_a_single_parent_needs_no_window(self, seed, User, Post):
        """With nothing to batch across, the plain per-parent read is correct."""
        User.objects.exclude(name="Bob").delete()
        by_user = self._by_user(
            self._schema(_orm(), User, Post),
            "{ users { name posts(first: 5) { edges { node { title } } } } }",
        )
        assert by_user == {"Bob": ["Draft Post"]}

    def _count(self, schema, query):
        with CaptureQueriesContext(connection) as ctx:
            self._by_user(schema, query)
        return len(ctx.captured_queries)

    PAGE = "{ users { name posts(first: 1) { edges { node { title } } } } }"

    def test_a_page_costs_the_same_whether_or_not_it_is_scoped(self, seed, User, Post):
        """Root, one windowed query for every parent's page, one for the totals."""
        assert self._count(self._schema(_orm(), User, Post), self.PAGE) == 3
        scoped = self._schema(_orm(), User, Post, scope_drafts=True)
        assert self._count(scoped, self.PAGE) == 3

    def test_the_cost_does_not_grow_with_the_number_of_parents(
        self, seed, User, Post, django_assert_num_queries
    ):
        """The point of the window: statements track the shape, not the rows."""
        schema = self._schema(_orm(), User, Post)
        before = self._count(schema, self.PAGE)
        for i in range(5):
            user = User.objects.create(name=f"Extra {i}", email=f"e{i}@x.com")
            Post.objects.create(title=f"P{i}", author=user, is_published=True)
        assert self._count(schema, self.PAGE) == before

    def test_a_connection_over_a_many_to_many_is_refused(self, seed, User, Post, Tag):
        """Those rows carry no parent column, so a window cannot partition them."""
        orm = _orm()

        @orm.type(Tag)
        class TagNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        with pytest.raises(ValueError, match="no column tying them to a parent"):

            @orm.type(Post)
            class PostNode(relay.Node):
                id: relay.NodeID[int]
                tags: ORMListConnection[TagNode] = orm.connection.eager()

    def test_total_count_exceeds_the_page(self, seed, User, Post):
        """Counting the page would report the page size, not the parent's total."""
        alice = User.objects.get(name="Alice")
        for i in range(4):
            Post.objects.create(title=f"Extra {i}", author=alice, is_published=True)

        result = self._schema(_orm(), User, Post).execute_sync(
            "{ users { name posts(first: 1) { totalCount edges { node { title } } } } }",
            context_value={},
        )
        assert result.errors is None, result.errors
        row = next(u for u in result.data["users"] if u["name"] == "Alice")
        assert len(row["posts"]["edges"]) == 1
        assert row["posts"]["totalCount"] >= 5, row["posts"]["totalCount"]
