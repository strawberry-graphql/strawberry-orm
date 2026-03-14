"""Abstract boolean filter logic tests: any, all, not, one_of, nested combinations."""


class AbstractTestQueryFilterBooleanOperators:
    def test_any_filter(self, execute, seed):
        data = execute("""
            { users(filter: {
                any: [
                    { field: { name: { exact: "Alice" } } },
                    { field: { name: { exact: "Bob" } } }
                ]
            }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    def test_all_filter(self, execute, seed):
        data = execute("""
            { users(filter: {
                all: [
                    { field: { email: { contains: "example" } } },
                    { field: { id: { gt: 1 } } }
                ]
            }) { name } }
        """)
        assert data == {"users": [{"name": "Bob"}]}

    def test_not_filter(self, execute, seed):
        data = execute("""
            { users(filter: {
                not: { field: { name: { exact: "Alice" } } }
            }) { name } }
        """)
        assert data == {"users": [{"name": "Bob"}, {"name": "Charlie"}]}

    def test_nested_any_all(self, execute, seed):
        data = execute("""
            { posts(filter: {
                any: [
                    {
                        all: [
                            { field: { authorId: { exact: 1 } } },
                            { field: { isPublished: { exact: true } } }
                        ]
                    },
                    { field: { authorId: { exact: 3 } } }
                ]
            }) { title } }
        """)
        assert data == {"posts": [
            {"title": "Hello World"},
            {"title": "GraphQL Guide"},
            {"title": "Rust Adventures"},
        ]}

    def test_not_any_equals_none_of(self, execute, seed):
        data = execute("""
            { users(filter: {
                not: {
                    any: [
                        { field: { name: { exact: "Alice" } } },
                        { field: { name: { exact: "Bob" } } }
                    ]
                }
            }) { name } }
        """)
        assert data == {"users": [{"name": "Charlie"}]}

    def test_one_of_combinator(self, execute, seed):
        data = execute("""
            { users(filter: {
                oneOf: [
                    { field: { name: { exact: "Alice" } } },
                    { field: { name: { exact: "Charlie" } } }
                ]
            }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}

    def test_not_all_combination(self, execute, seed):
        data = execute("""
            { users(filter: {
                not: {
                    all: [
                        { field: { email: { contains: "example" } } },
                        { field: { id: { gt: 1 } } }
                    ]
                }
            }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}
