"""Shared test configuration for strawberry-orm."""

import os
import warnings

import pytest
from sqlalchemy.exc import SAWarning

# Most tests build types with no ``scope_rows``; those about the warning pass
# ``warn_missing_scope`` themselves.
os.environ["STRAWBERRY_ORM_WARN_MISSING_SCOPE"] = "0"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Fail a test that runs SQL reading tables it never joins.

    SQLAlchemy warns of the cartesian product as it compiles the statement,
    whose criteria then answer for every row of one of the tables. The
    ``filterwarnings`` entry makes the warning an error, but the backends take
    an error compiling a statement for one the database refused, which is what
    a test of a refusal expects to see; so it is recorded here instead.
    """
    caught: list[warnings.WarningMessage] = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.filterwarnings("always", ".*cartesian product", SAWarning)
            outcome = yield
    finally:
        for warning in caught:
            if not _is_cartesian(warning):
                warnings.warn_explicit(
                    warning.message, warning.category, warning.filename, warning.lineno
                )
    unjoined = [warning for warning in caught if _is_cartesian(warning)]
    if unjoined:
        pytest.fail(
            "\n".join(f"{w.filename}:{w.lineno}: {w.message}" for w in unjoined),
            pytrace=False,
        )
    return outcome


def _is_cartesian(warning: warnings.WarningMessage) -> bool:
    return issubclass(warning.category, SAWarning) and (
        "cartesian product" in str(warning.message)
    )
