"""Differential tests: batched relation resolution must equal per-row resolution.

Batching rewrites a resolver's query, and the failure mode of a bad rewrite is
silently wrong rows rather than an exception. So every resolver shape is run
twice - once batched, once forced down the per-row path - and the two results
must be identical. Shapes that cannot be rewritten safely must *bail*, which is
asserted by statement count rather than by output alone.
"""

import random

import strawberry

from strawberry_orm.types import auto

# Shapes whose remainder is identical across parents, so they collapse into
# one statement per distinct branch.
BATCHABLE_SHAPES = [
    "plain",
    "filtered",
    "branching",
    "ordered",
    "excluded",
    # A negation that does not mention the parent still rewrites cleanly.
    "negated",
]

# Correct, but each parent contributes its own literal, so every parent is its
# own shape and the query count does not drop.
PER_PARENT_SHAPES = ["per_parent_value"]

BAIL_SHAPES = [
    # The parent key sits inside an OR arm, so it cannot become an IN clause.
    "or_clause",
    # Per-parent LIMIT needs a window function, not an IN clause.
    "sliced",
    # The resolver executed its own query; there is nothing left to rewrite.
    "materialized",
]


QUERY = "{ users { name posts { title } } }"


class AbstractTestBatchingDifferential:
    """Subclasses provide ``build_schema``, ``execute`` and ``count_queries``."""

    def _run(self, shape, *, batching):
        schema = self.build_schema(shape, batching=batching)
        result = self.execute(schema, QUERY)
        assert result.errors is None, result.errors
        return result.data

    def test_batched_output_matches_per_row_output(self, seed):
        for shape in BATCHABLE_SHAPES + PER_PARENT_SHAPES + BAIL_SHAPES:
            batched = self._run(shape, batching=True)
            per_row = self._run(shape, batching=False)
            assert batched == per_row, f"{shape}: batched output diverged"

    def test_batchable_shapes_use_fewer_queries_than_per_row(self, seed):
        for shape in BATCHABLE_SHAPES:
            batched = self.count_queries(self.build_schema(shape, batching=True), QUERY)
            per_row = self.count_queries(
                self.build_schema(shape, batching=False), QUERY
            )
            assert batched < per_row, (
                f"{shape}: batching did not reduce queries ({batched} vs {per_row})"
            )

    def test_batched_query_count_does_not_grow_with_parents(self, seed, extra_users):
        """The point of batching: statements track shapes, not row count."""
        for shape in ("plain", "filtered", "ordered"):
            schema = self.build_schema(shape, batching=True)
            before = self.count_queries(schema, QUERY)

            extra_users(3)
            after = self.count_queries(self.build_schema(shape, batching=True), QUERY)

            assert after == before, f"{shape}: {before} -> {after} with more parents"

    def test_branching_resolver_makes_one_query_per_branch(self, seed):
        one_shape = self.count_queries(self.build_schema("plain", batching=True), QUERY)
        two_shapes = self.count_queries(
            self.build_schema("branching", batching=True), QUERY
        )
        assert two_shapes == one_shape + 1

    def test_randomized_shape_combinations_match_per_row(self, seed):
        """Fuzz the resolver shape rather than trusting a hand-picked list.

        Each case builds a resolver from a random combination of predicates,
        ordering and branching, then asserts the batched response is identical
        to the unbatched one. A rewrite that is wrong for some combination we
        did not think of shows up here.
        """
        random.seed(20260813)
        for _ in range(25):
            spec = {
                "published_only": random.choice([True, False]),
                "exclude_draft": random.choice([True, False]),
                "order": random.choice([None, "title", "-title", "id"]),
                "branch_on_name": random.choice([True, False]),
                "extra_predicate": random.choice([None, "id_gt_0", "title_not_null"]),
            }
            batched = self.execute(self.random_schema(spec, batching=True), QUERY)
            per_row = self.execute(self.random_schema(spec, batching=False), QUERY)
            assert batched.errors is None, (spec, batched.errors)
            assert per_row.errors is None, (spec, per_row.errors)
            assert batched.data == per_row.data, spec

    def test_three_level_nesting_batches_at_every_level(self, seed):
        query = "{ users { name posts { title comments { body } } } }"
        batched_schema = self.nested_schema(batching=True)
        per_row_schema = self.nested_schema(batching=False)

        batched = self.execute(batched_schema, query)
        per_row = self.execute(per_row_schema, query)
        assert batched.errors is None, batched.errors
        assert batched.data == per_row.data

        assert self.count_queries(batched_schema, query) < self.count_queries(
            per_row_schema, query
        )

    def test_bail_shapes_fall_back_to_per_row(self, seed):
        for shape in BAIL_SHAPES:
            batched = self.count_queries(self.build_schema(shape, batching=True), QUERY)
            per_row = self.count_queries(
                self.build_schema(shape, batching=False), QUERY
            )
            assert batched == per_row, f"{shape}: should not have been rewritten"


def build_user_post_types(orm, User, Post, resolver):
    @orm.type(Post)
    class PT:
        id: auto
        title: auto

    @orm.type(User)
    class UT:
        id: auto
        name: auto

        @strawberry.field
        def posts(self) -> list[PT]:
            return resolver(self)  # type: ignore[return-value]

    return UT
