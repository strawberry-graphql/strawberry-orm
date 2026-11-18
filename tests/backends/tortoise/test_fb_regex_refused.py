"""Tortoise offers no regex filter, and refuses one wherever it is asked for.

Tortoise 1.1.9 has no ``regex`` or ``iregex`` lookup: the filter a regex was
translated to raised ``Unknown filter param``, which reached the client as
``Invalid filter expression`` after the guard had passed it. Its
``posix_regex`` needs a SQLite connection opened with
``install_regexp_functions``, and offers no way to keep a value longer than
``max_regex_text_length`` from the regex. So ``enable_regex_filters=True`` is
refused when the ORM is built, generated filters never offer a regex, and a
lookup type of the app's own that offers one is refused when it is used.
"""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.backends.tortoise import _build_tortoise_lookup
from strawberry_orm.filters import StringLookup
from strawberry_orm.types import auto
from tests.backends.tortoise.models import User

REFUSAL = "Tortoise has no regex lookup"


def test_enabling_regex_filters_is_refused():
    with pytest.raises(ValueError, match=REFUSAL):
        StrawberryORM.for_tortoise(enable_regex_filters=True)


def test_leaving_them_off_is_fine():
    orm = StrawberryORM.for_tortoise(enable_regex_filters=False)
    assert orm._backend._enable_regex_filters is False


@pytest.mark.parametrize("op", ["regex", "i_regex"])
def test_the_filter_builder_refuses_a_regex_lookup(op):
    with pytest.raises(ValueError, match=REFUSAL):
        _build_tortoise_lookup("name", StringLookup(**{op: "^A"}), enable_regex=True)


async def test_a_lookup_type_of_the_apps_own_offering_one_is_refused(seed):
    @strawberry.input
    class FbRegexLookup:
        regex: str | None = strawberry.UNSET

    orm = StrawberryORM.for_tortoise(
        lazy_resolution="off",
        warn_missing_scope=False,
        filter_overrides={str: FbRegexLookup},
    )

    @orm.type(User, filters=orm.filter(User))
    class FbUserType:
        id: auto
        name: auto

    @strawberry.type
    class Query:
        users: list[FbUserType] = orm.field.auto()

    schema = orm.schema(query=Query)
    result = await schema.execute(
        '{ users(filter: {field: {name: {regex: "^A"}}}) { name } }'
    )
    assert result.errors is not None
    assert REFUSAL in result.errors[0].message, result.errors


async def test_the_filter_it_used_to_send_is_refused_without_the_columns(seed):
    from tortoise.expressions import Q

    from strawberry_orm.backends.tortoise import _tortoise_checked_q

    with pytest.raises(ValueError, match="^Invalid filter expression$") as caught:
        _tortoise_checked_q(User, Q(name__regex="^A"))
    assert caught.value.__cause__ is None
