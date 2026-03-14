"""Queryset detection tests: verify is_query_object and get_default_queryset."""


class TestQueryQuerysetDetection:
    def test_detects_queryset(self, orm, User, setup_tables):
        qs = User.objects.all()
        assert orm.backend.is_query_object(qs) is True

    def test_rejects_plain_value(self, orm):
        assert orm.backend.is_query_object([1, 2, 3]) is False

    def test_default_queryset(self, orm, User, setup_tables):
        qs = orm.backend.get_default_queryset(User)
        assert orm.backend.is_query_object(qs) is True
