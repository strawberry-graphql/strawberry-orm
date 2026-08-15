"""Edge cases where a naive batch rewrite would go wrong.

Each of these is a way the "run every sibling, group by shape, one IN query"
strategy could produce wrong rows rather than merely fewer queries:

* field arguments must reach the speculative sibling calls, or siblings build a
  different query than the one GraphQL asked for
* the parent key is not always an integer, and grouping reads it back off rows
* a self-referential relation makes parent and child the same table
* a relation reached through a join has no key column on the row to group by
* the same relation under two different paths must not share a cache entry
"""

USERS_QUERY = "{ users { name posts { title } } }"


class AbstractTestBatchingEdgeCases:
    """Subclasses provide the schema builders and ``execute``/``count_queries``."""

    def test_field_arguments_reach_every_sibling(self, seed):
        """Siblings are resolved speculatively, so they must be passed the same
        arguments; otherwise a batch answers a question nobody asked."""
        query = '{ users { name postsMatching(title: "Hello World") { title } } }'
        batched = self.execute(self.args_schema(batching=True), query)
        per_row = self.execute(self.args_schema(batching=False), query)

        assert batched.errors is None, batched.errors
        assert batched.data == per_row.data

        alice = next(u for u in batched.data["users"] if u["name"] == "Alice")
        assert [p["title"] for p in alice["postsMatching"]] == ["Hello World"]

    def test_different_arguments_produce_different_results(self, seed):
        first = self.execute(
            self.args_schema(batching=True),
            '{ users { name postsMatching(title: "Hello World") { title } } }',
        )
        second = self.execute(
            self.args_schema(batching=True),
            '{ users { name postsMatching(title: "Draft Post") { title } } }',
        )
        assert first.data != second.data

    def test_string_primary_keys_group_correctly(self, custom_pk_seed):
        """The parent key is read back off each row, so a non-integer key must
        round-trip through grouping intact."""
        query = "{ publishers { name books { title } } }"
        batched = self.execute(self.custom_pk_schema(batching=True), query)
        per_row = self.execute(self.custom_pk_schema(batching=False), query)

        assert batched.errors is None, batched.errors
        assert batched.data == per_row.data

        by_name = {p["name"]: p for p in batched.data["publishers"]}
        assert [b["title"] for b in by_name["Penguin"]["books"]] == ["Dune"]
        assert [b["title"] for b in by_name["Ace Books"]["books"]] == ["Neuromancer"]

    def test_string_primary_keys_are_batched(self, custom_pk_seed):
        query = "{ publishers { name books { title } } }"
        batched = self.count_queries(self.custom_pk_schema(batching=True), query)
        per_row = self.count_queries(self.custom_pk_schema(batching=False), query)
        assert batched < per_row

    def test_self_referential_relation(self, seed):
        """Parent and child are the same table, so the rewrite must still keep
        each parent's own children."""
        query = "{ comments { body replies { body } } }"
        batched = self.execute(self.self_ref_schema(batching=True), query)
        per_row = self.execute(self.self_ref_schema(batching=False), query)

        assert batched.errors is None, batched.errors
        assert batched.data == per_row.data

    def test_relation_reached_through_a_join_bails(self, seed):
        """A joined predicate has no key column on the row to group by."""
        query = "{ users { name taggedPosts { title } } }"
        batched = self.execute(self.join_schema(batching=True), query)
        per_row = self.execute(self.join_schema(batching=False), query)

        assert batched.errors is None, batched.errors
        assert batched.data == per_row.data
        assert self.count_queries(
            self.join_schema(batching=True), query
        ) == self.count_queries(self.join_schema(batching=False), query)

    def test_joined_predicate_does_not_reassign_rows_across_parents(self, seed):
        """The case that makes a joined rewrite visibly wrong.

        Once two authors share a tag, ``tags__posts__author=self`` returns rows
        whose own ``author_id`` is *not* the parent that matched them. Grouping
        by that column would hand Alice's row to Bob, so this must not batch.
        """
        self.share_a_tag_across_authors()

        query = "{ users { name taggedPosts { title } } }"
        batched = self.execute(self.join_schema(batching=True), query)
        per_row = self.execute(self.join_schema(batching=False), query)

        assert batched.errors is None, batched.errors
        assert batched.data == per_row.data

        by_name = {
            user["name"]: sorted(p["title"] for p in user["taggedPosts"])
            for user in batched.data["users"]
        }
        # Both authors see the shared-tag post, not one each.
        assert "Bob shares python" in by_name["Alice"]
        assert "Bob shares python" in by_name["Bob"]

    def test_same_relation_under_two_paths_is_cached_separately(self, seed):
        query = """
        {
          users { name posts { title } }
          otherUsers: users { name posts { title } }
        }
        """
        result = self.execute(self.two_path_schema(batching=True), query)
        assert result.errors is None, result.errors
        assert result.data["users"] == result.data["otherUsers"]

    def test_duplicate_parent_instances_all_receive_rows(self, seed):
        """The same instance can legitimately appear twice in a parent list."""
        query = "{ duplicatedUsers { name posts { title } } }"
        batched = self.execute(self.duplicate_schema(batching=True), query)
        per_row = self.execute(self.duplicate_schema(batching=False), query)

        assert batched.errors is None, batched.errors
        assert batched.data == per_row.data
        names = [u["name"] for u in batched.data["duplicatedUsers"]]
        assert len(names) != len(set(names))
        for user in batched.data["duplicatedUsers"]:
            assert user["posts"] is not None

    def test_empty_parent_list_is_harmless(self, seed):
        result = self.execute(self.empty_parents_schema(batching=True), USERS_QUERY)
        assert result.errors is None
        assert result.data["users"] == []
