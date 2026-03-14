"""Abstract tests: self in custom resolvers is the ORM model instance."""


class AbstractTestQuerySelfIsModel:
    def test_summary_uses_model_fields(self, self_model_execute):
        data = self_model_execute('{ posts { title summary } }')
        assert data == {"posts": [
            {"title": "Hello World", "summary": "Hello World: First post"},
            {"title": "GraphQL Guide", "summary": "GraphQL Guide: Learn Grap"},
            {"title": "Draft Post", "summary": "Draft Post: Not publis"},
            {"title": "Rust Adventures", "summary": "Rust Adventures: Systems pr"},
        ]}

    def test_title_upper_uses_model_method(self, self_model_execute):
        data = self_model_execute('{ posts { title titleUpper } }')
        assert data == {"posts": [
            {"title": "Hello World", "titleUpper": "HELLO WORLD"},
            {"title": "GraphQL Guide", "titleUpper": "GRAPHQL GUIDE"},
            {"title": "Draft Post", "titleUpper": "DRAFT POST"},
            {"title": "Rust Adventures", "titleUpper": "RUST ADVENTURES"},
        ]}

    def test_display_name_uses_model_fields(self, self_model_execute):
        data = self_model_execute('{ users { displayName } }')
        assert data == {"users": [
            {"displayName": "Alice <alice@example.com>"},
            {"displayName": "Bob <bob@example.com>"},
            {"displayName": "Charlie <charlie@test.org>"},
        ]}

    def test_post_count_accesses_relationship(self, self_model_execute):
        data = self_model_execute('{ users { name postCount } }')
        assert data == {"users": [
            {"name": "Alice", "postCount": 2},
            {"name": "Bob", "postCount": 1},
            {"name": "Charlie", "postCount": 1},
        ]}
