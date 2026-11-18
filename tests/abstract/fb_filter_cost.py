"""What one request's filters cost the database is bounded.

A regex costs time quadratic in a value's length for each row it is tested
on, and on SQLite it runs in Python's ``re`` holding the GIL. A ``LIKE`` costs
the value's length times the pattern's. The filter budget counted branches
and listed values but never these, so one filter could carry as many regex
lookups as ``max_filter_branches`` allows, and aliases could repeat it. Now
``max_regex_filters`` caps the regex and ``iRegex`` lookups of a request and
``max_filter_text_length`` the characters of its ``contains``,
``startsWith`` and ``endsWith`` lookups (each with ``i``), every reference to
a variable included, through ``any``, ``all``, ``oneOf``, ``not``,
``object`` and aliases alike. A relation field's filter counts once however
many parent rows it is resolved for.

A regex is never run on a value longer than ``max_regex_text_length``, which
does not match it, and the pattern the database runs is the one the guard
analysed: ``iRegex`` on SQLAlchemy over SQLite ran case-sensitively before,
since SQLite takes no flags, and a pattern Python cannot parse reached the
database unanalysed.

Backends supply:

``make_orm(**options)``             a fresh ORM with those options
``models``                          ``User`` and ``Post``
``run(schema, query, variables)``   awaitable result of running *query*
``statements()``                    context manager yielding the list of SQL
                                    statements sent while it is open
``regex_filters``                   whether the backend offers regex lookups
``regexp_calls()``                  (for those that do) context manager
                                    yielding the ``(pattern, value)`` pairs
                                    the database's regex function is called
                                    with while it is open
"""

import pytest
import strawberry

from strawberry_orm.types import auto

CAP = 2
LIMITS = {"max_regex_filters": CAP, "max_filter_text_length": 4 * CAP}
REGEX = ("regex", "iRegex")
TEXT = ("contains", "iContains", "startsWith", "iStartsWith", "endsWith", "iEndsWith")
VALUES = {"regex": "^A", "iRegex": "^a"}
TEXT_VALUE = "abcd"

NAME = "{field: {name: {%s: $v}}}"
TITLE = "{field: {title: {%s: $v}}}"


def _schema(make_orm, models, **options):
    """Users and their posts: ``object`` reaches a user's posts."""
    orm = make_orm(**options)
    User, Post = models.User, models.Post

    @orm.type(Post, filters=orm.filter(Post))
    class FbPostType:
        id: auto
        title: auto

    @orm.type(User, filters=orm.filter(User))
    class FbUserType:
        id: auto
        name: auto
        email: auto
        posts: list[FbPostType]

    @strawberry.type
    class Query:
        users: list[FbUserType] = orm.field.auto()

    return orm.schema(query=Query)


def _authored_schema(make_orm, models, **options):
    """Posts and their authors: ``object`` reaches a post's author."""
    orm = make_orm(**options)
    User, Post = models.User, models.Post

    @orm.type(User, filters=orm.filter(User))
    class FbAuthorType:
        id: auto
        name: auto

    @orm.type(Post, filters=orm.filter(Post))
    class FbAuthoredPostType:
        id: auto
        title: auto
        author: FbAuthorType

    @strawberry.type
    class Query:
        posts: list[FbAuthoredPostType] = orm.field.auto()

    return orm.schema(query=Query)


def _schema_for(shape):
    return _authored_schema if shape == "toOne" else _schema


def _listed(key, count, branch):
    branches = ", ".join([branch] * count)
    return f"{{{key}: [{branches}]}}"


def _root(field, filter_):
    return f"query ($v: String!) {{ {field}(filter: {filter_}) {{ id }} }}"


def _aliased(filter_, count):
    fields = " ".join(f"a{i}: users(filter: {filter_}) {{ id }}" for i in range(count))
    return f"query ($v: String!) {{ {fields} }}"


SHAPES = {
    "any": lambda lookup, n: _root("users", _listed("any", n, NAME % lookup)),
    "all": lambda lookup, n: _root("users", _listed("all", n, NAME % lookup)),
    "oneOf": lambda lookup, n: _root("users", _listed("oneOf", n, NAME % lookup)),
    "not": lambda lookup, n: _root(
        "users", _listed("all", n, "{not: %s}" % (NAME % lookup))
    ),
    "toMany": lambda lookup, n: _root(
        "users", _listed("all", n, "{object: {posts: %s}}" % (TITLE % lookup))
    ),
    "toOne": lambda lookup, n: _root(
        "posts", _listed("all", n, "{object: {author: %s}}" % (NAME % lookup))
    ),
    "aliases": lambda lookup, n: _aliased(NAME % lookup, n),
    "relationField": lambda lookup, n: (
        "query ($v: String!) { users { id posts(filter: "
        + _listed("any", n, TITLE % lookup)
        + ") { id } } }"
    ),
}

#: Every lookup in one shape, and every shape with a text and a regex lookup:
#: the shape decides what the budget walks, the lookup what it counts.
CELLS = sorted(
    {("any", lookup) for lookup in TEXT + REGEX}
    | {(shape, lookup) for shape in SHAPES for lookup in ("contains", "regex")}
)


def _value(lookup):
    return VALUES.get(lookup, TEXT_VALUE)


def _costly(sent):
    """The statements that ran a lookup the budget counts."""
    return [sql for sql in sent if "LIKE" in sql.upper() or "REGEXP" in sql.upper()]


def _messages(result):
    return [error.message for error in result.errors or []]


def _not_offered(result, lookup):
    assert any(
        f"unknown field '{lookup}'" in message for message in _messages(result)
    ), result.errors


class AbstractTestCostlyLookupBudget:
    @pytest.mark.parametrize(("shape", "lookup"), CELLS)
    async def test_as_many_as_the_budget_allows_run(
        self, make_orm, models, run, statements, regex_filters, lookup, shape
    ):
        schema = _schema_for(shape)(
            make_orm, models, enable_regex_filters=regex_filters, **LIMITS
        )
        with statements() as sent:
            result = await run(
                schema, SHAPES[shape](lookup, CAP), {"v": _value(lookup)}
            )
        if lookup in REGEX and not regex_filters:
            _not_offered(result, lookup)
            assert sent == []
            return
        assert result.errors is None, result.errors
        assert _costly(sent)

    @pytest.mark.parametrize(("shape", "lookup"), CELLS)
    async def test_one_more_is_refused_before_it_runs(
        self, make_orm, models, run, statements, regex_filters, lookup, shape
    ):
        schema = _schema_for(shape)(
            make_orm, models, enable_regex_filters=regex_filters, **LIMITS
        )
        query = SHAPES[shape](lookup, CAP + 1)
        with statements() as sent:
            result = await run(schema, query, {"v": _value(lookup)})
        if lookup in REGEX and not regex_filters:
            _not_offered(result, lookup)
            assert sent == []
            return
        unit = "regex lookups" if lookup in REGEX else "characters"
        limit = LIMITS[
            "max_regex_filters" if lookup in REGEX else "max_filter_text_length"
        ]
        assert any(
            unit in message and f"maximum is {limit}" in message
            for message in _messages(result)
        ), f"not refused, errors: {result.errors}; {len(_costly(sent))} statements ran"
        # Aliases already resolved before the one over the budget have run.
        assert len(_costly(sent)) <= (CAP if shape == "aliases" else 0), sent

    @pytest.mark.parametrize("lookup", TEXT)
    async def test_the_default_text_budget_is_a_thousand_characters(
        self, make_orm, models, run, lookup
    ):
        schema = _schema(make_orm, models)
        ten = [NAME % lookup] * 10
        one_more = f'{{field: {{email: {{{lookup}: "a"}}}}}}'
        query = "query ($v: String!) { users(filter: {any: [%s]}) { id } }"
        allowed = await run(schema, query % ", ".join(ten), {"v": "a" * 100})
        assert allowed.errors is None, allowed.errors
        refused = await run(
            schema, query % ", ".join([*ten, one_more]), {"v": "a" * 100}
        )
        assert any("maximum is 1000" in message for message in _messages(refused)), (
            f"1,001 characters not refused, errors: {refused.errors}"
        )

    @pytest.mark.parametrize("lookup", ["exact", "neq", "inList", "notInList"])
    async def test_a_lookup_costing_no_more_than_reading_the_value_is_not_counted(
        self, make_orm, models, run, lookup
    ):
        schema = _schema(make_orm, models, **LIMITS)
        listed = lookup in ("inList", "notInList")
        kind = "[String!]!" if listed else "String!"
        filter_ = _listed("any", 20, NAME % lookup)
        query = f"query ($v: {kind}) {{ users(filter: {filter_}) {{ id }} }}"
        value = ["a" * 50] if listed else "a" * 50
        result = await run(schema, query, {"v": value})
        assert result.errors is None, result.errors

    async def test_a_lookup_given_no_value_counts_nothing(self, make_orm, models, run):
        schema = _schema(make_orm, models, **LIMITS)
        filter_ = _listed("any", 20, "{field: {name: {contains: null}}}")
        query = f"{{ users(filter: {filter_}) {{ id }} }}"
        result = await run(schema, query)
        assert result.errors is None, result.errors


BOUND = 4
#: Seeded names: Alice and Charlie are longer than ``BOUND``, Bob is not.
SHORT = ["Bob"]


def _users(regex_filter):
    return f"query ($v: String!) {{ users(filter: {regex_filter}) {{ name }} }}"


def _names(result):
    assert result.errors is None, result.errors
    return sorted(row["name"] for row in result.data["users"])


def _assert_names(result, expected):
    names = _names(result)
    assert names == expected, f"matched {names}, expected {expected}"


class AbstractTestRegexOnBoundedText:
    def _schema(self, make_orm, models, **options):
        return _schema(
            make_orm,
            models,
            enable_regex_filters=True,
            max_regex_text_length=BOUND,
            **options,
        )

    @pytest.mark.parametrize("lookup", REGEX)
    async def test_a_value_longer_than_the_bound_does_not_match(
        self, make_orm, models, run, lookup
    ):
        schema = self._schema(make_orm, models)
        result = await run(schema, _users(NAME % lookup), {"v": "^[ABC]"})
        _assert_names(result, SHORT)

    @pytest.mark.parametrize("lookup", REGEX)
    async def test_not_keeps_it(self, make_orm, models, run, lookup):
        schema = self._schema(make_orm, models)
        result = await run(
            schema, _users("{not: %s}" % (NAME % lookup)), {"v": "^[AC]"}
        )
        _assert_names(result, ["Alice", "Bob", "Charlie"])

    async def test_through_a_to_one_relation(self, make_orm, models, run):
        schema = _authored_schema(
            make_orm, models, enable_regex_filters=True, max_regex_text_length=BOUND
        )
        result = await run(
            schema,
            "query ($v: String!) { posts(filter: {object: {author: %s}}) "
            "{ author { name } } }" % (NAME % "regex"),
            {"v": "^[AB]"},
        )
        assert result.errors is None, result.errors
        assert {row["author"]["name"] for row in result.data["posts"]} == {"Bob"}

    async def test_through_a_to_many_relation(self, make_orm, models, run):
        schema = self._schema(make_orm, models)
        result = await run(
            schema,
            _users("{object: {posts: %s}}" % (TITLE % "regex")),
            {"v": "^D"},
        )
        _assert_names(result, [])

    @pytest.mark.parametrize("lookup", REGEX)
    async def test_a_longer_value_never_reaches_the_regex_engine(
        self, make_orm, models, run, regexp_calls, lookup
    ):
        schema = self._schema(make_orm, models)
        with regexp_calls() as calls:
            result = await run(schema, _users(NAME % lookup), {"v": "^[ABC]"})
        assert result.errors is None, result.errors
        read = [value for _, value in calls if value is not None]
        assert read, "the regex never ran"
        longest = max(len(value) for value in read)
        assert longest <= BOUND, f"the regex ran on a {longest}-character value"

    async def test_a_value_as_long_as_the_bound_matches(self, make_orm, models, run):
        schema = _schema(
            make_orm, models, enable_regex_filters=True, max_regex_text_length=5
        )
        result = await run(schema, _users(NAME % "regex"), {"v": "^[ABC]"})
        _assert_names(result, ["Alice", "Bob"])


class AbstractTestRegexAsAnalysed:
    @pytest.mark.parametrize(
        ("lookup", "pattern", "executed"),
        [
            ("regex", "^B", "^B"),
            ("regex", "^b", "^b"),
            ("iRegex", "^b", "(?i)^b"),
            ("iRegex", "^BOB$", "(?i)^BOB$"),
        ],
    )
    async def test_the_database_runs_the_pattern_the_guard_analysed(
        self, make_orm, models, run, regexp_calls, lookup, pattern, executed
    ):
        schema = _schema(make_orm, models, enable_regex_filters=True)
        with regexp_calls() as calls:
            result = await run(schema, _users(NAME % lookup), {"v": pattern})
        assert result.errors is None, result.errors
        ran = {called for called, _ in calls}
        assert ran == {executed}, f"the database ran {ran}, not {executed!r}"

    @pytest.mark.parametrize(
        ("pattern", "names"), [("^b", ["Bob"]), ("E$", ["Alice", "Charlie"])]
    )
    async def test_iregex_ignores_case(self, make_orm, models, run, pattern, names):
        schema = _schema(make_orm, models, enable_regex_filters=True)
        result = await run(schema, _users(NAME % "iRegex"), {"v": pattern})
        _assert_names(result, names)

    async def test_regex_does_not(self, make_orm, models, run):
        schema = _schema(make_orm, models, enable_regex_filters=True)
        result = await run(schema, _users(NAME % "regex"), {"v": "^b"})
        _assert_names(result, [])

    @pytest.mark.parametrize("lookup", REGEX)
    @pytest.mark.parametrize(
        "pattern", [r"(?c)(a+)+$", r"\p{L}+\p{L}+!", r"[[:alpha:]]+!", r"a(?i)b"]
    )
    async def test_a_pattern_it_cannot_analyse_never_reaches_the_database(
        self, make_orm, models, run, statements, lookup, pattern
    ):
        schema = _schema(make_orm, models, enable_regex_filters=True)
        with statements() as sent:
            result = await run(schema, _users(NAME % lookup), {"v": pattern})
        assert any("cannot be analysed" in m for m in _messages(result)), (
            f"not refused, errors: {result.errors}; {len(sent)} statements ran"
        )
        assert sent == []
