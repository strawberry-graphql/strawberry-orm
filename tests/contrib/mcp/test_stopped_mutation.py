"""A mutation that SynchronousOnlyOperation stops part-way is not run again,
and its result says which of its root fields had already run."""

from __future__ import annotations

import pytest
import strawberry
from django.core.exceptions import SynchronousOnlyOperation
from graphql import parse
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from strawberry.types.graphql import OperationType

from strawberry_orm.contrib.mcp import GraphQLTool, mcp_server
from strawberry_orm.contrib.mcp.execution import Call, Executor, Target

LEDGER: list[int] = []


@strawberry.type
class Query:
    ok: bool = True


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def charge_card(self, cents: int) -> bool:
        LEDGER.append(cents)
        return True

    @strawberry.mutation
    def write_receipt(self) -> bool:
        raise SynchronousOnlyOperation("You cannot call this from an async context")

    @strawberry.mutation
    def maybe_write_receipt(self) -> bool | None:
        raise SynchronousOnlyOperation("You cannot call this from an async context")


checkout = strawberry.Schema(query=Query, mutation=Mutation)

STOPPED = (
    "This mutation needs sync execution: SynchronousOnlyOperation stopped "
    "{stopped}{after}, and a mutation is not run a second time. Pass sync=True "
    "to mcp_server."
)


@pytest.fixture(autouse=True)
def _empty_ledger():
    LEDGER.clear()


async def test_a_stopped_mutation_says_which_root_fields_already_ran():
    server = mcp_server(
        checkout, operations="mutation Checkout { chargeCard(cents: 500) writeReceipt }"
    )

    async with Client(server) as client:
        first = await client.call_tool("Checkout", {})

    assert LEDGER == [500]
    assert "after chargeCard had run" in first.content[0].text
    assert first.is_error is True
    assert first.structured_content["data"] is None
    raised, hint = first.structured_content["errors"]
    assert raised["path"] == ["writeReceipt"]
    assert hint == {
        "message": STOPPED.format(
            stopped="writeReceipt", after=", after chargeCard had run"
        )
    }


async def test_a_stopped_mutation_returns_its_partial_data():
    server = mcp_server(
        checkout,
        operations="mutation Checkout { chargeCard(cents: 7) maybeWriteReceipt }",
    )

    async with Client(server) as client:
        result = await client.call_tool("Checkout", {})

    assert result.is_error is False
    assert result.structured_content["data"] == {
        "chargeCard": True,
        "maybeWriteReceipt": None,
    }
    assert result.structured_content["errors"][-1]["message"] == STOPPED.format(
        stopped="maybeWriteReceipt", after=", after chargeCard had run"
    )
    assert LEDGER == [7]


async def test_the_root_fields_named_are_those_that_ran():
    query = """
    mutation($pay: Boolean! = true, $twice: Boolean!) {
      skipped: chargeCard(cents: 1) @skip(if: true)
      paid: chargeCard(cents: 2) @include(if: $pay)
      ... on Mutation { again: chargeCard(cents: 3) @include(if: $twice) }
      paid: chargeCard(cents: 2)
      ...Receipt
      ...Receipt
    }
    fragment Receipt on Mutation { tip: chargeCard(cents: 4) writeReceipt }
    """
    server = mcp_server(checkout, graphql_tool=GraphQLTool(allow_mutations=True))

    async with Client(server) as client:
        result = await client.call_tool(
            "graphql", {"query": query, "variables": {"twice": False}}
        )

    assert LEDGER == [2, 4]
    assert result.structured_content["errors"][-1]["message"] == STOPPED.format(
        stopped="writeReceipt", after=", after paid, tip had run"
    )


async def test_a_mutation_stopped_at_its_first_field_names_only_that_field():
    server = mcp_server(checkout, operations="mutation Receipt { writeReceipt }")

    async with Client(server) as client:
        result = await client.call_tool("Receipt", {})

    assert result.structured_content["errors"][-1]["message"] == STOPPED.format(
        stopped="writeReceipt", after=""
    )


class _Raising:
    async def execute(self, *args, **kwargs):
        raise SynchronousOnlyOperation("async unsafe")


async def test_a_mutation_whose_execute_raised_is_not_run_again():
    executor = Executor(sync=None, execute=None)
    query = "mutation { writeReceipt }"
    call = Call(query, parse(query), OperationType.MUTATION, {}, Target(_Raising()))

    with pytest.raises(ToolError, match="a mutation is not run a second time"):
        await executor.run(call, None)

    assert not executor.sync_execute
