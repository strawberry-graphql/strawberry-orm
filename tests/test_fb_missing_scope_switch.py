"""The missing-scope warning is off only when someone turns it off.

It was turned off for every ORM built while ``pytest`` was imported, and a
production process can import it: a health check reusing a test helper, a
plugin, a REPL. That silenced the one warning that a type clients can reach
has no ``scope_rows``. Now ``warn_missing_scope=False`` turns it off for one
ORM, and ``STRAWBERRY_ORM_WARN_MISSING_SCOPE=0`` for every ORM built in the
process that does not say, which is how this suite's ``conftest.py`` does it.
"""

import os
import sys

import pytest

from strawberry_orm import StrawberryORM

ENVIRONMENT = "STRAWBERRY_ORM_WARN_MISSING_SCOPE"

BUILDERS = {
    "sqlalchemy": lambda **options: StrawberryORM.for_sqlalchemy(
        dialect="sqlite", **options
    ),
    "django": StrawberryORM.for_django,
    "tortoise": StrawberryORM.for_tortoise,
}


@pytest.fixture(params=sorted(BUILDERS))
def build(request):
    return BUILDERS[request.param]


def warns(orm):
    return orm._backend._warn_missing_scope


def test_importing_pytest_does_not_turn_it_off(build, monkeypatch):
    monkeypatch.delenv(ENVIRONMENT, raising=False)
    assert "pytest" in sys.modules
    assert warns(build()) is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "OFF", " 0 "])
def test_the_environment_turns_it_off(build, monkeypatch, value):
    monkeypatch.setenv(ENVIRONMENT, value)
    assert warns(build()) is False


@pytest.mark.parametrize("value", ["", "1", "true", "on", "maybe"])
def test_any_other_value_leaves_it_on(build, monkeypatch, value):
    monkeypatch.setenv(ENVIRONMENT, value)
    assert warns(build()) is True


@pytest.mark.parametrize(("value", "option"), [("0", True), ("1", False)])
def test_an_option_given_to_the_orm_wins(build, monkeypatch, value, option):
    monkeypatch.setenv(ENVIRONMENT, value)
    assert warns(build(warn_missing_scope=option)) is option


def test_this_suite_turns_it_off_explicitly():
    assert os.environ[ENVIRONMENT] == "0"
