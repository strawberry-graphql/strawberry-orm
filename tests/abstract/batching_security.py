"""Security properties of relation batching.

Rewriting a per-parent query into one ``IN`` query is only acceptable if it
cannot widen what a parent can see. These assert the two ways that could go
wrong: scoping dropped from the rewritten query, and rows leaking across
parents.
"""

QUERY = "{ users { name posts { title } } }"


class AbstractTestBatchingSecurity:
    """Subclasses provide ``scoped_schema`` and ``execute``."""

    def test_batching_preserves_child_scoping(self, seed):
        """A rewritten query keeps the child type's scope_rows filter."""
        result = self.execute(self.scoped_schema(batching=True), QUERY)
        assert result.errors is None

        titles = {
            post["title"] for user in result.data["users"] for post in user["posts"]
        }
        assert "Draft Post" not in titles

    def test_batched_scoping_matches_per_row_scoping(self, seed):
        batched = self.execute(self.scoped_schema(batching=True), QUERY)
        per_row = self.execute(self.scoped_schema(batching=False), QUERY)
        assert batched.errors is None
        assert per_row.errors is None
        assert batched.data == per_row.data

    def test_no_parent_sees_another_parents_rows(self, seed):
        """Every returned row must belong to the parent it was returned under."""
        result = self.execute(self.scoped_schema(batching=True), QUERY)
        assert result.errors is None

        expected = self.expected_titles_by_user()
        for user in result.data["users"]:
            got = sorted(post["title"] for post in user["posts"])
            assert got == sorted(expected[user["name"]]), (
                f"{user['name']} received the wrong rows: {got}"
            )
