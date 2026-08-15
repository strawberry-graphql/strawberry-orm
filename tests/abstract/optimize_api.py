"""``orm.optimize`` - eager-loading rows a resolver has already materialized.

The optimizer intercepts a query object on its way out of a resolver. A
resolver that returns instances instead - a mutation result, or rows tucked
inside a payload - misses that hand-off, and every relation the query asked for
becomes its own round trip.

``orm.optimize`` is the way back onto the optimized path. These tests cover the
three properties that make it usable: it returns the same rows as the
unoptimized version, it collapses the round trips, and it does so without
either dropping row scoping or overwriting scalar values held in memory.
"""

PAYLOAD_QUERY = """
    {
      users {
        errors
        data { name posts { title } }
      }
    }
"""


def build_selection_info(query: str = PAYLOAD_QUERY):
    """A minimal ``info`` for the pass-through cases, which never reach a backend."""
    from graphql import parse
    from graphql.language.ast import FieldNode

    class _RawInfo:
        def __init__(self, field_nodes):
            self.field_nodes = field_nodes
            self.fragments = {}

    class _Info:
        def __init__(self, raw):
            self._raw_info = raw
            self.context = {}

    operation = parse(query).definitions[0]
    return _Info(
        _RawInfo(
            [
                node
                for node in operation.selection_set.selections
                if isinstance(node, FieldNode)
            ]
        )
    )


ALL_POSTS = {
    "errors": None,
    "data": [
        {
            "name": "Alice",
            "posts": [{"title": "Hello World"}, {"title": "GraphQL Guide"}],
        },
        {"name": "Bob", "posts": [{"title": "Draft Post"}]},
        {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
    ],
}

PUBLISHED_POSTS = {
    "errors": None,
    "data": [
        {
            "name": "Alice",
            "posts": [{"title": "Hello World"}, {"title": "GraphQL Guide"}],
        },
        {"name": "Bob", "posts": []},
        {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
    ],
}


ALICE_ONLY = {
    "errors": None,
    "data": {
        "name": "Alice",
        "posts": [{"title": "Hello World"}, {"title": "GraphQL Guide"}],
    },
}


class AbstractTestOptimizeAPI:
    """Backends supply ``run_payload`` and ``run_dirty_scalar`` fixtures.

    ``run_payload(optimize=..., scoped=..., at=..., shape=...)`` executes
    ``PAYLOAD_QUERY`` against a resolver whose ``data`` is a list of rows
    (``shape="list"``), a single row (``"one"``), or an unexecuted query
    (``"query"``), and returns ``(data, query_count)``.
    """

    #: Backends differ in how they load onto rows already in memory. Django and
    #: Tortoise fill the relation caches in place, so the rows plus one relation
    #: is two queries; SQLAlchemy re-selects the rows to attach the eager load.
    max_eager_queries = 2

    # -- the three shapes orm.optimize accepts -------------------------------

    def test_a_single_instance_is_optimized(self, seed, run_payload):
        data, _ = run_payload(optimize=True, shape="one")
        assert data == ALICE_ONLY

    def test_a_query_object_is_optimized_and_materialized(self, seed, run_payload):
        """A query object still goes through the ordinary optimizer path."""
        optimized, optimized_queries = run_payload(optimize=True, shape="query")
        plain, plain_queries = run_payload(optimize=False, shape="query")
        assert optimized == plain == ALL_POSTS
        assert optimized_queries == plain_queries, (
            "optimizing a query object twice should not cost an extra query"
        )

    # -- values it must not touch --------------------------------------------

    def test_none_is_returned_as_is(self, orm, selection_info):
        assert orm.optimize(None, selection_info) is None

    def test_missing_info_is_returned_as_is(self, orm):
        assert orm.optimize(["anything"], None) == ["anything"]

    def test_a_scalar_is_returned_as_is(self, orm, selection_info):
        assert orm.optimize("not a row", selection_info) == "not a row"

    def test_a_list_without_rows_is_returned_as_is(self, orm, selection_info):
        """Safe to wrap a payload whose ``data`` is not ORM rows at all."""
        payload = [1, "two", {"three": 3}]
        assert orm.optimize(payload, selection_info) is payload

    def test_a_model_class_is_not_mistaken_for_a_row(self, orm, Post):
        assert orm.backend.is_model_instance(Post) is False

    def test_optimized_and_unoptimized_agree(self, seed, run_payload):
        """Optimizing must not change the answer, only how it is fetched."""
        optimized, _ = run_payload(optimize=True)
        plain, _ = run_payload(optimize=False)
        assert optimized == plain == ALL_POSTS

    def test_rows_are_eager_loaded_without_an_explicit_call(self, seed, run_payload):
        """The optimizer reaches materialized rows on its own.

        ``data`` is a resolved field like any other, so the rows arrive at the
        optimizer with exactly the selection that describes them - no call
        needed. Two queries: the rows, then their relation.
        """
        _, plain_queries = run_payload(optimize=False)
        assert plain_queries <= self.max_eager_queries, (
            f"{plain_queries} queries for 3 rows and one relation suggests "
            f"the automatic pass did not run"
        )

    def test_an_explicit_call_costs_nothing_extra(self, seed, run_payload):
        """``orm.optimize`` on rows the optimizer already prepared is a no-op."""
        _, optimized_queries = run_payload(optimize=True)
        _, plain_queries = run_payload(optimize=False)
        assert optimized_queries == plain_queries

    def test_a_call_that_loads_nothing_does_not_suppress_the_automatic_pass(
        self, seed, run_payload
    ):
        """A wrong ``at`` must be no worse than never calling it.

        The rows sit under ``data``; pointed at the payload instead, the call
        finds no relations and loads nothing. Marking them regardless would
        skip the automatic pass on ``data`` and reinstate the per-row queries
        the call was meant to avoid.
        """
        _, misdirected = run_payload(optimize=True, at=None)
        _, plain = run_payload(optimize=False)
        assert misdirected == plain

    def test_relation_scoping_still_applies(self, seed, run_payload):
        """``scope_rows`` on the related type must survive this path.

        Loading relations onto instances is a second way into the same data.
        If it skipped scoping, a caller could read rows through a payload that
        they cannot read through an ordinary query.
        """
        scoped, _ = run_payload(optimize=True, scoped=True)
        assert scoped == PUBLISHED_POSTS

    def test_scoping_holds_whether_or_not_rows_are_optimized(self, seed, run_payload):
        """Scoping must not depend on whether the caller optimized.

        ``orm.optimize`` applies ``scope_rows``; plain lazy loading does not.
        That difference is a read-access leak, not a performance detail.
        """
        scoped_optimized, _ = run_payload(optimize=True, scoped=True)
        scoped_plain, _ = run_payload(optimize=False, scoped=True)
        assert scoped_optimized == scoped_plain

    def test_without_at_nothing_is_loaded(self, seed, run_payload):
        """Selections live under ``data``; pointed at the payload there are none.

        The rows still come back - just unoptimized - which is why ``at`` is
        worth getting right.
        """
        data, _ = run_payload(optimize=True, at=None)
        assert data == ALL_POSTS

    def test_in_memory_scalars_survive(self, seed, run_dirty_scalar):
        """A value set on an instance must outlive the relation load.

        This is the mutation case: the row in the database still holds the old
        title, and re-fetching over the top of it would report stale data as
        though the write had not happened.
        """
        assert run_dirty_scalar() == {
            "errors": None,
            "data": [{"name": "Alice renamed", "posts": [{"title": "Hello World"}]}],
        }


class AbstractTestOptimizeAPIAsync(AbstractTestOptimizeAPI):
    """Async backends (e.g. Tortoise) await execution.

    The pass-through cases short-circuit before reaching the backend, so they
    return plain values on every backend and are inherited unchanged.
    """

    async def test_a_single_instance_is_optimized(self, seed, run_payload):
        data, _ = await run_payload(optimize=True, shape="one")
        assert data == ALICE_ONLY

    async def test_a_query_object_is_optimized_and_materialized(
        self, seed, run_payload
    ):
        optimized, optimized_queries = await run_payload(optimize=True, shape="query")
        plain, plain_queries = await run_payload(optimize=False, shape="query")
        assert optimized == plain == ALL_POSTS
        assert optimized_queries == plain_queries

    async def test_optimized_and_unoptimized_agree(self, seed, run_payload):
        optimized, _ = await run_payload(optimize=True)
        plain, _ = await run_payload(optimize=False)
        assert optimized == plain == ALL_POSTS

    async def test_rows_are_eager_loaded_without_an_explicit_call(
        self, seed, run_payload
    ):
        _, plain_queries = await run_payload(optimize=False)
        assert plain_queries <= self.max_eager_queries, (
            f"{plain_queries} queries for 3 rows and one relation suggests "
            f"the automatic pass did not run"
        )

    async def test_an_explicit_call_costs_nothing_extra(self, seed, run_payload):
        _, optimized_queries = await run_payload(optimize=True)
        _, plain_queries = await run_payload(optimize=False)
        assert optimized_queries == plain_queries

    async def test_a_call_that_loads_nothing_does_not_suppress_the_automatic_pass(
        self, seed, run_payload
    ):
        _, misdirected = await run_payload(optimize=True, at=None)
        _, plain = await run_payload(optimize=False)
        assert misdirected == plain

    async def test_relation_scoping_still_applies(self, seed, run_payload):
        scoped, _ = await run_payload(optimize=True, scoped=True)
        assert scoped == PUBLISHED_POSTS

    async def test_scoping_holds_whether_or_not_rows_are_optimized(
        self, seed, run_payload
    ):
        scoped_optimized, _ = await run_payload(optimize=True, scoped=True)
        scoped_plain, _ = await run_payload(optimize=False, scoped=True)
        assert scoped_optimized == scoped_plain

    async def test_without_at_nothing_is_loaded(self, seed, run_payload):
        data, _ = await run_payload(optimize=True, at=None)
        assert data == ALL_POSTS

    async def test_in_memory_scalars_survive(self, seed, run_dirty_scalar):
        assert await run_dirty_scalar() == {
            "errors": None,
            "data": [{"name": "Alice renamed", "posts": [{"title": "Hello World"}]}],
        }
