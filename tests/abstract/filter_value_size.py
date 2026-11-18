"""The values a request's filters hand the database are bounded by size.

A GraphQL variable can be referenced any number of times, and a reference is
two tokens however long its value is, so the limits on how many values and
branches a filter holds do not bound what it binds: one 100 kB string sent
once, referenced from every slot of an ``inList`` under every alias, was
bound hundreds of times over. Every value a filter holds is measured, each
reference counted, against ``max_filter_value_length`` for the filter and
for the request.

Each place a value can be written - every kind of lookup, under ``any``,
``all``, ``oneOf``, ``not`` and ``object`` - is crossed with a filter just
under the limit, which runs, and one just over, which is refused before a
statement is sent. What reaches the database is measured as well.

A backend provides ``make_orm(**options)``, ``models`` (``User``, ``Post``,
with a text ``Post.body``), ``run(schema, query, variables)``, and
``bound()``: a context manager collecting, per statement, the characters of
the ``str`` parameters it was sent with.
"""

import pytest
import strawberry

from strawberry_orm.types import auto

LIMIT = 1_000
PIECE = "x" * 300


def _branches(lookup: str, refs: int) -> str:
    return ", ".join(f"{{field: {{body: {{{lookup}: $s}}}}}}" for _ in range(refs))


def _listed(lookup: str, refs: int) -> str:
    return f"{{field: {{body: {{{lookup}: [{', '.join(['$s'] * refs)}]}}}}}}"


#: Each place a value can go, as a ``users`` or ``posts`` filter holding
#: *refs* references to ``$s``. Text lookups have a budget of their own,
#: raised here so this one is what refuses them.
PLACES = {
    "inList": ("posts", lambda n: _listed("inList", n)),
    "notInList": ("posts", lambda n: _listed("notInList", n)),
    "exact under any": ("posts", lambda n: f"{{any: [{_branches('exact', n)}]}}"),
    "neq under all": ("posts", lambda n: f"{{all: [{_branches('neq', n)}]}}"),
    "exact under oneOf": ("posts", lambda n: f"{{oneOf: [{_branches('exact', n)}]}}"),
    "neq under not": ("posts", lambda n: f"{{not: {{any: [{_branches('neq', n)}]}}}}"),
    "contains under any": ("posts", lambda n: f"{{any: [{_branches('contains', n)}]}}"),
    "through object": (
        "users",
        lambda n: f"{{object: {{posts: {{any: [{_branches('exact', n)}]}}}}}}",
    ),
}


def _schema(make_orm, models, **options):
    orm = make_orm(warn_missing_scope=False, **options)

    @orm.type(models.Post, filters=orm.filter(models.Post))
    class SizedPost:
        id: auto
        body: auto

    @orm.type(models.User, filters=orm.filter(models.User))
    class SizedUser:
        id: auto
        posts: list[SizedPost]

    @strawberry.type
    class Query:
        posts: list[SizedPost] = orm.field.eager()
        users: list[SizedUser] = orm.field.eager()

    return orm.schema(query=Query)


def _query(root: str, filters: list[str]) -> str:
    fields = " ".join(
        f"a{i}: {root}(filter: {each}) {{ id }}" for i, each in enumerate(filters)
    )
    return f"query ($s: String!) {{ {fields} }}"


class AbstractTestFilterValueSize:
    @pytest.mark.parametrize("place", PLACES)
    async def test_a_filter_is_held_to_the_characters_it_binds(
        self, make_orm, models, run, bound, place
    ):
        root, build = PLACES[place]
        schema = _schema(
            make_orm,
            models,
            max_filter_value_length=LIMIT,
            max_filter_text_length=LIMIT * 10,
        )

        with bound() as under:
            result = await run(schema, _query(root, [build(3)]), {"s": PIECE})
        assert result.errors is None, result.errors
        assert sum(under) <= LIMIT, under

        with bound() as over:
            result = await run(schema, _query(root, [build(4)]), {"s": PIECE})
        messages = [error.message for error in result.errors or ()]
        assert messages == [
            f"Filter has over {LIMIT} characters of values in total; maximum is {LIMIT}"
        ], messages
        assert sum(over) == 0, over

    async def test_a_request_is_held_to_the_characters_its_filters_bind(
        self, make_orm, models, run, bound
    ):
        _root, build = PLACES["inList"]
        schema = _schema(make_orm, models, max_filter_value_length=LIMIT)

        result = await run(schema, _query("posts", [build(1)] * 3), {"s": PIECE})
        assert result.errors is None, result.errors

        with bound() as sizes:
            result = await run(schema, _query("posts", [build(1)] * 4), {"s": PIECE})
        messages = [error.message for error in result.errors or ()]
        assert (
            f"The request's filters have over {LIMIT} characters of values in "
            f"total; maximum is {LIMIT}"
        ) in messages, messages
        assert sum(sizes) <= LIMIT, sizes

    async def test_the_default_limit_holds_a_value_sent_once_and_referenced_twice(
        self, make_orm, models, run, bound
    ):
        _root, build = PLACES["inList"]
        schema = _schema(make_orm, models)
        value = "x" * 60_000

        result = await run(schema, _query("posts", [build(1)]), {"s": value})
        assert result.errors is None, result.errors

        with bound() as sizes:
            result = await run(schema, _query("posts", [build(2)]), {"s": value})
        messages = [error.message for error in result.errors or ()]
        assert messages == [
            "Filter has over 100000 characters of values in total; maximum is 100000"
        ], messages
        assert sum(sizes) == 0, sizes
