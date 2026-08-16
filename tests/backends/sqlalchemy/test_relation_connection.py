"""A connection declared on a type is served by that parent's relation.

Built from the annotation alone it would query the whole table, because
``__set_name__`` runs before ``@orm.type`` knows what the parent is. The type
loop rebuilds it once the model is known, and the pages are cut with one
windowed query rather than one query per parent.
"""

import pytest
import strawberry
from sqlalchemy import event
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import Tag as SATag
from tests.backends.sqlalchemy.models import User as SAUser


def _orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, lazy_resolution="off"
    )


class TestRelationConnection:
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self._s = sa_session

    def _schema(self, orm, *, scope_drafts=False):
        @orm.type(SAPost, filters=orm.filter(SAPost), order=orm.order(SAPost))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

            if scope_drafts:

                @classmethod
                def scope_rows(cls, query, info):
                    return query.where(SAPost.is_published.is_(True))

        @orm.type(SAUser)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        @strawberry.type
        class Query:
            users: list[UserNode] = orm.field.eager()

        return orm.schema(query=Query)

    def _by_user(self, schema, query):
        result = schema.execute_sync(query, context_value={"session": self._s})
        assert result.errors is None, result.errors
        return {
            u["name"]: [e["node"]["title"] for e in u["posts"]["edges"]]
            for u in result.data["users"]
        }

    def _count(self, schema, query):
        statements: list[str] = []

        def _before(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        engine = self._s.bind
        event.listen(engine, "before_cursor_execute", _before)
        try:
            self._by_user(schema, query)
        finally:
            event.remove(engine, "before_cursor_execute", _before)
        return len(statements)

    def test_a_cursor_pages_within_each_parent(self):
        """after= has to skip within a parent's own page, not the whole window."""
        schema = self._schema(_orm())
        first = schema.execute_sync(
            "{ users { name posts(first: 1) { edges { cursor node { title } } } } }",
            context_value={"session": self._s},
        )
        assert first.errors is None, first.errors
        alice = next(u for u in first.data["users"] if u["name"] == "Alice")
        cursor = alice["posts"]["edges"][0]["cursor"]
        page_one = alice["posts"]["edges"][0]["node"]["title"]

        second = schema.execute_sync(
            f'{{ users {{ name posts(first: 1, after: "{cursor}") '
            "{ edges { node { title } } } } }",
            context_value={"session": self._s},
        )
        assert second.errors is None, second.errors
        alice2 = next(u for u in second.data["users"] if u["name"] == "Alice")
        assert [e["node"]["title"] for e in alice2["posts"]["edges"]] != [page_one]

    def test_without_a_page_size_there_is_nothing_to_window(self):
        """No first= means no per-parent limit, so the plain path is the answer."""
        by_user = self._by_user(
            self._schema(_orm()),
            "{ users { name posts { edges { node { title } } } } }",
        )
        assert by_user["Bob"] == ["Draft Post"]

    def test_a_single_parent_needs_no_window(self):
        """With nothing to batch across, the plain per-parent read is correct."""
        orm = _orm()

        @orm.type(SAPost, filters=orm.filter(SAPost), order=orm.order(SAPost))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        @orm.type(SAUser)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager()

            @classmethod
            def scope_rows(cls, query, info):
                return query.where(SAUser.name == "Bob")

        @strawberry.type
        class Query:
            users: list[UserNode] = orm.field.eager()

        by_user = self._by_user(
            orm.schema(query=Query),
            "{ users { name posts(first: 5) { edges { node { title } } } } }",
        )
        assert by_user == {"Bob": ["Draft Post"]}

    PAGE = "{ users { name posts(first: 1) { edges { node { title } } } } }"

    def test_each_parent_sees_only_its_own_rows(self):
        by_user = self._by_user(
            self._schema(_orm()),
            "{ users { name posts(first: 10) { edges { node { title } } } } }",
        )
        assert by_user["Bob"] == ["Draft Post"]
        assert "Draft Post" not in by_user["Alice"]

    def test_the_related_types_scope_still_applies(self):
        """A connection must not be a way around scope_rows."""
        by_user = self._by_user(
            self._schema(_orm(), scope_drafts=True),
            "{ users { name posts(first: 10) { edges { node { title } } } } }",
        )
        assert all("Draft Post" not in titles for titles in by_user.values())

    def test_pagination_is_per_parent(self):
        by_user = self._by_user(self._schema(_orm()), self.PAGE)
        assert all(len(titles) <= 1 for titles in by_user.values())
        assert any(titles for titles in by_user.values())

    def test_total_count_is_per_parent(self):
        schema = self._schema(_orm())
        result = schema.execute_sync(
            "{ users { name posts(first: 1) { totalCount } } }",
            context_value={"session": self._s},
        )
        assert result.errors is None, result.errors
        counts = {u["name"]: u["posts"]["totalCount"] for u in result.data["users"]}
        assert counts["Bob"] == 1, counts
        assert counts["Alice"] >= 2, counts

    def test_a_page_costs_the_same_whether_or_not_it_is_scoped(self):
        """Root, one windowed query for every parent's page, one for the totals."""
        assert self._count(self._schema(_orm()), self.PAGE) == 3
        assert self._count(self._schema(_orm(), scope_drafts=True), self.PAGE) == 3

    def test_the_cost_does_not_grow_with_the_number_of_parents(self):
        """The point of the window: statements track the shape, not the rows."""
        schema = self._schema(_orm())
        before = self._count(schema, self.PAGE)
        for i in range(5):
            user = SAUser(name=f"Extra {i}", email=f"e{i}@x.com")
            self._s.add(user)
            self._s.flush()
            self._s.add(SAPost(title=f"P{i}", body="b", author=user, is_published=True))
        self._s.flush()
        assert self._count(schema, self.PAGE) == before

    def test_a_connection_over_a_many_to_many_is_refused(self):
        """Those rows carry no parent column, so a window cannot partition them."""
        orm = _orm()

        @orm.type(SATag)
        class TagNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        with pytest.raises(ValueError, match="no column tying them to a parent"):

            @orm.type(SAPost)
            class PostNode(relay.Node):
                id: relay.NodeID[int]
                tags: ORMListConnection[TagNode] = orm.connection.eager()

    def test_total_count_exceeds_the_page(self):
        """Counting the page would report the page size, not the parent's total."""
        alice = self._s.query(SAUser).filter(SAUser.name == "Alice").one()
        for i in range(4):
            self._s.add(
                SAPost(title=f"Extra {i}", body="b", author=alice, is_published=True)
            )
        self._s.flush()

        result = self._schema(_orm()).execute_sync(
            "{ users { name posts(first: 1) { totalCount edges { node { title } } } } }",
            context_value={"session": self._s},
        )
        assert result.errors is None, result.errors
        row = next(u for u in result.data["users"] if u["name"] == "Alice")
        assert len(row["posts"]["edges"]) == 1
        assert row["posts"]["totalCount"] >= 5, row["posts"]["totalCount"]
