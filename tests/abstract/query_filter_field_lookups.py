"""Abstract field-level filter tests: all operators from StringLookup and IntComparisonLookup."""


class AbstractTestQueryFilterFieldLookups:
    def test_exact_string(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { exact: "Alice" } } }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}]}

    def test_contains(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { title: { contains: "Guide" } } }) { title } }
        """)
        assert data == {"posts": [{"title": "GraphQL Guide"}]}

    def test_boolean_filter(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { isPublished: { exact: true } } }) { title } }
        """)
        assert data == {"posts": [
            {"title": "Hello World"},
            {"title": "GraphQL Guide"},
            {"title": "Rust Adventures"},
        ]}

    def test_integer_gt(self, execute, seed):
        data = execute("""
            { users(filter: { field: { id: { gt: 1 } } }) { name } }
        """)
        assert data == {"users": [{"name": "Bob"}, {"name": "Charlie"}]}

    def test_in_list(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { inList: ["Alice", "Charlie"] } } }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}

    def test_starts_with(self, execute, seed):
        data = execute("""
            { users(filter: { field: { email: { startsWith: "alice" } } }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}]}

    def test_neq(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { neq: "Bob" } } }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}

    def test_lte(self, execute, seed):
        data = execute("""
            { users(filter: { field: { id: { lte: 2 } } }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    def test_ends_with(self, execute, seed):
        data = execute("""
            { users(filter: { field: { email: { endsWith: ".org" } } }) { name } }
        """)
        assert data == {"users": [{"name": "Charlie"}]}

    def test_not_in_list(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { notInList: ["Alice", "Bob"] } } }) { name } }
        """)
        assert data == {"users": [{"name": "Charlie"}]}

    def test_is_null_false(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { isNull: false } } }) { name } }
        """)
        assert data == {"users": [
            {"name": "Alice"},
            {"name": "Bob"},
            {"name": "Charlie"},
        ]}

    def test_is_null_true(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { isNull: true } } }) { name } }
        """)
        assert data == {"users": []}

    def test_gte(self, execute, seed):
        data = execute("""
            { users(filter: { field: { id: { gte: 2 } } }) { name } }
        """)
        assert data == {"users": [{"name": "Bob"}, {"name": "Charlie"}]}

    def test_lt(self, execute, seed):
        data = execute("""
            { users(filter: { field: { id: { lt: 3 } } }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    def test_range(self, execute, seed):
        data = execute("""
            { users(filter: { field: { id: { range: { start: 1, end: 2 } } } }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    def test_i_contains(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { title: { iContains: "guide" } } }) { title } }
        """)
        assert data == {"posts": [{"title": "GraphQL Guide"}]}

    def test_i_starts_with(self, execute, seed):
        data = execute("""
            { users(filter: { field: { email: { iStartsWith: "ALICE" } } }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}]}

    def test_i_ends_with(self, execute, seed):
        data = execute("""
            { users(filter: { field: { email: { iEndsWith: ".ORG" } } }) { name } }
        """)
        assert data == {"users": [{"name": "Charlie"}]}

    def test_boolean_neq(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { isPublished: { neq: true } } }) { title } }
        """)
        assert data == {"posts": [{"title": "Draft Post"}]}

    def test_boolean_is_null(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { isPublished: { isNull: true } } }) { title } }
        """)
        assert data == {"posts": []}
