"""The MCP ``graphql`` tool's filters are held to the characters they bind.

Its own limits count a call's values, not their size, so a long string sent
once and referenced from many slots is caught by the filter budget the
schema's fields apply, before any statement is sent.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from mcp import Client
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from strawberry_orm import StrawberryORM
from strawberry_orm.contrib.mcp import mcp_server
from tests.abstract.filter_value_size import PLACES, _query, _schema
from tests.backends.sqlalchemy.models import Base, Post, User


@pytest.fixture(autouse=True)
def _quiet_strawberry_errors():
    logger = logging.getLogger("strawberry.execution")
    disabled = logger.disabled
    logger.disabled = True
    yield
    logger.disabled = disabled


@pytest.mark.parametrize("refs", [1, 2])
async def test_a_value_referenced_past_the_budget_is_refused(refs):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(id=1, name="Alice", email="alice@example.com"))
    session.add(Post(id=1, title="t", body="b", author_id=1))
    session.commit()

    sent: list[int] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        sent.append(sum(len(v) for v in parameters if isinstance(v, str)))

    def make_orm(**options):
        return StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", **options
        )

    schema = _schema(make_orm, SimpleNamespace(Post=Post, User=User))
    server = mcp_server(
        schema, graphql_tool=True, context_getter=lambda request: {"session": session}
    )
    _root, build = PLACES["notInList"]
    arguments = {
        "query": _query("posts", [build(refs)]),
        "variables": {"s": "x" * 60_000},
    }

    async with Client(server) as client:
        result = await client.call_tool("graphql", arguments)

    answer = json.loads(result.content[0].text)
    errors = [e["message"] for e in answer.get("errors") or ()]
    if refs == 1:
        assert errors == [], errors
        assert answer["data"] == {"a0": [{"id": 1}]}
    else:
        assert errors == [
            "Filter has over 100000 characters of values in total; maximum is 100000"
        ], errors
        assert sum(sent) == 0, sent
