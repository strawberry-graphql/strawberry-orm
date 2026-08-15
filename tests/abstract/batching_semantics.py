"""Semantics that batching must preserve beyond "same rows come back".

The differential harness compares whole-response equality for a fixed set of
resolver shapes. These cover the structural properties that a shape-based
comparison would not catch: per-parent ordering, empty parents, nesting,
aliasing, error attribution, and per-operation isolation.
"""

QUERY = "{ users { name posts { title } } }"


class AbstractTestBatchingSemantics:
    """Subclasses provide ``schema_for`` and ``execute``."""

    # -- rows land under the right parent, in the right order ---------------

    def test_per_parent_ordering_is_preserved(self, seed):
        """A batched query returns all parents' rows in one result set, so the
        fan-out must not scramble each parent's own ordering."""
        result = self.execute(self.schema_for("ordered"), QUERY)
        assert result.errors is None

        alice = next(u for u in result.data["users"] if u["name"] == "Alice")
        titles = [post["title"] for post in alice["posts"]]
        assert titles == sorted(titles, reverse=True)

    def test_ordering_matches_the_unbatched_result_exactly(self, seed):
        batched = self.execute(self.schema_for("ordered"), QUERY)
        per_row = self.execute(self.schema_for("ordered", batching=False), QUERY)
        assert batched.data == per_row.data

    def test_parent_with_no_matching_rows_gets_an_empty_list(self, seed):
        """Bob has only an unpublished post, so he must come back with []."""
        result = self.execute(self.schema_for("filtered"), QUERY)
        assert result.errors is None

        bob = next(u for u in result.data["users"] if u["name"] == "Bob")
        assert bob["posts"] == []

    # -- structure ----------------------------------------------------------

    def test_aliases_of_the_same_relation_stay_separate(self, seed):
        query = """
        { users { name
            published: posts { title }
            everything: allPosts { title }
        } }
        """
        result = self.execute(self.schema_for("aliased"), query)
        assert result.errors is None

        bob = next(u for u in result.data["users"] if u["name"] == "Bob")
        assert bob["published"] == []
        assert [p["title"] for p in bob["everything"]] == ["Draft Post"]

    def test_batching_survives_inline_fragments(self, seed):
        query = "{ users { name ... on UT { posts { title } } } }"
        plain = self.execute(self.schema_for("plain"), QUERY)
        fragmented = self.execute(self.schema_for("plain"), query)
        assert fragmented.errors is None
        assert fragmented.data == plain.data

    def test_repeated_execution_does_not_reuse_a_stale_cache(self, seed):
        """The batch cache is per operation; a second run must re-query."""
        schema = self.schema_for("plain")
        first = self.execute(schema, QUERY)
        second = self.execute(schema, QUERY)
        assert first.data == second.data
        assert self.count_queries(schema, QUERY) == self.count_queries(schema, QUERY)

    # -- failure handling ---------------------------------------------------

    def test_error_from_one_parent_is_not_attributed_to_another(self, seed):
        """Running ahead resolves siblings speculatively, so a sibling's
        exception must not be reported against the parent that triggered it.

        The field is nullable here so GraphQL stops null propagation at the
        failing field, which is what makes the attribution observable.
        """
        query = "{ users { name maybePosts { title } } }"
        result = self.execute(self.schema_for("raises_for_bob"), query)

        assert result.errors is not None
        assert [error.path for error in result.errors] == [["users", 1, "maybePosts"]]

        users = result.data["users"]
        assert users[1]["name"] == "Bob"
        assert users[1]["maybePosts"] is None
        assert users[0]["maybePosts"] is not None

    def test_error_attribution_matches_the_unbatched_result(self, seed):
        query = "{ users { name maybePosts { title } } }"
        batched = self.execute(self.schema_for("raises_for_bob"), query)
        per_row = self.execute(self.schema_for("raises_for_bob", batching=False), query)
        assert batched.data == per_row.data
        assert [e.path for e in batched.errors] == [e.path for e in per_row.errors]
