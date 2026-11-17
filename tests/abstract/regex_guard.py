"""A regex filter that backtracks for cubic time never reaches the database.

SQLite matches a regex with Python's ``re`` in the server's process, holding
the GIL, so one pattern that backtracks for long stalls every request the
process serves. Each pattern refused here was let through, and took eight
times as long each time a value doubled (``\\w*\\d*+!``: 3.3 seconds on 1,600
digits): a possessive repeat after a repeat that can match the same
characters, two repeats that share only characters outside Latin script, a
set the ASCII flag widens, and a dotted capital ``İ``, which ``re`` reads as
``i`` once ``iRegex`` ignores case. A counted repeat of more characters than
the length cap (``.*x{500}y``) multiplied the time by its count.

Backends supply:

``make_orm(**options)``             a fresh ORM with those options
``models``                          ``User``
``run(schema, query, variables)``   awaitable result of running *query*
``statements()``                    context manager yielding the list of SQL
                                    statements sent while it is open
"""

import pytest
import strawberry

from strawberry_orm.types import auto

QUERY = "query ($p: String!) { users(filter: {field: {email: {%s: $p}}}) { name } }"
TRADES = "may not hold two repeats"


def _schema(make_orm, models):
    orm = make_orm(enable_regex_filters=True)
    User = models.User

    @orm.type(User, filters=orm.filter(User))
    class UserType:
        id: auto
        name: auto
        email: auto

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.auto()

    return orm.schema(query=Query)


class AbstractTestRegexGuard:
    @pytest.mark.parametrize(
        ("lookup", "pattern", "refusal"),
        [
            ("regex", r"\w*\d*+!", TRADES),
            ("regex", r".*(?>x)*+y", TRADES),
            ("regex", r"\w*[^\x00-\u4e00]*!", TRADES),
            ("regex", r"(?a:[^\d])*\d*!", TRADES),
            ("iRegex", "\u0130*i*!", TRADES),
            ("regex", r".*x{500}y", "counted repeats"),
        ],
    )
    async def test_a_pattern_that_backtracks_for_cubic_time_is_refused(
        self, make_orm, models, run, statements, lookup, pattern, refusal
    ):
        schema = _schema(make_orm, models)
        with statements() as sent:
            result = await run(schema, QUERY % lookup, {"p": pattern})
        assert result.data is None
        assert result.errors is not None
        assert refusal in result.errors[0].message, result.errors
        assert sent == []

    @pytest.mark.parametrize(
        ("lookup", "pattern"),
        [
            ("regex", "[^@]+@.+"),
            ("regex", r"[a-z]*\d*+!"),
            ("iRegex", r"\w+\d{4}"),
        ],
    )
    async def test_a_pattern_whose_repeats_cannot_trade_characters_is_let_through(
        self, make_orm, models, run, lookup, pattern
    ):
        schema = _schema(make_orm, models)
        result = await run(schema, QUERY % lookup, {"p": pattern})
        messages = [error.message for error in result.errors or []]
        assert not any("A regex filter" in message for message in messages)
