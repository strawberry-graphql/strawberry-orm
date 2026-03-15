"""Abstract error handling and edge-case tests (backend-agnostic)."""


class AbstractTestQueryErrorHandling:
    def test_empty_result(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { exact: "Nobody" } } }) { name } }
        """)
        assert data == {"users": []}

    def test_is_query_object_with_none(self, orm):
        assert orm.is_query_object(None) is False

    def test_is_query_object_with_dict(self, orm):
        assert orm.is_query_object({"key": "value"}) is False

    def test_is_query_object_with_int(self, orm):
        assert orm.is_query_object(42) is False
