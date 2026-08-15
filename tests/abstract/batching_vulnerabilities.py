"""Adversarial tests for the batch rewrite.

A wrong rewrite does not raise; it returns the wrong rows. Each test here is
built from a scenario where the natural fixture data would hide the failure,
so the assertion is only meaningful with the adversarial setup in place.
"""

QUERY_NESTED = "{ users { name posts { title comments { body } } } }"
QUERY_NESTED_SHALLOW = "{ users { name posts { title } } }"


class AbstractTestBatchingVulnerabilities:
    """Subclasses provide ``nested_schema``, ``execute`` and the seed helpers."""

    def test_nested_batches_do_not_drop_later_parent_groups(self, seed):
        """A depth-3 path is reached once per parent group.

        ``users.posts.comments`` is resolved separately for each user's posts.
        Caching that path by name alone made every group after the first read
        an empty batch, silently returning no comments. The stock fixture only
        gives Alice comments, which hides it entirely.
        """
        self.give_every_user_a_comment()

        batched = self.execute(self.nested_schema(batching=True), QUERY_NESTED)
        per_row = self.execute(self.nested_schema(batching=False), QUERY_NESTED)

        assert batched.errors is None, batched.errors
        assert batched.data == per_row.data

        bodies = {
            user["name"]: [
                c["body"] for post in user["posts"] for c in post["comments"]
            ]
            for user in batched.data["users"]
        }
        assert bodies["Bob"], "Bob's comments were dropped by batching"
        assert bodies["Charlie"], "Charlie's comments were dropped by batching"

    def test_splitting_does_not_corrupt_the_original_query(self, seed):
        """The remainder is built by clone-and-strip.

        If the parent predicate were stripped from the caller's own query
        instead of a copy, the bail path would hand back a query with no parent
        filter at all - every parent would see every row.
        """
        backend, original = self.parent_scoped_query()
        before = self.row_ids(original)

        split = backend.split_parent_predicate(original, 1)
        assert split is not None

        assert self.row_ids(original) == before
        # And the remainder really did lose the parent filter.
        assert set(self.row_ids(split[2])) > set(before)

    def test_context_scoped_scope_rows_is_applied_per_batch(self, seed):
        """Row scoping that reads ``info.context`` must survive the rewrite."""
        for tenant, expected in self.tenant_expectations():
            batched = self.execute_with_context(
                self.tenant_schema(batching=True), QUERY_NESTED_SHALLOW, tenant
            )
            per_row = self.execute_with_context(
                self.tenant_schema(batching=False), QUERY_NESTED_SHALLOW, tenant
            )
            assert batched.errors is None, batched.errors
            assert batched.data == per_row.data

            titles = {
                post["title"]
                for user in batched.data["users"]
                for post in user["posts"]
            }
            assert titles == expected, (tenant, titles)

    def test_every_parent_group_is_represented(self, seed):
        self.give_every_user_a_comment()
        result = self.execute(self.nested_schema(batching=True), QUERY_NESTED)

        assert result.errors is None
        for user in result.data["users"]:
            for post in user["posts"]:
                assert post["comments"] is not None
