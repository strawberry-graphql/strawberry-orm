"""Error handling tests — common from abstract, Django-specific kept local."""

from tests.abstract.query_error_handling import AbstractTestQueryErrorHandling


class TestQueryErrorHandling(AbstractTestQueryErrorHandling):
    def test_is_query_object_with_query(self, orm, User, setup_tables):
        qs = User.objects.all()
        assert orm.is_query_object(qs) is True
