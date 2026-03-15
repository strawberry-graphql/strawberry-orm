"""Abstract tests: multiple types from the same ORM model exposing different fields."""


class AbstractTestQueryMultipleTypes:
    def test_brief_type_has_fewer_fields(self, user_brief_type, user_full_type):
        brief_fields = {
            f.name for f in user_brief_type.__strawberry_definition__.fields
        }
        full_fields = {f.name for f in user_full_type.__strawberry_definition__.fields}
        assert brief_fields < full_fields
        assert "email" not in brief_fields
        assert "email" in full_fields

    def test_brief_query(self, multi_type_execute):
        data = multi_type_execute("{ usersBrief { id name } }")
        assert data == {
            "usersBrief": [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "Bob"},
                {"id": 3, "name": "Charlie"},
            ]
        }

    def test_full_query(self, multi_type_execute):
        data = multi_type_execute("{ usersFull { id name email } }")
        assert data == {
            "usersFull": [
                {"id": 1, "name": "Alice", "email": "alice@example.com"},
                {"id": 2, "name": "Bob", "email": "bob@example.com"},
                {"id": 3, "name": "Charlie", "email": "charlie@test.org"},
            ]
        }

    def test_both_queries_together(self, multi_type_execute):
        data = multi_type_execute("{ usersBrief { name } usersFull { name email } }")
        assert data == {
            "usersBrief": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ],
            "usersFull": [
                {"name": "Alice", "email": "alice@example.com"},
                {"name": "Bob", "email": "bob@example.com"},
                {"name": "Charlie", "email": "charlie@test.org"},
            ],
        }
