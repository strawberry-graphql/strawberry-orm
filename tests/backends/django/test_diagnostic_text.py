"""What a client sends never reaches the warnings registry (Django)."""

import pytest
import strawberry
from asgiref.sync import sync_to_async
from django.test import override_settings

from strawberry_orm import StrawberryORM
from strawberry_orm._async import in_async_context
from strawberry_orm.types import auto
from tests.abstract.diagnostic_text import AbstractTestDiagnosticText
from tests.backends.django.models import User
from tests.backends.django.test_bounded_reads import (  # noqa: F401
    Item,
    Owner,
    UbOwner,
    hide,
    make_orm,
    run,
    seeded,
)
from tests.recorded_warnings import recording


@pytest.fixture
def owner_rows():
    def _owner_rows(info):
        def read():
            return list(UbOwner.objects.order_by("id"))

        return sync_to_async(read)() if in_async_context() else read()

    return _owner_rows


class TestDiagnosticText(AbstractTestDiagnosticText):
    pass


def _user_schema():
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(User, filters=orm.filter(User))
    class UbUserRow:
        id: auto
        name: auto
        created_at: auto

    @strawberry.type
    class Query:
        users: list[UbUserRow] = orm.field.eager()

    return orm.schema(query=Query)


CONDITIONS = {
    "gt": '{gt: "%s"}',
    "inList": '{inList: ["%s"]}',
    "range": '{range: {start: "%s", end: "2999-01-01T00:00:00"}}',
}


@pytest.mark.parametrize("zone", ["", "+00:00"])
@pytest.mark.parametrize("operator", list(CONDITIONS))
def test_a_datetime_a_client_filters_by_makes_no_warning(seed, operator, zone):
    """With time zones on, Django warns about a naive datetime, quoting it -
    a new ``__warningregistry__`` entry for every one a client sends. It is
    read in the default time zone, as Django would have."""
    schema = _user_schema()
    with override_settings(USE_TZ=True), recording("default") as caught:
        for second in range(5):
            stamp = f"2000-01-01T00:00:0{second}{zone}"
            result = schema.execute_sync(
                "{ users(filter: {field: {createdAt: %s}}) { name } }"
                % (CONDITIONS[operator] % stamp)
            )
            assert result.errors is None, result.errors
            expected = 0 if operator == "inList" else len(User.objects.all())
            assert len(result.data["users"]) == expected, result.data
    assert [str(w.message) for w in caught] == []


def test_a_datetime_filter_without_time_zones_warns_of_nothing(seed):
    schema = _user_schema()
    with recording("default") as caught:
        result = schema.execute_sync(
            '{ users(filter: {field: {createdAt: {gt: "2000-01-01T00:00:00"}}}) '
            "{ name } }"
        )
    assert result.errors is None, result.errors
    assert len(result.data["users"]) == len(User.objects.all())
    assert [str(w.message) for w in caught] == []


def test_a_datetime_that_does_not_parse_is_refused_unquoted(seed):
    schema = _user_schema()
    with override_settings(USE_TZ=True):
        result = schema.execute_sync(
            '{ users(filter: {field: {createdAt: {gt: "never-zzzz"}}}) { name } }'
        )
    assert [error.message for error in result.errors] == ["Invalid filter expression"]
