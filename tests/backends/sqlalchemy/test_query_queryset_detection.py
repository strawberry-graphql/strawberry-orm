"""Queryset detection tests: verify is_query_object and get_default_queryset."""

from sqlalchemy import select


class TestQueryQuerysetDetection:
    def test_detects_select(self, orm, User):
        stmt = select(User)
        assert orm.backend.is_query_object(stmt) is True

    def test_rejects_plain_value(self, orm):
        assert orm.backend.is_query_object([1, 2, 3]) is False

    def test_default_queryset(self, orm, User):
        qs = orm.backend.get_default_queryset(User)
        assert orm.backend.is_query_object(qs) is True
