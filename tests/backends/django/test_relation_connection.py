"""A connection declared on a type is served by that parent's relation.

Built from the annotation alone it would query the whole table, because
``__set_name__`` runs before ``@orm.type`` knows what the parent is. The type
loop rebuilds it once the model is known.
"""

import base64

import pytest
import strawberry
from django.db import connection
from django.db.models.signals import post_init
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

    def test_a_single_parents_page_is_cut_like_the_window_cuts_it(
        self, seed, User, Post
    ):
        """Alone, the parent's rows are a plain query, so the page is a LIMIT.

        It has to answer exactly what the windowed read gives the same parent
        among siblings: the same edges, cursors, flags and total.
        """
        after = base64.b64encode(b"arrayconnection:0").decode()
        query = (
            f'{{ users {{ name posts(first: 1, after: "{after}") {{ totalCount '
            "edges { cursor node { title } } "
            "pageInfo { hasNextPage hasPreviousPage } } } }"
        )
        among_siblings = self._schema(_orm(), User, Post).execute_sync(
            query, context_value={}
        )
        User.objects.exclude(name="Alice").delete()
        with CaptureQueriesContext(connection) as ctx:
            alone = self._schema(_orm(), User, Post).execute_sync(
                query, context_value={}
            )

        assert alone.errors is None, alone.errors
        assert among_siblings.errors is None, among_siblings.errors
        windowed = next(u for u in among_siblings.data["users"] if u["name"] == "Alice")
        assert alone.data["users"] == [windowed]
        assert windowed["posts"]["totalCount"] == 2
        assert [e["node"]["title"] for e in windowed["posts"]["edges"]] == [
            "GraphQL Guide"
        ]
        reads = [
            q["sql"]
            for q in ctx.captured_queries
            if 'FROM "post"' in q["sql"] and "COUNT" not in q["sql"]
        ]
        assert len(reads) == 1 and "LIMIT" in reads[0], ctx.captured_queries

    def test_a_lazy_connection_pages_and_counts_each_parent(self, seed, User, Post):
        """Each parent's query is cut, and a page that holds the rest is the total."""
        from collections.abc import Iterable

        orm = _orm()

        @orm.type(Post)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

            @orm.connection.lazy(ORMListConnection[PostNode])
            def posts(self, info: strawberry.Info) -> Iterable[PostNode]:
                return Post.objects.filter(author_id=self.pk)

        @strawberry.type
        class Query:
            users: list[UserNode] = orm.field.eager()

        with CaptureQueriesContext(connection) as ctx:
            result = orm.schema(query=Query).execute_sync(
                "{ users { name posts(first: 5) { totalCount "
                "edges { node { title } } } } }",
                context_value={},
            )
        assert result.errors is None, result.errors
        assert {u["name"]: u["posts"]["totalCount"] for u in result.data["users"]} == {
            "Alice": 2,
            "Bob": 1,
            "Charlie": 1,
        }
        statements = [q["sql"] for q in ctx.captured_queries]
        assert len([sql for sql in statements if "LIMIT" in sql]) == 3, statements
        assert not [sql for sql in statements if "COUNT(" in sql], statements

    def _built(self, model, schema, query):
        """The result, and the *model* rows built to answer it."""
        built = []

        def keep(sender, instance, **kwargs):
            built.append(instance)

        post_init.connect(keep, sender=model, weak=False)
        try:
            result = schema.execute_sync(query, context_value={})
        finally:
            post_init.disconnect(keep, sender=model)
        return result, built

    def test_an_alias_gets_its_own_page(self, seed, User, Post):
        """Each field reads its own page, even where both reach the same row."""
        result = self._schema(_orm(), User, Post).execute_sync(
            "{ narrow: users { name posts(first: 5, "
            'filter: {field: {title: {contains: "Hello"}}}) '
            "{ edges { node { title } } } } "
            "everything: users { name posts { edges { node { title } } } } }",
            context_value={},
        )
        assert result.errors is None, result.errors
        alice = next(u for u in result.data["everything"] if u["name"] == "Alice")
        assert sorted(e["node"]["title"] for e in alice["posts"]["edges"]) == [
            "GraphQL Guide",
            "Hello World",
        ]

    def test_nothing_is_left_on_the_row(self, seed, User, Post):
        """A later read of the same rows must not be handed this page."""
        result, users = self._built(
            User,
            self._schema(_orm(), User, Post),
            "{ users { posts(first: 5) { edges { node { title } } } } }",
        )
        assert result.errors is None, result.errors
        assert users
        assert all(
            not k.startswith("_orm_connection_page") for u in users for k in vars(u)
        )

    def test_a_page_size_over_the_limit_reads_no_rows(self, seed, User, Post):
        result, posts = self._built(
            Post,
            self._schema(_orm(), User, Post),
            "{ users { posts(first: 1000) { edges { node { title } } } } }",
        )
        assert "cannot be higher than 100" in result.errors[0].message
        assert posts == [], f"read {len(posts)} posts before refusing the page size"

    def test_a_forged_cursor_reads_no_rows(self, seed, User, Post):
        forged = base64.b64encode(b"arrayconnection:1000000").decode()
        result, posts = self._built(
            Post,
            self._schema(_orm(), User, Post),
            f'{{ users {{ name posts(first: 2, after: "{forged}") '
            "{ edges { node { title } } } } }",
        )
        assert result.errors is None, result.errors
        assert all(u["posts"]["edges"] == [] for u in result.data["users"])
        assert posts == [], f"read {len(posts)} posts past the end of every page"

    def test_before_pages_as_strawberry_defines_it(self, seed, User, Post):
        """``first`` with ``before`` takes the first *n* rows that precede the cursor."""
        alice = User.objects.get(name="Alice")
        for i in range(6):
            Post.objects.create(
                title=f"Extra {i}", body="b", author=alice, is_published=True
            )
        before = base64.b64encode(b"arrayconnection:5").decode()
        result = self._schema(_orm(), User, Post).execute_sync(
            f'{{ users {{ name posts(first: 2, before: "{before}") '
            "{ edges { cursor } pageInfo { hasPreviousPage hasNextPage } } } }",
            context_value={},
        )
        assert result.errors is None, result.errors
        alice = next(u for u in result.data["users"] if u["name"] == "Alice")
        assert [e["cursor"] for e in alice["posts"]["edges"]] == [
            base64.b64encode(b"arrayconnection:0").decode(),
            base64.b64encode(b"arrayconnection:1").decode(),
        ]
        assert alice["posts"]["pageInfo"] == {
            "hasPreviousPage": False,
            "hasNextPage": True,
        }

    def _count(self, schema, query):
        with CaptureQueriesContext(connection) as ctx:
            self._by_user(schema, query)
        return len(ctx.captured_queries)

    def _counts(self, schema, query):
        result = schema.execute_sync(query, context_value={})
        assert result.errors is None, result.errors
        return {
            u["name"]: (
                u["posts"]["totalCount"],
                [e["node"]["title"] for e in u["posts"]["edges"]],
            )
            for u in result.data["users"]
        }

    def test_a_filter_narrows_each_parents_page(self, seed, User, Post):
        """The page is cut before the resolver runs, so the filter must reach it."""
        counts = self._counts(
            self._schema(_orm(), User, Post),
            "{ users { name posts(first: 5, "
            'filter: {field: {title: {contains: "Hello"}}}) '
            "{ totalCount edges { node { title } } } } }",
        )
        assert counts["Alice"] == (1, ["Hello World"])
        assert counts["Bob"] == (0, [])

    def test_a_filter_narrows_each_parents_total(self, seed, User, Post):
        """A total counted without the filter reports rows the caller excluded."""
        counts = self._counts(
            self._schema(_orm(), User, Post),
            "{ users { name posts(first: 1, "
            'filter: {field: {title: {contains: "Hello"}}}) '
            "{ totalCount edges { node { title } } } } }",
        )
        assert counts["Alice"][0] == 1, "the total ignored the filter"

    def test_an_order_applies_within_each_parents_page(self, seed, User, Post):
        by_desc = self._by_user(
            self._schema(_orm(), User, Post),
            "{ users { name posts(first: 5, order: {field: {title: DESC}}) "
            "{ edges { node { title } } } } }",
        )
        by_asc = self._by_user(
            self._schema(_orm(), User, Post),
            "{ users { name posts(first: 5, order: {field: {title: ASC}}) "
            "{ edges { node { title } } } } }",
        )
        assert by_desc["Alice"] == ["Hello World", "GraphQL Guide"]
        assert by_asc["Alice"] == ["GraphQL Guide", "Hello World"]

    def test_an_order_decides_which_rows_the_page_keeps(self, seed, User, Post):
        """Ordering has to reach the window, not just the rows it returned."""
        by_user = self._by_user(
            self._schema(_orm(), User, Post),
            "{ users { name posts(first: 1, order: {field: {title: ASC}}) "
            "{ edges { node { title } } } } }",
        )
        assert by_user["Alice"] == ["GraphQL Guide"]

    def test_a_filter_still_costs_one_query_per_shape(self, seed, User, Post):
        assert (
            self._count(
                self._schema(_orm(), User, Post),
                "{ users { name posts(first: 1, "
                'filter: {field: {title: {contains: "o"}}}) '
                "{ totalCount edges { node { title } } } } }",
            )
            == 3
        )

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
