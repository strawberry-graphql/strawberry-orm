"""What a client sends never reaches the warnings registry (Tortoise)."""

import pytest

from tests.abstract.diagnostic_text import AbstractTestDiagnosticText
from tests.backends.tortoise.test_bounded_reads import (  # noqa: F401
    Item,
    Owner,
    UbOwner,
    hide,
    make_orm,
    run,
    seeded,
)


@pytest.fixture
def owner_rows():
    def _owner_rows(info):
        async def read():
            return list(await UbOwner.all().order_by("id"))

        return read()

    return _owner_rows


class TestDiagnosticText(AbstractTestDiagnosticText):
    pass
