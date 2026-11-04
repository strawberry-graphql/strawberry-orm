"""QueryAgent: one GraphQL operation per question, written by a model."""

from __future__ import annotations

import json

import pytest
import strawberry

from strawberry_orm.contrib.mcp import (
    PAUSE,
    QueryAgent,
    QueryAgentError,
    QueryRejected,
)

from .fakes import PUBLISH, Model, Query, agent, ask_user, call, reply, schema

POSTS_QUERY = "{ posts { title } }"
ALL_TITLES = {"posts": [{"title": "Hello"}, {"title": "Rust"}, {"title": "Draft"}]}


async def test_answers_with_the_query_and_its_data():
    query = "query($t: String) { posts(title: $t) { title } }"
    qa, model = agent(reply(call(query, {"t": "Hello"})))

    answer = await qa.ask("Find Hello", context={"user": "alice"})

    assert answer == {
        "query": query,
        "variables": {"t": "Hello"},
        "data": {"posts": [{"title": "Hello"}]},
    }
    request = model.requests[0]
    assert request["model"] == "test:model"
    assert request["api_key"] is None
    assert request["max_tokens"] == 4096
    assert request["messages"][1] == {"role": "user", "content": "Find Hello"}


async def test_the_query_runs_with_the_callers_context():
    qa, _ = agent(reply(call(POSTS_QUERY)))
    context = {"user": "alice"}

    await qa.ask("Posts?", context=context)

    assert context["seen"] == ["alice"]


async def test_fields_that_fail_are_returned_beside_the_data():
    qa, _ = agent(reply(call('{ posts(title: "Hello") { title secret } }')))

    answer = await qa.ask("Secrets?", context={})

    assert answer["data"] == {"posts": [{"title": "Hello", "secret": None}]}
    assert answer["errors"] == ["You may not read secrets."]


async def test_the_model_sees_why_a_query_failed_and_tries_again():
    qa, model = agent(
        reply(call("{ posts { nope } }")), reply(call(POSTS_QUERY, id="c2"))
    )

    answer = await qa.ask("Posts?", context={})

    assert answer["data"] == ALL_TITLES
    [feedback] = model.tool_replies()
    assert "Cannot query field 'nope'" in feedback["errors"][0]


async def test_a_query_with_no_data_is_retried_with_its_errors():
    qa, model = agent(
        reply(call("{ post { title } }")), reply(call(POSTS_QUERY, id="c2"))
    )

    await qa.ask("A post?", context={})

    assert model.tool_replies() == [{"errors": ["No such post."]}]


@pytest.mark.parametrize(
    ("query", "error"),
    [
        ("{ posts {", "Syntax Error"),
        ("{ posts { title } } { named { name } }", "Send exactly one operation."),
        (PUBLISH, "Mutation operations are not allowed."),
        ("subscription { posts }", "Subscription operations are not allowed."),
    ],
)
async def test_operations_are_checked_before_they_run(query, error):
    qa, model = agent(reply(call(query)), reply(call(POSTS_QUERY, id="c2")))

    await qa.ask("?", context={})

    [feedback] = model.tool_replies()
    assert feedback["errors"][0].startswith(error)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "",
        json.dumps({"query": 1}),
        json.dumps({"query": "{}", "variables": []}),
    ],
)
async def test_malformed_tool_arguments_are_sent_back(raw):
    qa, model = agent(reply(call(raw=raw)), reply(call(POSTS_QUERY, id="c2")))

    await qa.ask("?", context={})

    [feedback] = model.tool_replies()
    assert feedback["errors"]


async def test_depth_counts_fields_through_fragments():
    query = """
        query { posts { ...P } named { ... on Author { name } } posts { ...Missing } }
        fragment P on Post { author { ...A } }
        fragment A on Author { ... on Author { name } }
    """
    qa, model = agent(
        reply(call(query)), reply(call(POSTS_QUERY, id="c2")), max_depth=2
    )

    await qa.ask("?", context={})

    assert model.tool_replies() == [
        {"errors": ["The query is 3 levels deep; the limit is 2."]}
    ]


async def test_a_fragment_that_spreads_itself_is_counted_once():
    query = "{ posts { ...P } } fragment P on Post { author { name } ...P }"
    qa, model = agent(reply(call(query)), reply(call(POSTS_QUERY, id="c2")))

    await qa.ask("?", context={})

    [feedback] = model.tool_replies()
    assert "Cannot spread fragment 'P' within itself." in feedback["errors"]


async def test_only_one_call_per_turn_runs():
    qa, model = agent(
        reply(call("{ posts { nope } }"), call(POSTS_QUERY, id="c2")),
        reply(call(POSTS_QUERY, id="c3")),
    )

    await qa.ask("?", context={})

    assert {"errors": ["Call one tool at a time."]} in model.tool_replies()


async def test_a_tool_that_is_not_offered_counts_as_a_failed_attempt():
    qa, model = agent(
        reply(call(POSTS_QUERY, name="lookup")), reply(call(POSTS_QUERY, id="c2"))
    )

    await qa.ask("?", context={})

    assert model.tool_replies() == [{"errors": ["There is no lookup tool to call."]}]
    assert "lookup" not in model.tools(0)


async def test_a_model_that_declines_explains_why():
    qa, _ = agent(reply(content="The schema has no weather data."))

    with pytest.raises(QueryAgentError, match="no weather data") as raised:
        await qa.ask("Weather?", context={})

    assert raised.value.query is None
    assert raised.value.errors == []


async def test_a_model_that_declines_without_text():
    qa, _ = agent(reply())

    with pytest.raises(QueryAgentError, match="did not write a query"):
        await qa.ask("?", context={})


async def test_it_gives_up_after_max_attempts():
    qa, _ = agent(
        reply(call("{ posts { nope } }")),
        reply(call("{ posts { nope } }", id="c2")),
        max_attempts=2,
    )

    with pytest.raises(QueryAgentError, match="after 2 attempts") as raised:
        await qa.ask("?", context={})

    assert raised.value.query == "{ posts { nope } }"
    assert raised.value.errors


def test_the_prompt_shows_queries_and_clarifying_by_default():
    qa, _ = agent()

    assert qa.root_fields == ["posts", "named", "post"]
    assert "type Mutation" not in qa.sdl
    assert "type Author implements Named" in qa.sdl
    assert "query or mutation" not in qa.system_prompt
    assert "call ask_user" in qa.system_prompt
    assert "lookup shows you" not in qa.system_prompt


def test_instructions_and_options_reach_the_model():
    qa, _ = agent(
        instructions="Prefer titles.", api_key="k", temperature=0, max_tokens=100
    )

    assert "Prefer titles." in qa.system_prompt
    assert qa._completion_options == {
        "api_key": "k",
        "max_tokens": 100,
        "temperature": 0,
    }


def test_a_schema_without_mutations_lists_its_queries():
    qa = QueryAgent(
        strawberry.Schema(query=Query),
        model="test:model",
        completion=Model(),
        allow_mutations=True,
    )

    assert qa.root_fields == ["posts", "named", "post"]


def test_any_llm_is_the_default_completion():
    from any_llm import acompletion

    qa = QueryAgent(schema, model="anthropic:claude-sonnet-5-5")

    assert qa._completion is acompletion


# Clarifying questions


async def test_the_model_can_ask_for_missing_details():
    asked = []

    async def clarifier(clarification):
        asked.append(clarification)
        return {"author": "Alice"}

    qa, model = agent(reply(ask_user()), reply(call(POSTS_QUERY)))

    answer = await qa.ask("Posts by an author", context={}, clarifier=clarifier)

    assert answer["data"] == ALL_TITLES
    [clarification] = asked
    assert clarification.kind == "question"
    assert clarification.message == "Which author?"
    assert clarification.fields == ["author"]
    assert model.tool_replies() == [{"answers": {"author": "Alice"}}]
    assert "ask_user" in model.tools(0)


async def test_a_declined_question_tells_the_model():
    async def clarifier(clarification):
        return None

    qa, model = agent(reply(ask_user()), reply(call(POSTS_QUERY)))

    await qa.ask("?", context={}, clarifier=clarifier)

    assert model.tool_replies() == [{"declined": True}]


async def test_questions_stop_at_max_clarifications():
    async def clarifier(clarification):
        return {"author": "Alice"}

    qa, model = agent(
        reply(ask_user()),
        reply(ask_user(id="q2")),
        reply(call(POSTS_QUERY)),
        max_clarifications=1,
    )

    await qa.ask("?", context={}, clarifier=clarifier)

    assert "ask_user" not in model.tools(1)
    assert model.tool_replies()[-1] == {
        "errors": ["There is no ask_user tool to call."]
    }


async def test_without_clarify_the_model_cannot_ask():
    qa, model = agent(reply(call(POSTS_QUERY)), clarify=False)

    await qa.ask("?", context={})

    assert model.tools(0) == ["run_graphql"]
    assert "ask_user" not in qa.system_prompt


async def test_an_unusable_question_is_sent_back():
    qa, model = agent(
        reply(ask_user(fields={"when": {"type": "date"}})),
        reply(call(POSTS_QUERY)),
        max_clarifications=2,
    )

    await qa.ask("?", context={}, clarifier=None)

    [feedback] = model.tool_replies()
    assert feedback["errors"][0].startswith("Field 'when' needs a type")


async def test_without_a_clarifier_the_agent_pauses_and_resumes():
    qa, model = agent(reply(ask_user()), reply(call(POSTS_QUERY)))

    paused = await qa.ask("Posts by an author", context={})

    assert paused["needs_input"] == {
        "kind": "question",
        "message": "Which author?",
        "schema": {
            "type": "object",
            "properties": {"author": {"type": "string", "description": "Their name"}},
            "required": ["author"],
        },
    }
    state = json.loads(json.dumps(paused["state"]))
    answer = await qa.resume(state, {"author": "Alice"}, context={})
    assert answer["data"] == ALL_TITLES
    assert model.tool_replies() == [{"answers": {"author": "Alice"}}]
    assert paused["state"]["pending"] == {"kind": "question", "call_id": "q1"}


async def test_resuming_after_a_declined_question():
    qa, model = agent(reply(ask_user()), reply(call(POSTS_QUERY)))

    paused = await qa.ask("?", context={})
    await qa.resume(paused["state"], None, context={})

    assert model.tool_replies() == [{"declined": True}]


async def test_a_clarifier_can_pause_the_agent():
    async def clarifier(clarification):
        return PAUSE

    qa, _ = agent(reply(ask_user()))

    paused = await qa.ask("?", context={}, clarifier=clarifier)

    assert paused["needs_input"]["kind"] == "question"


async def test_only_paused_state_can_be_resumed():
    qa, _ = agent()

    with pytest.raises(ValueError, match="not waiting for answers"):
        await qa.resume({"pending": None}, {}, context={})


async def test_on_clarify_can_answer_from_the_context():
    def from_context(clarification, context):
        return {"author": context["user"]}

    qa, model = agent(
        reply(ask_user()), reply(call(POSTS_QUERY)), on_clarify=from_context
    )

    await qa.ask("My posts", context={"user": "Alice"})

    assert model.tool_replies() == [{"answers": {"author": "Alice"}}]


async def test_on_clarify_can_pass_a_question_on():
    seen = []

    async def nothing_to_add(clarification, context):
        seen.append(clarification.kind)

    qa, _ = agent(reply(ask_user()), on_clarify=nothing_to_add)

    paused = await qa.ask("?", context={})

    assert seen == ["question"]
    assert "needs_input" in paused


async def test_on_clarify_can_stop_the_agent():
    def refuse(clarification, context):
        raise QueryAgentError("Questions are not allowed here.")

    qa, _ = agent(reply(ask_user()), on_clarify=refuse)

    with pytest.raises(QueryAgentError, match="not allowed here"):
        await qa.ask("?", context={})


# Mutations


async def test_mutations_run_once_confirmed():
    asked = []

    async def clarifier(clarification):
        asked.append(clarification)
        return {"confirm": True}

    qa, _ = agent(reply(call(PUBLISH)), allow_mutations=True)

    answer = await qa.ask("Publish x", context={}, clarifier=clarifier)

    assert answer["data"] == {"publish": "published x"}
    [confirmation] = asked
    assert confirmation.kind == "confirm"
    assert confirmation.query == PUBLISH
    assert PUBLISH in confirmation.message
    assert "publish" in qa.root_fields
    assert "type Mutation" in qa.sdl
    assert "query or mutation" in qa.system_prompt


@pytest.mark.parametrize("answers", [None, {"confirm": False}])
async def test_an_unconfirmed_mutation_does_not_run(answers):
    async def clarifier(clarification):
        return answers

    qa, _ = agent(reply(call(PUBLISH)), allow_mutations=True)

    with pytest.raises(QueryAgentError, match="not confirmed") as raised:
        await qa.ask("Publish x", context={}, clarifier=clarifier)

    assert raised.value.query == PUBLISH


async def test_confirming_can_be_turned_off():
    qa, _ = agent(reply(call(PUBLISH)), allow_mutations=True, confirm_mutations=False)

    answer = await qa.ask("Publish x", context={})

    assert answer["data"] == {"publish": "published x"}


async def test_a_paused_confirmation_resumes_into_the_mutation():
    variables_query = "mutation($t: String!) { publish(title: $t) }"
    qa, _ = agent(reply(call(variables_query, {"t": "x"})), allow_mutations=True)

    paused = await qa.ask("Publish x", context={})

    needs_input = paused["needs_input"]
    assert needs_input["kind"] == "confirm"
    assert needs_input["query"] == variables_query
    assert needs_input["variables"] == {"t": "x"}
    assert '{"t": "x"}' in needs_input["message"]
    answer = await qa.resume(paused["state"], {"confirm": True}, context={})
    assert answer["data"] == {"publish": "published x"}


@pytest.mark.parametrize("answers", [None, {"confirm": "yes"}])
async def test_a_paused_confirmation_that_is_declined(answers):
    qa, _ = agent(reply(call(PUBLISH)), allow_mutations=True)

    paused = await qa.ask("Publish x", context={})

    with pytest.raises(QueryAgentError, match="not confirmed"):
        await qa.resume(paused["state"], answers, context={})


async def test_a_confirmed_mutation_that_fails_goes_back_to_the_model():
    qa, model = agent(
        reply(call("mutation { fail }")),
        reply(content="Publishing is down right now."),
        allow_mutations=True,
    )

    paused = await qa.ask("Publish", context={})

    with pytest.raises(QueryAgentError, match="down right now"):
        await qa.resume(paused["state"], {"confirm": True}, context={})
    assert model.tool_replies() == [{"errors": ["Publishing is down."]}]


async def test_a_paused_confirmation_runs_its_change_once():
    """A client resending the same state, retrying or otherwise, does not
    run the change a second time."""
    qa, _ = agent(reply(call(PUBLISH)), allow_mutations=True)

    paused = await qa.ask("Publish x", context={})
    await qa.resume(paused["state"], {"confirm": True}, context={})

    with pytest.raises(QueryAgentError, match="already been run"):
        await qa.resume(paused["state"], {"confirm": True}, context={})


async def test_a_paused_confirmation_expires():
    qa, _ = agent(reply(call(PUBLISH)), allow_mutations=True, confirm_within=0)

    paused = await qa.ask("Publish x", context={})

    with pytest.raises(QueryAgentError, match="expired") as raised:
        await qa.resume(paused["state"], {"confirm": True}, context={})
    assert raised.value.query == PUBLISH


async def test_a_confirmation_without_its_time_counts_as_expired():
    qa, _ = agent(reply(call(PUBLISH)), allow_mutations=True)

    paused = await qa.ask("Publish x", context={})
    del paused["state"]["pending"]["at"]

    with pytest.raises(QueryAgentError, match="expired"):
        await qa.resume(paused["state"], {"confirm": True}, context={})


async def test_confirmations_that_ran_are_forgotten_once_they_expire():
    qa, _ = agent(reply(call(PUBLISH)), allow_mutations=True)
    qa._confirmed["long ago"] = 0.0

    paused = await qa.ask("Publish x", context={})
    await qa.resume(paused["state"], {"confirm": True}, context={})

    assert list(qa._confirmed) == [paused["state"]["pending"]["id"]]


async def test_a_resumed_change_is_checked_again():
    """The state comes back from outside, so what it would run is checked
    before it runs, and a failure goes to the model like any other."""
    qa, model = agent(
        reply(call(PUBLISH)),
        reply(content="That change cannot be made."),
        allow_mutations=True,
        max_depth=1,
    )
    paused = await qa.ask("Publish x", context={})
    paused["state"]["pending"]["query"] = "{ posts { author { name } } }"

    with pytest.raises(QueryAgentError, match="cannot be made"):
        await qa.resume(paused["state"], {"confirm": True}, context={})
    assert model.tool_replies() == [
        {"errors": ["The query is 3 levels deep; the limit is 1."]}
    ]


def test_lookups_with_unconfirmed_mutations_are_warned_about():
    """Rows other users wrote reach the model with lookups on, and nothing
    stands between an instruction in one and an unconfirmed change."""
    with pytest.warns(UserWarning, match="Keep confirm_mutations on"):
        agent(lookups=True, allow_mutations=True, confirm_mutations=False)


# Lookups


async def test_lookups_show_the_model_a_few_rows():
    qa, model = agent(
        reply(call("{ posts { title secret } }", name="lookup")),
        reply(call(POSTS_QUERY)),
        lookups=True,
        max_lookup_rows=2,
    )

    await qa.ask("?", context={})

    assert model.tool_replies() == [
        {
            "data": {
                "posts": [
                    {"title": "Hello", "secret": None},
                    {"title": "Rust", "secret": None},
                ]
            },
            "truncated": "Lists show their first 2.",
            "errors": ["You may not read secrets."] * 3,
        }
    ]
    assert "lookup shows you up to 2 items" in qa.system_prompt


async def test_a_lookup_that_fits_is_not_marked_truncated():
    qa, model = agent(
        reply(call("{ named { name } }", name="lookup")),
        reply(call(POSTS_QUERY)),
        lookups=True,
    )

    await qa.ask("?", context={})

    assert model.tool_replies() == [{"data": {"named": {"name": "Alice"}}}]


async def test_lookups_only_read():
    qa, model = agent(
        reply(call(PUBLISH, name="lookup")),
        reply(call(POSTS_QUERY)),
        lookups=True,
        allow_mutations=True,
    )

    await qa.ask("?", context={})

    assert model.tool_replies() == [
        {"errors": ["Mutation operations are not allowed."]}
    ]


async def test_lookups_stop_at_max_lookups():
    qa, model = agent(
        reply(call(POSTS_QUERY, name="lookup")),
        reply(call(POSTS_QUERY)),
        lookups=True,
        max_lookups=1,
    )

    await qa.ask("?", context={})

    assert "lookup" in model.tools(0)
    assert "lookup" not in model.tools(1)


# before_query


async def test_before_query_can_send_the_model_an_error():
    seen = []

    async def limit(query, variables, context):
        seen.append((query, variables, context["user"]))
        if "title" not in variables:
            raise QueryRejected("Filter by title.")

    filtered = "query($title: String) { posts(title: $title) { title } }"
    qa, model = agent(
        reply(call(POSTS_QUERY)),
        reply(call(filtered, {"title": "Hello"}, id="c2")),
        before_query=limit,
    )

    answer = await qa.ask("?", context={"user": "alice"})

    assert answer["data"] == {"posts": [{"title": "Hello"}]}
    assert model.tool_replies() == [{"errors": ["Filter by title."]}]
    assert seen == [(POSTS_QUERY, {}, "alice"), (filtered, {"title": "Hello"}, "alice")]


async def test_before_query_also_checks_lookups():
    def no_lookups(query, variables, context):
        raise QueryRejected("No lookups today.")

    qa, model = agent(
        reply(call(POSTS_QUERY, name="lookup")),
        reply(call(POSTS_QUERY)),
        lookups=True,
        before_query=lambda q, v, c: None,
    )
    await qa.ask("?", context={})
    assert model.tool_replies() == [{"data": ALL_TITLES}]

    qa, model = agent(
        reply(call(POSTS_QUERY, name="lookup")),
        reply(content="Giving up."),
        lookups=True,
        before_query=no_lookups,
    )
    with pytest.raises(QueryAgentError):
        await qa.ask("?", context={})
    assert model.tool_replies() == [{"errors": ["No lookups today."]}]


async def test_before_query_can_stop_the_agent():
    def stop(query, variables, context):
        raise QueryAgentError("Rate limited.")

    qa, _ = agent(reply(call(POSTS_QUERY)), before_query=stop)

    with pytest.raises(QueryAgentError, match="Rate limited."):
        await qa.ask("?", context={})
