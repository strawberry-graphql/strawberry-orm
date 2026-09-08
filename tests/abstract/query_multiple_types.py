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

    def test_scope_on_the_queried_type_applies(self, multi_type_execute):
        """The scope declared on UserFull.posts filters UserFull's rows.

        UserFull is the *second* type registered for the user model, so a
        model-keyed hint lookup would find UserBrief's (absent) hints instead
        and let the draft post through.
        """
        data = multi_type_execute("{ usersFull { name posts { title } } }")
        assert data == {
            "usersFull": [
                {
                    "name": "Alice",
                    "posts": [{"title": "Hello World"}, {"title": "GraphQL Guide"}],
                },
                {"name": "Bob", "posts": []},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }

    def test_sibling_type_of_the_same_model_stays_unscoped(self, multi_type_execute):
        """UserBrief.posts declares no scope, so UserFull's must not leak onto it."""
        data = multi_type_execute("{ usersBrief { name posts { title } } }")
        assert data == {
            "usersBrief": [
                {
                    "name": "Alice",
                    "posts": [{"title": "Hello World"}, {"title": "GraphQL Guide"}],
                },
                {"name": "Bob", "posts": [{"title": "Draft Post"}]},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }
