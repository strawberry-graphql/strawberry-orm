"""A connection declared on a type is served by that parent's relation.

Built from the annotation alone it would query the whole table, because
``__set_name__`` runs before ``@orm.type`` knows what the parent is. The type
loop rebuilds it once the model is known, and the pages are cut with one
windowed query rather than one query per parent.

Tortoise has no window expression, so the backend wraps the query it built in
SQL that numbers rows within each parent; these tests hold that wrapping to the
same behaviour the other backends get from their ORM.
"""

from contextlib import contextmanager

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.backends.tortoise.models import Post as TPost
from tests.backends.tortoise.models import Tag as TTag
from tests.backends.tortoise.models import User as TUser


def _orm():
    return StrawberryORM.for_tortoise(warn_missing_scope=False, lazy_resolution="off")


@contextmanager
def _count_queries():
    from tortoise import connections

    client = connections.get("default")
    counter = {"n": 0}
    originals = {}

    def _wrap(original):
        async def counting(*args, **kwargs):
            counter["n"] += 1
            return await original(*args, **kwargs)

        return counting

    for name in ("execute_query", "execute_query_dict"):
        original = getattr(client, name, None)
        if original is None:  # pragma: no cover - both exist on the sqlite client
            continue
        originals[name] = original
        setattr(client, name, _wrap(original))
    try:
        yield counter
    finally:
        for name, original in originals.items():
            setattr(client, name, original)


class TestRelationConnection:
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        """Tortoise has to be initialised before relations are introspectable."""

    def _schema(self, orm, *, scope_drafts=False):
        @orm.type(TPost, filters=orm.filter(TPost), order=orm.order(TPost))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

            if scope_drafts:

                @classmethod
                def scope_rows(cls, qs, info):
                    return qs.filter(is_published=True)

        @orm.type(TUser)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        @strawberry.type
        class Query:
            users: list[UserNode] = orm.field.eager()

        return orm.schema(query=Query)

    async def _by_user(self, schema, query):
        result = await schema.execute(query)
        assert result.errors is None, result.errors
        return {
            u["name"]: [e["node"]["title"] for e in u["posts"]["edges"]]
            for u in result.data["users"]
        }

    PAGE = "{ users { name posts(first: 1) { edges { node { title } } } } }"

    async def test_each_parent_sees_only_its_own_rows(self):
        by_user = await self._by_user(
            self._schema(_orm()),
            "{ users { name posts(first: 10) { edges { node { title } } } } }",
        )
        assert by_user["Bob"] == ["Draft Post"]
        assert "Draft Post" not in by_user["Alice"]
        assert sorted(by_user["Alice"]) == ["GraphQL Guide", "Hello World"]

    async def test_the_related_types_scope_still_applies(self):
        """A connection must not be a way around scope_rows."""
        by_user = await self._by_user(
            self._schema(_orm(), scope_drafts=True),
            "{ users { name posts(first: 10) { edges { node { title } } } } }",
        )
        assert all("Draft Post" not in titles for titles in by_user.values())
        assert by_user["Bob"] == []

    async def test_pagination_is_per_parent(self):
        by_user = await self._by_user(self._schema(_orm()), self.PAGE)
        assert all(len(titles) <= 1 for titles in by_user.values())
        assert any(titles for titles in by_user.values())

    async def test_total_count_is_per_parent(self):
        result = await self._schema(_orm()).execute(
            "{ users { name posts(first: 1) { totalCount } } }"
        )
        assert result.errors is None, result.errors
        counts = {u["name"]: u["posts"]["totalCount"] for u in result.data["users"]}
        assert counts["Bob"] == 1, counts
        assert counts["Alice"] == 2, counts

    async def test_total_count_exceeds_the_page(self):
        """Counting the page would report the page size, not the parent's total."""
        alice = await TUser.get(name="Alice")
        for i in range(4):
            await TPost.create(
                title=f"Extra {i}", body="b", author=alice, is_published=True
            )

        result = await self._schema(_orm()).execute(
            "{ users { name posts(first: 1) { totalCount edges { node { title } } } } }"
        )
        assert result.errors is None, result.errors
        row = next(u for u in result.data["users"] if u["name"] == "Alice")
        assert len(row["posts"]["edges"]) == 1
        assert row["posts"]["totalCount"] == 6, row["posts"]["totalCount"]

    async def test_a_cursor_pages_within_each_parent(self):
        """after= has to skip within a parent's own page, not the whole window."""
        schema = self._schema(_orm())
        first = await schema.execute(
            "{ users { name posts(first: 1) { edges { cursor node { title } } } } }"
        )
        assert first.errors is None, first.errors
        alice = next(u for u in first.data["users"] if u["name"] == "Alice")
        cursor = alice["posts"]["edges"][0]["cursor"]
        page_one = alice["posts"]["edges"][0]["node"]["title"]

        second = await schema.execute(
            f'{{ users {{ name posts(first: 1, after: "{cursor}") '
            "{ edges { node { title } } } } }"
        )
        assert second.errors is None, second.errors
        alice2 = next(u for u in second.data["users"] if u["name"] == "Alice")
        assert [e["node"]["title"] for e in alice2["posts"]["edges"]] != [page_one]

    async def test_without_a_page_size_there_is_nothing_to_window(self):
        """No first= means no per-parent limit, so the plain path is the answer."""
        by_user = await self._by_user(
            self._schema(_orm()),
            "{ users { name posts { edges { node { title } } } } }",
        )
        assert by_user["Bob"] == ["Draft Post"]
        assert sorted(by_user["Alice"]) == ["GraphQL Guide", "Hello World"]

    async def test_a_single_parent_needs_no_window(self):
        """With nothing to batch across, the plain per-parent read is correct."""
        orm = _orm()

        @orm.type(TPost, filters=orm.filter(TPost), order=orm.order(TPost))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        @orm.type(TUser)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager()

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(name="Bob")

        @strawberry.type
        class Query:
            users: list[UserNode] = orm.field.eager()

        by_user = await self._by_user(
            orm.schema(query=Query),
            "{ users { name posts(first: 5) { edges { node { title } } } } }",
        )
        assert by_user == {"Bob": ["Draft Post"]}

    async def test_a_page_costs_the_same_whether_or_not_it_is_scoped(self):
        """Root, one windowed query for every parent's page, one for the totals."""
        schema = self._schema(_orm())
        with _count_queries() as counter:
            await self._by_user(schema, self.PAGE)
        assert counter["n"] == 3

        scoped = self._schema(_orm(), scope_drafts=True)
        with _count_queries() as counter:
            await self._by_user(scoped, self.PAGE)
        assert counter["n"] == 3

    async def test_the_cost_does_not_grow_with_the_number_of_parents(self):
        """The point of the window: statements track the shape, not the rows."""
        schema = self._schema(_orm())
        with _count_queries() as counter:
            await self._by_user(schema, self.PAGE)
        before = counter["n"]

        for i in range(5):
            user = await TUser.create(name=f"Extra {i}", email=f"e{i}@x.com")
            await TPost.create(title=f"P{i}", body="b", author=user, is_published=True)

        with _count_queries() as counter:
            by_user = await self._by_user(schema, self.PAGE)
        assert counter["n"] == before
        assert len(by_user) == 8

    async def test_a_connection_over_a_many_to_many_is_refused(self):
        """Those rows carry no parent column, so a window cannot partition them."""
        orm = _orm()

        @orm.type(TTag)
        class TagNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        with pytest.raises(ValueError, match="no column tying them to a parent"):

            @orm.type(TPost)
            class PostNode(relay.Node):
                id: relay.NodeID[int]
                tags: ORMListConnection[TagNode] = orm.connection.eager()

    async def _counts(self, schema, query):
        result = await schema.execute(query)
        assert result.errors is None, result.errors
        return {
            u["name"]: (
                u["posts"]["totalCount"],
                [e["node"]["title"] for e in u["posts"]["edges"]],
            )
            for u in result.data["users"]
        }

    async def test_a_filter_narrows_each_parents_page(self):
        """The page is cut before the resolver runs, so the filter must reach it."""
        counts = await self._counts(
            self._schema(_orm()),
            "{ users { name posts(first: 5, "
            'filter: {field: {title: {contains: "Hello"}}}) '
            "{ totalCount edges { node { title } } } } }",
        )
        assert counts["Alice"] == (1, ["Hello World"])
        assert counts["Bob"] == (0, [])

    async def test_a_filter_narrows_each_parents_total(self):
        """A total counted without the filter reports rows the caller excluded."""
        counts = await self._counts(
            self._schema(_orm()),
            "{ users { name posts(first: 1, "
            'filter: {field: {title: {contains: "Hello"}}}) '
            "{ totalCount edges { node { title } } } } }",
        )
        assert counts["Alice"][0] == 1, "the total ignored the filter"

    async def test_an_order_applies_within_each_parents_page(self):
        by_desc = await self._by_user(
            self._schema(_orm()),
            "{ users { name posts(first: 5, order: {field: {title: DESC}}) "
            "{ edges { node { title } } } } }",
        )
        by_asc = await self._by_user(
            self._schema(_orm()),
            "{ users { name posts(first: 5, order: {field: {title: ASC}}) "
            "{ edges { node { title } } } } }",
        )
        assert by_desc["Alice"] == ["Hello World", "GraphQL Guide"]
        assert by_asc["Alice"] == ["GraphQL Guide", "Hello World"]

    async def test_an_order_decides_which_rows_the_page_keeps(self):
        """Ordering has to reach the window, not just the rows it returned."""
        by_user = await self._by_user(
            self._schema(_orm()),
            "{ users { name posts(first: 1, order: {field: {title: ASC}}) "
            "{ edges { node { title } } } } }",
        )
        assert by_user["Alice"] == ["GraphQL Guide"]

    async def test_a_filter_still_costs_one_query_per_shape(self):
        schema = self._schema(_orm())
        query = (
            "{ users { name posts(first: 1, "
            'filter: {field: {title: {contains: "o"}}}) '
            "{ totalCount edges { node { title } } } } }"
        )
        with _count_queries() as counter:
            await self._by_user(schema, query)
        assert counter["n"] == 3

    async def test_a_backend_that_cannot_window_refuses(self, monkeypatch):
        """Without a window every parent costs a query, so say so up front."""
        orm = _orm()
        monkeypatch.setattr(
            type(orm._backend), "_supports_windowed_pages", False, raising=False
        )

        @orm.type(TPost, filters=orm.filter(TPost), order=orm.order(TPost))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        with pytest.raises(ValueError, match="needs a window function"):

            @orm.type(TUser)
            class UserNode(relay.Node):
                id: relay.NodeID[int]
                posts: ORMListConnection[PostNode] = orm.connection.eager()

    async def test_the_window_is_built_only_from_real_columns(self):
        """The partition and ordering are pasted in, so they are checked first."""
        from strawberry_orm.backends.tortoise import _quote_ident

        assert _quote_ident("author_id", TPost, '"') == '"author_id"'
        with pytest.raises(ValueError, match="no column 'nope'"):
            _quote_ident("nope", TPost, '"')

    async def test_filter_values_are_still_bound_not_pasted(self):
        """The window wraps Tortoise's SQL, so its placeholders must survive."""
        orm = _orm()

        @orm.type(TPost, filters=orm.filter(TPost), order=orm.order(TPost))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(title__not="'; DROP TABLE post; --")

        @orm.type(TUser)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        @strawberry.type
        class Query:
            users: list[UserNode] = orm.field.eager()

        by_user = await self._by_user(
            orm.schema(query=Query),
            "{ users { name posts(first: 10) { edges { node { title } } } } }",
        )
        assert sorted(by_user["Alice"]) == ["GraphQL Guide", "Hello World"]
        # The table would be gone if the value had been pasted into the SQL.
        assert await TPost.all().count() == 4
