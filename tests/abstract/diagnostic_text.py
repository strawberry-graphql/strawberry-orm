"""What a client sends never reaches the warnings registry, and logs are bounded.

``lazy_resolution="warn"`` reports through ``warnings.warn``, and Python keeps
every distinct warning text it shows in ``__warningregistry__`` for the life
of the process. So a warning names only the schema's types and fields: what a
client picks - response keys, an operation name, a cursor, how often it
selects a field - never makes a new one. The report logged, and raised in
``"error"`` mode, still names the request's paths, cut to a bounded length.

Backends supply what ``tests.abstract.bounded_reads`` asks for, and:

``owner_rows(info)``  every owner, read into a list (or an awaitable of one)
"""

import base64
import logging
import re
import sys

import pytest
import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.recorded_warnings import recording

REQUESTS = 8
LONG = 1000
MOST_WARNED = 400
MOST_LOGGED = 4096
#: Client text longer than this is not repeated in full anywhere.
ECHO = re.compile(r"x{300}|y{300}")
LOGGER = "strawberry_orm.lazy_query"


def alias(i: int) -> str:
    return f"a{i}_" + "x" * LONG


def opaque_cursor(i: int) -> str:
    return base64.b64encode((f"c{i}" + "y" * LONG).encode()).decode()


def _keys(count, selection):
    """*selection* under *count* response keys of its own."""
    return " ".join(f"k{j}: {selection}" for j in range(count))


#: ``(optimizer, query for request i, whether the request is refused)``
SCENARIOS = {
    "a relation read without the optimizer": (
        False,
        lambda i: f"{{ ownerRows {{ {alias(i)}: items {{ name }} }} }}",
        False,
    ),
    "an operation name": (
        False,
        lambda i: f"query Q{i}{'x' * LONG} {{ ownerRows {{ items {{ name }} }} }}",
        False,
    ),
    "a field selected under more response keys": (
        False,
        lambda i: f"{{ ownerRows {{ {_keys(i + 1, 'items { name }')} }} }}",
        False,
    ),
    "a selection under the relation": (
        False,
        lambda i: f"{{ ownerRows {{ items {{ {_keys(i * 40 + 1, 'name')} }} }} }}",
        False,
    ),
    "last without first on a relation connection": (
        True,
        lambda i: f"{{ owners {{ {alias(i)}: items(last: 1) {{ totalCount }} }} }}",
        False,
    ),
    "a cursor that does not decode": (
        True,
        lambda i: (
            f"{{ owners {{ {alias(i)}: items(first: 1, "
            f'after: "{opaque_cursor(i)}") {{ totalCount }} }} }}'
        ),
        True,
    ),
    "rows from a root resolver": (
        True,
        lambda i: f"{{ {alias(i)}: listedOwners {{ items {{ name }} }} }}",
        False,
    ),
}

#: The scenarios the lazy-resolution report covers, for ``"error"`` mode.
REPORTED = [name for name, (_o, _q, refused) in SCENARIOS.items() if not refused]


def build(orm, Owner, Item, hide, owner_rows, *, optimizer):
    @orm.type(Item)
    class UbItemNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

        @classmethod
        def scope_rows(cls, query, info):
            return hide(query)

    @orm.type(Owner)
    class UbOwnerNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        items: ORMListConnection[UbItemNode] = orm.connection.eager()

    @orm.type(Owner)
    class UbOwnerRow:
        id: auto
        name: auto
        items: list[UbItemNode]

    @strawberry.type
    class Query:
        owners: list[UbOwnerNode] = orm.field.eager()
        owner_rows: list[UbOwnerRow] = orm.field.eager()

        @strawberry.field
        def listed_owners(self, info: strawberry.Info) -> list[UbOwnerRow]:
            return owner_rows(info)

    return orm.schema(query=Query, optimizer=optimizer)


def _echoes(text: str) -> bool:
    return ECHO.search(text) is not None


def _registered() -> int:
    """How many warning texts every module's ``__warningregistry__`` holds."""
    return sum(
        sum(isinstance(key, tuple) for key in registry)
        for module in list(sys.modules.values())
        for registry in [getattr(module, "__dict__", {}).get("__warningregistry__")]
        if registry
    )


class AbstractTestDiagnosticText:
    @pytest.mark.parametrize("scenario", list(SCENARIOS))
    async def test_a_warning_names_only_the_schema(
        self,
        scenario,
        make_orm,
        Owner,
        Item,
        hide,
        owner_rows,
        seeded,
        run,
        caplog,
    ):
        optimizer, query, refused = SCENARIOS[scenario]
        schema = build(
            make_orm(lazy_resolution="warn"),
            Owner,
            Item,
            hide,
            owner_rows,
            optimizer=optimizer,
        )
        caplog.set_level(logging.WARNING, logger=LOGGER)
        shown = []
        registered = []
        errors = []
        with recording("default") as caught:
            for i in range(REQUESTS):
                result = await run(schema, query(i))
                assert (result.errors is not None) == refused, result.errors
                errors.extend(error.message for error in result.errors or ())
                shown.append(len(caught))
                registered.append(_registered())
        for message in errors:
            assert not _echoes(message), message[:600]
        assert len(set(errors)) <= 1, [m[:200] for m in set(errors)]
        logged = [r.getMessage() for r in caplog.records if r.name == LOGGER]
        assert logged
        for message in logged:
            assert len(message) <= MOST_LOGGED, len(message)
            assert not _echoes(message), message[:600]
        texts = [str(warning.message) for warning in caught]
        assert any("Unoptimized relation loads detected" in t for t in texts), texts
        for text in texts:
            assert len(text) <= MOST_WARNED, text[:600]
            assert not _echoes(text), text[:600]
        assert shown[-1] == shown[0], [t[:200] for t in texts[shown[0] :]]
        assert registered[-1] == registered[0], registered

    @pytest.mark.parametrize("scenario", REPORTED)
    async def test_an_error_mode_report_is_bounded(
        self, scenario, make_orm, Owner, Item, hide, owner_rows, seeded, run
    ):
        optimizer, query, _refused = SCENARIOS[scenario]
        schema = build(
            make_orm(lazy_resolution="error"),
            Owner,
            Item,
            hide,
            owner_rows,
            optimizer=optimizer,
        )
        for i in (0, REQUESTS - 1):
            result = await run(schema, query(i))
            assert result.errors, result.data
            for error in result.errors:
                assert "Unoptimized relation loads detected" in error.message
                assert len(error.message) <= MOST_LOGGED, len(error.message)
                assert not _echoes(error.message), error.message[:600]
