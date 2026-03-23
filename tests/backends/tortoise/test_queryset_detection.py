"""Queryset detection tests for the Tortoise backend."""


class TestQuerysetDetection:
    def test_rejects_plain_value(self, orm):
        assert orm.backend.is_query_object("hello") is False
