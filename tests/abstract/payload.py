"""``orm.payload`` - resolvers that answer with ``data`` and ``errors``.

Backends supply ``payload_schemas`` (a builder for the three field kinds) and
``payload_execute``. The behaviour under test is backend-neutral: what differs
is only how each backend spells a queryset.
"""

import pytest
import strawberry

from strawberry_orm import PayloadPolicy


@strawberry.type
class Errors:
    """Stand-in for an application's error type."""

    message: str


class Denied(Exception):
    """Handled by the policy under test."""


class Unhandled(Exception):
    """Deliberately outside ``handles``."""


class Passthrough(Exception):
    """``on_error`` re-raises this rather than converting it."""


def _on_error(exc):
    """Convert what we recognise; re-raise the rest.

    Mirrors the usual shape of a real converter, which maps a known set of
    application errors and lets anything unexpected stay a GraphQL error.
    """
    if isinstance(exc, Passthrough):
        raise exc
    return Errors(message=str(exc))


def policy(**overrides):
    return PayloadPolicy(errors=Errors, on_error=_on_error, **overrides)


ALL_NAMES = ["Alice", "Bob", "Charlie"]


class AbstractTestPayload:
    """``build(kind, ...)`` returns a schema; ``execute(schema, query)`` runs it."""

    #: Rows plus one relation. SQLAlchemy re-selects the rows to attach the
    #: eager load; the others fill the caches in place.
    max_payload_queries = 2

    # -- the happy path ------------------------------------------------------

    def test_data_carries_the_rows_and_errors_is_null(self, seed, payload, execute):
        result = execute(
            payload("query"),
            "{ users { data { name } errors { message } } }",
        )
        assert result.errors is None, result.errors
        assert result.data["users"]["errors"] is None
        assert [row["name"] for row in result.data["users"]["data"]] == ALL_NAMES

    def test_the_payload_type_is_named_after_the_resolver(self, seed, payload):
        assert "UsersPayload" in str(payload("query"))

    def test_relations_under_data_are_eager_loaded(self, seed, payload, execute):
        """``data`` is a resolved field, so the optimizer sees the rows.

        Nothing in the payload machinery optimizes anything - this passes
        because the rows reach the optimizer with their own selection.
        """
        result, queries = execute(
            payload("query"),
            "{ users { data { name posts { title } } } }",
            count=True,
        )
        assert result.errors is None, result.errors
        assert queries <= self.max_payload_queries, (
            f"{queries} queries suggests the rows under data were not optimized"
        )

    # -- failure -------------------------------------------------------------

    def test_a_handled_failure_becomes_errors(self, seed, payload, execute):
        result = execute(
            payload("query", fail=Denied("not for you")),
            "{ users { data { name } errors { message } } }",
        )
        assert result.errors is None, result.errors
        assert result.data["users"] == {
            "data": None,
            "errors": {"message": "not for you"},
        }

    def test_an_unhandled_failure_stays_a_graphql_error(self, seed, payload, execute):
        """``handles`` decides what is converted; the rest keeps its traceback."""
        result = execute(
            payload("query", fail=Unhandled("boom"), handles=(Denied,)),
            "{ users { data { name } errors { message } } }",
        )
        assert result.errors is not None
        assert "boom" in str(result.errors[0].message)

    def test_on_error_may_reraise_to_keep_a_graphql_error(self, seed, payload, execute):
        """Re-raising from ``on_error`` opts a failure back out of the payload.

        A converter that only knows some of your errors needs a way to let the
        rest through, so anything it re-raises fails the field as it normally
        would.
        """
        result = execute(
            payload("query", fail=Passthrough("kaboom")),
            "{ users { data { name } errors { message } } }",
        )
        assert result.errors is not None
        assert "kaboom" in str(result.errors[0].message)
        assert result.data is None or result.data.get("users") is None

    # -- mutations -----------------------------------------------------------

    def test_a_mutation_wraps_its_result(self, seed, payload, execute):
        result = execute(
            payload("mutation"),
            'mutation { users(name: "Zed") { data { name } errors { message } } }',
            operation="mutation",
        )
        assert result.errors is None, result.errors
        assert result.data["users"]["data"]["name"] == "Zed"

    def test_a_failing_mutation_reports_errors(self, seed, payload, execute):
        result = execute(
            payload("mutation", fail=Denied("read only")),
            'mutation { users(name: "Zed") { data { name } errors { message } } }',
            operation="mutation",
        )
        assert result.errors is None, result.errors
        assert result.data["users"] == {
            "data": None,
            "errors": {"message": "read only"},
        }

    # -- connections ---------------------------------------------------------

    def test_a_connection_payload_paginates(self, seed, payload, execute):
        result = execute(
            payload("connection"),
            """
            {
              users {
                errors { message }
                data(first: 2) {
                  totalCount
                  edges { node { name } }
                  pageInfo { hasNextPage }
                }
              }
            }
            """,
        )
        assert result.errors is None, result.errors
        conn = result.data["users"]["data"]
        assert conn["totalCount"] == len(ALL_NAMES)
        assert [edge["node"]["name"] for edge in conn["edges"]] == ALL_NAMES[:2]
        assert conn["pageInfo"]["hasNextPage"] is True

    def test_a_failing_connection_yields_an_empty_one(self, seed, payload, execute):
        """``data`` stays a connection so the client renders the same shape."""
        result = execute(
            payload("connection", fail=Denied("nope")),
            """
            {
              users {
                errors { message }
                data(first: 2) { totalCount edges { node { name } } }
              }
            }
            """,
        )
        assert result.errors is None, result.errors
        assert result.data["users"]["errors"] == {"message": "nope"}
        assert result.data["users"]["data"]["edges"] == []

    def test_a_connection_type_is_derived_from_the_annotation(
        self, seed, payload, execute
    ):
        """Naming the node type is enough; the connection follows from it."""
        result = execute(
            payload("connection", derive=True),
            "{ users { data(first: 2) { totalCount edges { node { name } } } } }",
        )
        assert result.errors is None, result.errors
        conn = result.data["users"]["data"]
        assert conn["totalCount"] == len(ALL_NAMES)
        assert [edge["node"]["name"] for edge in conn["edges"]] == ALL_NAMES[:2]

    def test_a_string_annotation_is_resolved_through_the_policy(
        self, seed, payload, execute
    ):
        """``PayloadPolicy.types`` says where a named type lives.

        A resolver can then name a type its own module never imported, which
        is what a schema with one types module and many resolver modules needs.
        """
        result = execute(
            payload("query", by_name=True),
            "{ users { data { name } errors { message } } }",
        )
        assert result.errors is None, result.errors
        assert [row["name"] for row in result.data["users"]["data"]] == ALL_NAMES

    # -- misuse --------------------------------------------------------------

    def test_the_payload_name_can_be_overridden(self, orm_with_policy):
        @strawberry.type
        class Root:
            @orm_with_policy.payload.query(name="ChosenName")
            def users(self) -> list[str]:
                return []  # pragma: no cover - only the schema is inspected

        assert "ChosenName" in str(strawberry.Schema(query=Root))

    def test_an_input_mutation_collapses_its_arguments(self, orm_with_policy):
        @strawberry.type
        class Empty:
            ok: bool = True

        @strawberry.type
        class Mutate:
            @orm_with_policy.payload.mutation(input_mutation=True)
            def rename(self, name: str) -> str:
                return name  # pragma: no cover - only the schema is inspected

        assert "RenameInput" in str(strawberry.Schema(query=Empty, mutation=Mutate))

    def test_a_resolver_without_a_return_annotation_is_rejected(self, orm_with_policy):
        with pytest.raises(TypeError, match="needs a return annotation"):

            @orm_with_policy.payload.query
            def users(self):  # no annotation to build the payload from
                return []

    def test_a_connection_without_a_node_type_is_rejected(self, orm_with_policy):
        """With no connection type and no annotation there is nothing to build."""
        with pytest.raises(TypeError, match="needs a return annotation naming"):

            @orm_with_policy.payload.connection()
            def users(self):
                return []  # pragma: no cover - never built

    def test_using_payload_without_a_policy_is_rejected(self, orm):
        with pytest.raises(TypeError, match="needs a PayloadPolicy"):

            @orm.payload.query
            def users(self) -> list[str]:
                return []  # pragma: no cover - never built


class AbstractTestPayloadAsync(AbstractTestPayload):
    """Async backends await execution."""

    async def test_data_carries_the_rows_and_errors_is_null(
        self, seed, payload, execute
    ):
        result = await execute(
            payload("query"),
            "{ users { data { name } errors { message } } }",
        )
        assert result.errors is None, result.errors
        assert result.data["users"]["errors"] is None
        assert [row["name"] for row in result.data["users"]["data"]] == ALL_NAMES

    async def test_relations_under_data_are_eager_loaded(self, seed, payload, execute):
        result, queries = await execute(
            payload("query"),
            "{ users { data { name posts { title } } } }",
            count=True,
        )
        assert result.errors is None, result.errors
        assert queries <= self.max_payload_queries

    async def test_a_handled_failure_becomes_errors(self, seed, payload, execute):
        result = await execute(
            payload("query", fail=Denied("not for you")),
            "{ users { data { name } errors { message } } }",
        )
        assert result.errors is None, result.errors
        assert result.data["users"] == {
            "data": None,
            "errors": {"message": "not for you"},
        }

    async def test_an_unhandled_failure_stays_a_graphql_error(
        self, seed, payload, execute
    ):
        result = await execute(
            payload("query", fail=Unhandled("boom"), handles=(Denied,)),
            "{ users { data { name } errors { message } } }",
        )
        assert result.errors is not None
        assert "boom" in str(result.errors[0].message)

    async def test_on_error_may_reraise_to_keep_a_graphql_error(
        self, seed, payload, execute
    ):
        result = await execute(
            payload("query", fail=Passthrough("kaboom")),
            "{ users { data { name } errors { message } } }",
        )
        assert result.errors is not None
        assert "kaboom" in str(result.errors[0].message)
        assert result.data is None or result.data.get("users") is None

    async def test_a_mutation_wraps_its_result(self, seed, payload, execute):
        result = await execute(
            payload("mutation"),
            'mutation { users(name: "Zed") { data { name } errors { message } } }',
            operation="mutation",
        )
        assert result.errors is None, result.errors
        assert result.data["users"]["data"]["name"] == "Zed"

    async def test_a_failing_mutation_reports_errors(self, seed, payload, execute):
        result = await execute(
            payload("mutation", fail=Denied("read only")),
            'mutation { users(name: "Zed") { data { name } errors { message } } }',
            operation="mutation",
        )
        assert result.errors is None, result.errors
        assert result.data["users"] == {
            "data": None,
            "errors": {"message": "read only"},
        }

    async def test_a_connection_type_is_derived_from_the_annotation(
        self, seed, payload, execute
    ):
        result = await execute(
            payload("connection", derive=True),
            "{ users { data(first: 2) { totalCount edges { node { name } } } } }",
        )
        assert result.errors is None, result.errors
        conn = result.data["users"]["data"]
        assert conn["totalCount"] == len(ALL_NAMES)

    async def test_a_string_annotation_is_resolved_through_the_policy(
        self, seed, payload, execute
    ):
        result = await execute(
            payload("query", by_name=True),
            "{ users { data { name } errors { message } } }",
        )
        assert result.errors is None, result.errors
        assert [row["name"] for row in result.data["users"]["data"]] == ALL_NAMES

    async def test_a_connection_payload_paginates(self, seed, payload, execute):
        result = await execute(
            payload("connection"),
            """
            {
              users {
                errors { message }
                data(first: 2) {
                  totalCount
                  edges { node { name } }
                  pageInfo { hasNextPage }
                }
              }
            }
            """,
        )
        assert result.errors is None, result.errors
        conn = result.data["users"]["data"]
        assert conn["totalCount"] == len(ALL_NAMES)
        assert [edge["node"]["name"] for edge in conn["edges"]] == ALL_NAMES[:2]
        assert conn["pageInfo"]["hasNextPage"] is True

    async def test_a_failing_connection_yields_an_empty_one(
        self, seed, payload, execute
    ):
        result = await execute(
            payload("connection", fail=Denied("nope")),
            """
            {
              users {
                errors { message }
                data(first: 2) { totalCount edges { node { name } } }
              }
            }
            """,
        )
        assert result.errors is None, result.errors
        assert result.data["users"]["errors"] == {"message": "nope"}
        assert result.data["users"]["data"]["edges"] == []
