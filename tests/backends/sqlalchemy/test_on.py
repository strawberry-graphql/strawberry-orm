"""``on=`` serves a field from a relation under another name.

SQLAlchemy loader options populate the mapped attribute, so a second view of a
relation cannot be prefetched beside the first. Declaring one therefore turns
batching on, which is what collapses the per-parent statements into one query.
"""

import pytest
import strawberry
from sqlalchemy import event

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import Tag as SATag
from tests.backends.sqlalchemy.models import User as SAUser


def _orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, lazy_resolution="off"
    )


class TestVia:
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self._s = sa_session

    def _schema(self, orm):
        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto
            published: list[PostType] = orm.field.eager(
                on="posts",
                scope=lambda q, info: q.where(SAPost.is_published.is_(True)),
            )
            drafts: list[PostType] = orm.field.eager(
                on="posts",
                scope=lambda q, info: q.where(SAPost.is_published.is_(False)),
            )

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.eager()

        return orm.schema(query=Query)

    QUERY = "{ users { name published { title } drafts { title } } }"

    def _run(self, schema):
        result = schema.execute_sync(self.QUERY, context_value={"session": self._s})
        assert result.errors is None, result.errors
        return {
            u["name"]: (
                sorted(p["title"] for p in u["published"]),
                sorted(p["title"] for p in u["drafts"]),
            )
            for u in result.data["users"]
        }

    def test_one_relation_backs_two_filtered_views(self):
        by_user = self._run(self._schema(_orm()))

        published, drafts = by_user["Alice"]
        assert "Hello World" in published
        assert "Draft Post" not in published
        assert published != drafts, "both views returned the same rows"

    def test_each_view_keeps_its_own_scope(self):
        by_user = self._run(self._schema(_orm()))

        all_published = {t for pub, _ in by_user.values() for t in pub}
        all_drafts = {t for _, dr in by_user.values() for t in dr}
        assert not (all_published & all_drafts), (
            "a post appeared under both views, so one scope leaked into the other"
        )

    def test_on_with_batching_off_is_refused(self):
        """Without to_attr the batcher is the only thing making this eager."""
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            warn_missing_scope=False,
            lazy_resolution="off",
            batch_relations=False,
        )
        with pytest.raises(ValueError, match="on= needs batch_relations"):
            self._schema(orm)

    def test_both_views_load_without_a_query_per_row(self):
        """The batcher is what keeps this off one-query-per-parent."""
        schema = self._schema(_orm())
        statements: list[str] = []

        def _before(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        engine = self._s.bind
        event.listen(engine, "before_cursor_execute", _before)
        try:
            schema.execute_sync(self.QUERY, context_value={"session": self._s})
        finally:
            event.remove(engine, "before_cursor_execute", _before)

        assert len(statements) <= 3, statements

    def test_on_through_an_association_table_is_refused(self):
        """Those rows carry no parent column, so the batcher cannot group them."""
        orm = _orm()

        @orm.type(SATag)
        class TagType:
            id: auto
            name: auto

        with pytest.raises(ValueError, match="association table"):

            @orm.type(SAPost)
            class PostType:
                id: auto
                named: list[TagType] = orm.field.eager(on="tags")

    def test_on_naming_a_relation_that_does_not_exist_is_rejected(self):
        orm = _orm()

        @orm.type(SAPost)
        class PostType:
            id: auto

        with pytest.raises(ValueError, match="has no relation 'postz'"):

            @orm.type(SAUser)
            class UserType:
                id: auto
                published: list[PostType] = orm.field.eager(on="postz")
