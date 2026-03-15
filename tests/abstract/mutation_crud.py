"""Abstract mutation tests: create, update, delete posts."""


class AbstractTestMutationCrud:
    def test_create_post(self, execute, seed):
        data = execute("""
            mutation {
                createPost(input: { title: "New Post", body: "Content here", authorId: 2 }) {
                    id title body isPublished
                }
            }
        """)
        assert data == {
            "createPost": {
                "id": 5,
                "title": "New Post",
                "body": "Content here",
                "isPublished": False,
            }
        }

    def test_update_post(self, execute, seed):
        data = execute("""
            mutation {
                updatePost(input: { id: 1, title: "Updated Title" }) {
                    id title body
                }
            }
        """)
        assert data == {
            "updatePost": {
                "id": 1,
                "title": "Updated Title",
                "body": "First post",
            }
        }

    def test_delete_post(self, execute, seed):
        data = execute("mutation { deletePost(id: 3) }")
        assert data == {"deletePost": True}
        data = execute("{ posts { id } }")
        assert data == {"posts": [{"id": 1}, {"id": 2}, {"id": 4}]}

    def test_delete_nonexistent(self, execute, seed):
        data = execute("mutation { deletePost(id: 999) }")
        assert data == {"deletePost": False}

    def test_create_then_query(self, execute, seed):
        data = execute("""
            mutation {
                createPost(input: { title: "Fresh", body: "Brand new", authorId: 1 }) {
                    id
                }
            }
        """)
        assert data == {"createPost": {"id": 5}}
        data = execute("{ posts { title } }")
        assert data == {
            "posts": [
                {"title": "Hello World"},
                {"title": "GraphQL Guide"},
                {"title": "Draft Post"},
                {"title": "Rust Adventures"},
                {"title": "Fresh"},
            ]
        }
