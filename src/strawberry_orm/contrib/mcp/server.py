"""Serve a :class:`QueryAgent` as an MCP server with one ``ask`` tool."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import NoBackChannelError
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    ToolAnnotations,
)
from mcp_types.version import is_version_at_least

from .agent import PAUSE, QueryAgent, QueryAgentError
from .clarification import Clarification

_INPUT_REQUIRED_VERSION = "2026-07-28"
_REQUEST_KEY = "clarification"


def mcp_server(
    agent: QueryAgent,
    *,
    context_getter: Callable[[Any], Any] | None = None,
    name: str = "graphql",
    description: str | None = None,
    **server_options: Any,
) -> MCPServer:
    """An MCP server whose only tool, ``ask``, answers through ``agent``.

    Clients see one small tool instead of the schema. ``context_getter``
    receives the HTTP request (``None`` over stdio) and returns the GraphQL
    context, sync or async; this is where a bearer token becomes your user.
    It defaults to ``{"request": request}``.

    When the agent needs something from the person asking, the server asks
    with an elicitation form if the client supports one: inside the call on
    older protocol versions, and by returning ``input_required`` from
    2026-07-28, with the agent's progress in the sealed request state.
    Clients without forms get ``{"needs_input": ...}`` as the result, to ask
    their user and call again; a change that needs confirming is refused.

    ``server_options`` go to :class:`mcp.server.mcpserver.MCPServer`, for
    example ``token_verifier``, ``auth`` and ``request_state_security``,
    which multi-process deployments need so every process can read the
    state another one sealed.
    """
    server = MCPServer(name, **server_options)
    if description is None:
        verb = "read and change" if agent.allow_mutations else "read"
        description = (
            "Ask a question about the application's data in plain language. "
            "An agent on the server writes a GraphQL query, runs it with your "
            "permissions and returns the query with its result. It can "
            f"{verb}: {', '.join(agent.root_fields)}."
        )

    @server.tool(
        name="ask",
        description=description,
        annotations=ToolAnnotations(
            read_only_hint=not agent.allow_mutations, open_world_hint=False
        ),
    )
    async def ask(question: str, ctx: Context) -> dict[str, Any] | InputRequiredResult:
        request = ctx.request_context.request
        if context_getter is None:
            context = {"request": request}
        else:
            context = context_getter(request)
            if inspect.isawaitable(context):
                context = await context
        forms = _supports_forms(ctx)
        resumable = forms and is_version_at_least(
            ctx.protocol_version or "", _INPUT_REQUIRED_VERSION
        )
        clarifier = None if resumable or not forms else _elicit_in_call(ctx)
        try:
            if ctx.request_state is not None:
                answers = _answers((ctx.input_responses or {}).get(_REQUEST_KEY))
                result = await agent.resume(
                    json.loads(ctx.request_state), answers, context=context
                )
            else:
                result = await agent.ask(question, context=context, clarifier=clarifier)
        except QueryAgentError as error:
            detail = {"error": str(error), "query": error.query, "errors": error.errors}
            raise ToolError(json.dumps(detail)) from error
        if "needs_input" not in result:
            return result
        needs_input = result["needs_input"]
        if resumable:
            return InputRequiredResult(
                input_requests={_REQUEST_KEY: _elicit_request(needs_input)},
                request_state=json.dumps(result["state"]),
            )
        if needs_input["kind"] == "confirm":
            raise ToolError(
                json.dumps(
                    {
                        "error": "This change needs confirming, and this client "
                        "cannot show a confirmation.",
                        "query": needs_input["query"],
                        "errors": [],
                    }
                )
            )
        return {"needs_input": needs_input}

    return server


def _supports_forms(ctx: Context) -> bool:
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities else None
    if elicitation is None:
        return False
    return elicitation.form is not None or elicitation.url is None


def _elicit_in_call(ctx: Context):
    async def clarifier(clarification: Clarification) -> dict[str, Any] | None:
        try:
            result = await ctx.session.elicit_form(
                clarification.message,
                clarification.schema,
                related_request_id=ctx.request_id,
            )
        except NoBackChannelError:
            return PAUSE
        return _answers(result)

    return clarifier


def _elicit_request(needs_input: dict[str, Any]) -> ElicitRequest:
    return ElicitRequest(
        params=ElicitRequestFormParams(
            message=needs_input["message"], requested_schema=needs_input["schema"]
        )
    )


def _answers(result: object) -> dict[str, Any] | None:
    """What the person entered, or ``None`` unless they accepted a form.

    A client may answer an input request with another kind of result.
    """
    if not isinstance(result, ElicitResult) or result.action != "accept":
        return None
    return dict(result.content or {})
