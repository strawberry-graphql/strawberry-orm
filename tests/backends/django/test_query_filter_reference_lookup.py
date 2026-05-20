"""ReferenceLookup tests for Django."""

from django.db import connection
from django.test.utils import CaptureQueriesContext

from strawberry_orm.filters import is_reference_lookup_type
from tests.abstract.query_filter_reference_lookup import (
    AbstractTestQueryFilterReferenceLookup,
)
from tests.backends.django.fixtures import PostFilter, UserFilter


class TestQueryFilterReferenceLookup(AbstractTestQueryFilterReferenceLookup):
    def test_post_field_hides_forward_fk_column(self):
        field_type = PostFilter._field_type
        field_names = {f.name for f in field_type.__strawberry_definition__.fields}
        assert "author_id" not in field_names
        assert "authorId" not in field_names

    def test_user_field_id_uses_int_comparison_lookup(self):
        field_type = UserFilter._field_type
        fields = {f.name: f.type for f in field_type.__strawberry_definition__.fields}
        assert "id" in fields
        assert not is_reference_lookup_type(fields["id"])

    def test_object_author_id_filter_avoids_join(self, execute, seed):
        with CaptureQueriesContext(connection) as ctx:
            execute("""
                { posts(filter: {
                    object: { author: { field: { id: { inList: [1] } } } }
                }) { title } }
            """)
        sql = " ".join(q["sql"] for q in ctx.captured_queries).upper()
        assert "JOIN" not in sql
        assert "AUTHOR_ID" in sql

    def test_object_author_name_filter_may_join(self, execute, seed):
        with CaptureQueriesContext(connection) as ctx:
            execute("""
                { posts(filter: {
                    object: { author: { field: { name: { exact: "Alice" } } } }
                }) { title } }
            """)
        sql = " ".join(q["sql"] for q in ctx.captured_queries).upper()
        assert "JOIN" in sql
