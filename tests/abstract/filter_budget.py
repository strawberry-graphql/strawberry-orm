"""Filter limits are a budget for the whole filter, not for each list in it.

A GraphQL variable can be referenced any number of times, so a list or a set
of branches that is within its limit once can be repeated until the database
binds millions of values. ``max_filter_branches`` and ``max_in_list_size``
count every branch and every listed value across one filter argument, each
reference to a variable included, and the filter is refused before any SQL is
built. ``max_filter_depth`` counts every level, through the foreign-key
shortcut too.

Backends supply:

``make_orm(**limits)``              a fresh ORM with those limits
``models``                          ``User`` and ``Post``
``run(schema, query, variables)``   awaitable result of running *query*
``statements()``                    context manager yielding the list of SQL
                                    statements sent while it is open
"""

import strawberry

from strawberry_orm.types import auto

ALICE = '{ field: { name: { exact: "Alice" } } }'


def _schema(make_orm, models, **limits):
    orm = make_orm(**limits)
    User, Post = models.User, models.Post
    user_filter = orm.filter(User)
    post_filter = orm.filter(Post)

    @orm.type(User, filters=user_filter)
    class UserType:
        id: auto
        name: auto

    @orm.type(Post, filters=post_filter)
    class PostType:
        id: auto
        title: auto
        author: UserType

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.auto()
        posts: list[PostType] = orm.field.auto()

    return orm.schema(query=Query)


def names_listed(count):
    return {
        "field": {"name": {"inList": [f"n{i}" for i in range(count - 1)] + ["Alice"]}}
    }


def refused(result, message):
    assert result.data is None or result.data.get("users") is None
    assert result.errors is not None
    assert message in str(result.errors[0].message), result.errors


def user_names(result):
    assert result.errors is None, result.errors
    return sorted(row["name"] for row in result.data["users"])


REUSED = "query ($f: UserFilter!) { users(filter: { any: [%s] }) { name } }"


class AbstractTestFilterBudget:
    async def test_every_reference_to_a_variable_counts_toward_the_list_budget(
        self, make_orm, models, run
    ):
        schema = _schema(make_orm, models)
        result = await run(schema, REUSED % "$f, $f", {"f": names_listed(300)})
        refused(result, "maximum is 500")

    async def test_one_reference_within_the_list_budget_still_runs(
        self, make_orm, models, run
    ):
        schema = _schema(make_orm, models)
        result = await run(schema, REUSED % "$f", {"f": names_listed(500)})
        assert user_names(result) == ["Alice"]

    async def test_lists_are_counted_across_the_whole_filter(
        self, make_orm, models, run
    ):
        schema = _schema(make_orm, models)
        names = ", ".join(f'"n{i}"' for i in range(260))
        query = (
            "{ users(filter: { any: ["
            f"{{ field: {{ name: {{ inList: [{names}] }} }} }}, "
            f"{{ field: {{ name: {{ notInList: [{names}] }} }} }}"
            "] }) { name } }"
        )
        refused(await run(schema, query), "maximum is 500")

    async def test_branches_are_counted_across_the_whole_filter(
        self, make_orm, models, run
    ):
        schema = _schema(make_orm, models)
        inner = "{ any: [" + ", ".join([ALICE] * 31) + "] }"
        query = f"{{ users(filter: {{ any: [{inner}, {inner}] }}) {{ name }} }}"
        refused(await run(schema, query), "maximum is 50")

    async def test_every_reference_to_a_variable_counts_toward_the_branch_budget(
        self, make_orm, models, run
    ):
        schema = _schema(make_orm, models)
        variables = {"f": {"any": [{"field": {"name": {"exact": "Alice"}}}] * 30}}
        result = await run(schema, REUSED % "$f, $f", variables)
        refused(result, "maximum is 50")

    async def test_a_filter_within_the_branch_budget_still_runs(
        self, make_orm, models, run
    ):
        schema = _schema(make_orm, models)
        inner = "{ any: [" + ", ".join([ALICE] * 23) + "] }"
        query = f"{{ users(filter: {{ any: [{inner}, {inner}] }}) {{ name }} }}"
        assert user_names(await run(schema, query)) == ["Alice"]

    async def test_a_small_request_cannot_bind_a_million_values(
        self, make_orm, models, run, statements
    ):
        schema = _schema(make_orm, models)
        group = "{ any: [" + ", ".join(["$f"] * 50) + "] }"
        query = REUSED % ", ".join([group] * 40)
        variables = {"f": names_listed(500)}
        listed = variables["f"]["field"]["name"]["inList"]
        assert len(query) + sum(len(name) + 3 for name in listed) < 12 * 1024
        with statements() as sent:
            result = await run(schema, query, variables)
        refused(result, "maximum is")
        assert sent == []

    async def test_depth_is_counted_through_the_foreign_key_shortcut(
        self, make_orm, models, run
    ):
        schema = _schema(make_orm, models, max_filter_depth=2)
        query = (
            "{ posts(filter: { object: { author: { not: { not: { not: "
            "{ field: { id: { exact: 1 } } } } } } } }) { title } }"
        )
        result = await run(schema, query)
        assert result.errors is not None
        assert "maximum depth of 2" in str(result.errors[0].message)

    async def test_the_foreign_key_shortcut_within_the_depth_still_runs(
        self, make_orm, models, run
    ):
        schema = _schema(make_orm, models, max_filter_depth=2)
        query = (
            "{ posts(filter: { object: { author: { not: "
            "{ field: { id: { exact: 1 } } } } } }) { title } }"
        )
        result = await run(schema, query)
        assert result.errors is None, result.errors
        titles = sorted(row["title"] for row in result.data["posts"])
        assert titles == ["Draft Post", "Rust Adventures"]
