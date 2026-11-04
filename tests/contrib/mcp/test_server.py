"""The MCP server: one ask tool, and clarifications in whatever form the client can show."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.shared.exceptions import NoBackChannelError
from mcp.types import ElicitResult, ListRootsResult

from strawberry_orm.contrib.mcp import PAUSE, Clarification, QueryAgent, mcp_server
from strawberry_orm.contrib.mcp.server import (
    _answers,
    _elicit_in_call,
    _supports_forms,
)

from .fakes import PUBLISH, Model, agent, ask_user, call, reply, schema

POSTS_QUERY = '{ posts(title: "Hello") { title } }'
HELLO = {"posts": [{"title": "Hello"}]}


def _form(*responses):
    """An elicitation callback that answers with ``responses`` in turn."""
    shown = []

    async def callback(context, params):
        shown.append(params)
        return responses[len(shown) - 1]

    callback.shown = shown
    return callback


def _detail(result):
    return json.loads(result.content[0].text.split(": ", 1)[1])


async def test_the_server_offers_one_read_only_ask_tool():
    qa, _ = agent(reply(call(POSTS_QUERY)))

    async with Client(mcp_server(qa)) as client:
        [tool] = (await client.list_tools()).tools
        result = await client.call_tool("ask", {"question": "Posts?"})

    assert tool.name == "ask"
    assert tool.annotations.read_only_hint is True
    assert "It can read: posts, named, post." in tool.description
    assert result.is_error is False
    assert result.structured_content["data"] == HELLO


async def test_the_server_builds_the_context_from_the_request():
    contexts = []

    def sync_getter(request):
        contexts.append({"user": "sync", "request": request})
        return contexts[-1]

    async def async_getter(request):
        contexts.append({"user": "async"})
        return contexts[-1]

    for getter in (sync_getter, async_getter):
        qa, _ = agent(reply(call(POSTS_QUERY)))
        async with Client(mcp_server(qa, context_getter=getter)) as client:
            await client.call_tool("ask", {"question": "Posts?"})

    assert [c["seen"] for c in contexts] == [["sync"], ["async"]]
    assert contexts[0]["request"] is None


async def test_the_default_context_holds_the_request():
    seen = []

    class Recorder(QueryAgent):
        async def ask(self, question, *, context=None, clarifier=None):
            seen.append(context)
            return {"data": {}}

    qa = Recorder(schema, model="test:model", completion=Model())
    async with Client(mcp_server(qa)) as client:
        await client.call_tool("ask", {"question": "?"})

    assert seen == [{"request": None}]


async def test_a_failed_answer_is_a_tool_error():
    qa, _ = agent(reply(content="No weather here."))

    async with Client(mcp_server(qa)) as client:
        result = await client.call_tool("ask", {"question": "Weather?"})

    assert result.is_error is True
    assert _detail(result) == {"error": "No weather here.", "query": None, "errors": []}


async def test_the_description_can_say_what_the_api_holds():
    qa, _ = agent(allow_mutations=True)

    async with Client(mcp_server(qa)) as client:
        [tool] = (await client.list_tools()).tools
    async with Client(
        mcp_server(qa, description="Blog posts and authors.", name="blog")
    ) as client:
        [custom] = (await client.list_tools()).tools

    assert (
        "It can read and change: posts, named, post, publish, fail." in tool.description
    )
    assert tool.annotations.read_only_hint is False
    assert custom.description == "Blog posts and authors."


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_question_is_asked_with_a_form(mode):
    form = _form(ElicitResult(action="accept", content={"author": "Alice"}))
    qa, model = agent(reply(ask_user()), reply(call(POSTS_QUERY)))

    async with Client(mcp_server(qa), mode=mode, elicitation_callback=form) as client:
        result = await client.call_tool("ask", {"question": "Posts by an author"})

    assert result.structured_content["data"] == HELLO
    [shown] = form.shown
    assert shown.message == "Which author?"
    assert model.tool_replies() == [{"answers": {"author": "Alice"}}]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_declined_form_tells_the_model(mode):
    form = _form(ElicitResult(action="decline"))
    qa, model = agent(reply(ask_user()), reply(call(POSTS_QUERY)))

    async with Client(mcp_server(qa), mode=mode, elicitation_callback=form) as client:
        await client.call_tool("ask", {"question": "?"})

    assert model.tool_replies() == [{"declined": True}]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_mutation_is_confirmed_with_a_form(mode):
    form = _form(ElicitResult(action="accept", content={"confirm": True}))
    qa, _ = agent(reply(call(PUBLISH)), allow_mutations=True)

    async with Client(mcp_server(qa), mode=mode, elicitation_callback=form) as client:
        result = await client.call_tool("ask", {"question": "Publish x"})

    assert result.structured_content["data"] == {"publish": "published x"}
    assert PUBLISH in form.shown[0].message


async def test_a_client_without_forms_gets_needs_input():
    qa, _ = agent(reply(ask_user()))

    async with Client(mcp_server(qa)) as client:
        result = await client.call_tool("ask", {"question": "Posts by an author"})

    assert result.is_error is False
    assert result.structured_content == {
        "needs_input": {
            "kind": "question",
            "message": "Which author?",
            "schema": {
                "type": "object",
                "properties": {
                    "author": {"type": "string", "description": "Their name"}
                },
                "required": ["author"],
            },
        }
    }


async def test_a_client_without_forms_cannot_confirm_a_change():
    qa, _ = agent(reply(call(PUBLISH)), allow_mutations=True)

    async with Client(mcp_server(qa)) as client:
        result = await client.call_tool("ask", {"question": "Publish x"})

    assert result.is_error is True
    detail = _detail(result)
    assert "cannot show a confirmation" in detail["error"]
    assert detail["query"] == PUBLISH


def _ctx(capabilities):
    return SimpleNamespace(client_capabilities=capabilities)


def test_forms_need_the_elicitation_capability():
    form_only = SimpleNamespace(elicitation=SimpleNamespace(form={}, url=None))
    url_only = SimpleNamespace(elicitation=SimpleNamespace(form=None, url={}))
    bare = SimpleNamespace(elicitation=SimpleNamespace(form=None, url=None))

    assert _supports_forms(_ctx(None)) is False
    assert _supports_forms(_ctx(SimpleNamespace(elicitation=None))) is False
    assert _supports_forms(_ctx(form_only)) is True
    assert _supports_forms(_ctx(url_only)) is False
    assert _supports_forms(_ctx(bare)) is True


async def test_a_connection_with_no_way_back_pauses_the_agent():
    async def elicit_form(*args, **kwargs):
        raise NoBackChannelError("elicitation/create")

    ctx = SimpleNamespace(
        session=SimpleNamespace(elicit_form=elicit_form), request_id=1
    )
    clarifier = _elicit_in_call(ctx)

    assert await clarifier(Clarification.confirm("mutation { a }", {})) is PAUSE


def test_only_an_accepted_form_counts_as_answers():
    """A client may answer an input request with another kind of result."""
    assert _answers(ListRootsResult(roots=[])) is None
    assert _answers(ElicitResult(action="cancel")) is None
    assert _answers(ElicitResult(action="accept", content={"a": 1})) == {"a": 1}
