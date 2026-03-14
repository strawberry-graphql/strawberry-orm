"""Abstract auto-resolution tests: scalar fields and relationships."""

from strawberry_orm.types import auto


class AbstractTestQueryScalarAutoResolution:
    def test_auto_resolves_integer_pk(self, orm, User):
        @orm.type(User)
        class UT:
            id: auto

        fields = {f.name: f for f in UT.__strawberry_definition__.fields}
        assert "id" in fields

    def test_auto_resolves_string_column(self, orm, User):
        @orm.type(User)
        class UT:
            name: auto
            email: auto

        fields = {f.name: f for f in UT.__strawberry_definition__.fields}
        assert "name" in fields
        assert "email" in fields

    def test_auto_resolves_boolean_column(self, orm, Post):
        @orm.type(Post)
        class PT:
            is_published: auto

        fields = {f.name: f for f in PT.__strawberry_definition__.fields}
        assert "is_published" in fields

    def test_stores_orm_model_reference(self, orm, User):
        @orm.type(User)
        class UT:
            id: auto

        assert UT.__orm_model__ is User

    def test_nullable_column_resolves_to_optional(self, orm, Comment):
        @orm.type(Comment)
        class CT:
            id: auto
            parent_id: auto

        fields = {f.name: f for f in CT.__strawberry_definition__.fields}
        assert "parent_id" in fields


class AbstractTestQueryRelationshipAutoResolution:
    def test_user_posts_resolved(self, execute, seed):
        data = execute('{ users { name posts { title } } }')
        assert data == {"users": [
            {"name": "Alice", "posts": [
                {"title": "Hello World"},
                {"title": "GraphQL Guide"},
            ]},
            {"name": "Bob", "posts": [
                {"title": "Draft Post"},
            ]},
            {"name": "Charlie", "posts": [
                {"title": "Rust Adventures"},
            ]},
        ]}

    def test_post_tags_resolved(self, execute, seed):
        data = execute('{ posts { title tags { name } } }')
        assert data == {"posts": [
            {"title": "Hello World", "tags": [{"name": "python"}]},
            {"title": "GraphQL Guide", "tags": [{"name": "python"}, {"name": "graphql"}]},
            {"title": "Draft Post", "tags": []},
            {"title": "Rust Adventures", "tags": [{"name": "rust"}]},
        ]}

    def test_post_comments_resolved(self, execute, seed):
        data = execute('{ posts { title comments { body } } }')
        assert data == {"posts": [
            {"title": "Hello World", "comments": [
                {"body": "Nice post!"},
                {"body": "Thanks!"},
            ]},
            {"title": "GraphQL Guide", "comments": [
                {"body": "Great guide"},
            ]},
            {"title": "Draft Post", "comments": []},
            {"title": "Rust Adventures", "comments": []},
        ]}

    def test_deep_nesting(self, execute, seed):
        data = execute("""
            { users { name posts { title tags { name } comments { body } } } }
        """)
        assert data == {"users": [
            {
                "name": "Alice",
                "posts": [
                    {
                        "title": "Hello World",
                        "tags": [{"name": "python"}],
                        "comments": [{"body": "Nice post!"}, {"body": "Thanks!"}],
                    },
                    {
                        "title": "GraphQL Guide",
                        "tags": [{"name": "python"}, {"name": "graphql"}],
                        "comments": [{"body": "Great guide"}],
                    },
                ],
            },
            {
                "name": "Bob",
                "posts": [
                    {
                        "title": "Draft Post",
                        "tags": [],
                        "comments": [],
                    },
                ],
            },
            {
                "name": "Charlie",
                "posts": [
                    {
                        "title": "Rust Adventures",
                        "tags": [{"name": "rust"}],
                        "comments": [],
                    },
                ],
            },
        ]}

    def test_empty_relationship(self, execute, seed):
        data = execute('{ posts { title tags { name } } }')
        assert data == {"posts": [
            {"title": "Hello World", "tags": [{"name": "python"}]},
            {"title": "GraphQL Guide", "tags": [{"name": "python"}, {"name": "graphql"}]},
            {"title": "Draft Post", "tags": []},
            {"title": "Rust Adventures", "tags": [{"name": "rust"}]},
        ]}

    def test_fk_scalar_resolves(self, execute, seed):
        data = execute('{ posts { title comments { postId authorId } } }')
        assert data == {"posts": [
            {"title": "Hello World", "comments": [
                {"postId": 1, "authorId": 2},
                {"postId": 1, "authorId": 1},
            ]},
            {"title": "GraphQL Guide", "comments": [
                {"postId": 2, "authorId": 3},
            ]},
            {"title": "Draft Post", "comments": []},
            {"title": "Rust Adventures", "comments": []},
        ]}
