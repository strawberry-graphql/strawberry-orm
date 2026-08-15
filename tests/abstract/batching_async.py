"""Batching under async execution.

Run-ahead only works on a resolver that hands back an unexecuted query. An
async resolver returns a coroutine instead, which cannot be inspected without
awaiting it, so batching declines. What matters is that declining is *safe*:
the response must be byte-for-byte what the unbatched schema produces.
"""

QUERY = "{ users { name posts { title } } }"


class AbstractTestBatchingAsync:
    """Subclasses provide ``schema_for`` and ``execute_async``."""

    async def test_sync_resolver_batches_under_async_execution(self, seed):
        batched = await self.execute_async(self.schema_for(batching=True), QUERY)
        per_row = await self.execute_async(self.schema_for(batching=False), QUERY)

        assert batched.errors is None, batched.errors
        assert batched.data == per_row.data

    async def test_async_resolver_falls_back_safely(self, seed):
        batched = await self.execute_async(
            self.schema_for(batching=True, async_resolver=True), QUERY
        )
        per_row = await self.execute_async(
            self.schema_for(batching=False, async_resolver=True), QUERY
        )

        assert batched.errors is None, batched.errors
        assert batched.data == per_row.data

    async def test_async_batched_result_has_the_expected_rows(self, seed):
        """Pinned explicitly rather than against a sync run: under async
        execution Django wraps resolvers so ``execute_sync`` cannot be used
        from inside the event loop."""
        result = await self.execute_async(self.schema_for(batching=True), QUERY)

        assert result.errors is None, result.errors
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [{"title": "Hello World"}, {"title": "GraphQL Guide"}],
                },
                {"name": "Bob", "posts": [{"title": "Draft Post"}]},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }
