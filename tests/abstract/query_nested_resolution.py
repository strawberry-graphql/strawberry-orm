"""Abstract nested resolution tests: user->posts, post->tags, post->comments, deep nesting."""


class AbstractTestQueryNestedResolution:
    def test_user_with_posts(self, execute, seed):
        data = execute("{ user(id: 1) { name posts { title } } }")
        assert data == {
            "user": {
                "name": "Alice",
                "posts": [
                    {"title": "Hello World"},
                    {"title": "GraphQL Guide"},
                ],
            }
        }

    def test_post_with_tags(self, execute, seed):
        data = execute("{ posts { title tags { name } } }")
        assert data == {
            "posts": [
                {"title": "Hello World", "tags": [{"name": "python"}]},
                {
                    "title": "GraphQL Guide",
                    "tags": [{"name": "python"}, {"name": "graphql"}],
                },
                {"title": "Draft Post", "tags": []},
                {"title": "Rust Adventures", "tags": [{"name": "rust"}]},
            ]
        }

    def test_post_with_comments(self, execute, seed):
        data = execute("{ posts { title comments { body parentId } } }")
        assert data == {
            "posts": [
                {
                    "title": "Hello World",
                    "comments": [
                        {"body": "Nice post!", "parentId": None},
                        {"body": "Thanks!", "parentId": 1},
                    ],
                },
                {
                    "title": "GraphQL Guide",
                    "comments": [
                        {"body": "Great guide", "parentId": None},
                    ],
                },
                {"title": "Draft Post", "comments": []},
                {"title": "Rust Adventures", "comments": []},
            ]
        }

    def test_deeply_nested(self, execute, seed):
        data = execute(
            "{ user(id: 1) { posts { title tags { name } comments { body } } } }",
        )
        assert data == {
            "user": {
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
            }
        }
