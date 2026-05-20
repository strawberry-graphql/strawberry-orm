"""Abstract nested filter logic tests: multi-field, multi-operator, deep nesting, complex scenarios."""


class AbstractTestQueryFilterMultiField:
    def test_two_column_conditions(self, execute, seed):
        data = execute("""
            { users(filter: {
                all: [
                    { field: { name: { contains: "li" } } },
                    { field: { email: { endsWith: ".com" } } }
                ]
            }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}]}

    def test_three_column_conditions(self, execute, seed):
        data = execute("""
            { posts(filter: {
                all: [
                    { object: { author: { field: { id: { exact: 1 } } } } },
                    { field: { isPublished: { exact: true } } },
                    { field: { title: { contains: "World" } } }
                ]
            }) { title } }
        """)
        assert data == {"posts": [{"title": "Hello World"}]}

    def test_multi_column_no_match(self, execute, seed):
        data = execute("""
            { users(filter: {
                all: [
                    { field: { name: { exact: "Alice" } } },
                    { field: { email: { contains: "test.org" } } }
                ]
            }) { name } }
        """)
        assert data == {"users": []}


class AbstractTestQueryFilterMultiOperatorLookup:
    def test_gt_and_lt_on_same_column(self, execute, seed):
        data = execute("""
            { users(filter: {
                field: { id: { gt: 1, lt: 3 } }
            }) { name } }
        """)
        assert data == {"users": [{"name": "Bob"}]}

    def test_contains_and_starts_with(self, execute, seed):
        data = execute("""
            { users(filter: {
                field: { email: { contains: "example", startsWith: "bob" } }
            }) { name } }
        """)
        assert data == {"users": [{"name": "Bob"}]}

    def test_gte_and_lte_on_same_column(self, execute, seed):
        data = execute("""
            { users(filter: {
                field: { id: { gte: 1, lte: 2 } }
            }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}


class AbstractTestQueryFilterDeeplyNested:
    def test_not_any_all_three_deep(self, execute, seed):
        data = execute("""
            { posts(filter: {
                not: {
                    any: [
                        {
                            all: [
                                { field: { isPublished: { exact: true } } },
                                { object: { author: { field: { id: { exact: 1 } } } } }
                            ]
                        },
                        { object: { author: { field: { id: { exact: 3 } } } } }
                    ]
                }
            }) { title } }
        """)
        assert data == {"posts": [{"title": "Draft Post"}]}

    def test_all_wrapping_any_wrapping_not(self, execute, seed):
        data = execute("""
            { users(filter: {
                all: [
                    {
                        any: [
                            { not: { field: { name: { exact: "Alice" } } } },
                            { not: { field: { name: { exact: "Bob" } } } }
                        ]
                    },
                    { not: { field: { email: { contains: "test.org" } } } }
                ]
            }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    def test_any_of_all_combinations(self, execute, seed):
        data = execute("""
            { users(filter: {
                any: [
                    {
                        all: [
                            { field: { name: { startsWith: "A" } } },
                            { field: { email: { contains: "example" } } }
                        ]
                    },
                    {
                        all: [
                            { field: { name: { startsWith: "C" } } },
                            { field: { email: { contains: "test" } } }
                        ]
                    }
                ]
            }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}

    def test_four_levels_deep(self, execute, seed):
        data = execute("""
            { users(filter: {
                not: {
                    all: [
                        {
                            any: [
                                { not: { field: { name: { exact: "Charlie" } } } }
                            ]
                        },
                        { not: { field: { email: { contains: "test.org" } } } }
                    ]
                }
            }) { name } }
        """)
        assert data == {"users": [{"name": "Charlie"}]}


class AbstractTestQueryFilterComplexScenarios:
    def test_filter_then_exclude(self, execute, seed):
        data = execute("""
            { posts(filter: {
                all: [
                    { field: { isPublished: { exact: true } } },
                    { not: { object: { author: { field: { id: { exact: 1 } } } } } }
                ]
            }) { title } }
        """)
        assert data == {"posts": [{"title": "Rust Adventures"}]}

    def test_one_of_with_nested_all(self, execute, seed):
        data = execute("""
            { posts(filter: {
                oneOf: [
                    {
                        all: [
                            { object: { author: { field: { id: { exact: 1 } } } } },
                            { field: { title: { contains: "World" } } }
                        ]
                    },
                    { object: { author: { field: { id: { exact: 3 } } } } }
                ]
            }) { title } }
        """)
        assert data == {
            "posts": [
                {"title": "Hello World"},
                {"title": "Rust Adventures"},
            ]
        }

    def test_not_with_in_list(self, execute, seed):
        data = execute("""
            { users(filter: {
                not: { field: { name: { inList: ["Alice", "Bob"] } } }
            }) { name } }
        """)
        assert data == {"users": [{"name": "Charlie"}]}

    def test_all_with_range_and_string_filter(self, execute, seed):
        data = execute("""
            { users(filter: {
                all: [
                    { field: { id: { range: { start: 1, end: 2 } } } },
                    { field: { email: { contains: "example" } } }
                ]
            }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    def test_any_field_or_boolean_mix(self, execute, seed):
        data = execute("""
            { users(filter: {
                any: [
                    { field: { name: { exact: "Alice" } } },
                    {
                        all: [
                            { field: { email: { contains: "test" } } },
                            { field: { id: { gt: 2 } } }
                        ]
                    }
                ]
            }) { name } }
        """)
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}
