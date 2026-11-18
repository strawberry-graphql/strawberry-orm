"""A write that fails tells the client one plain message (Tortoise)."""

from tests.abstract.write_error_masking import AbstractTestWriteErrorMasking
from tests.backends.tortoise.test_read_error_masking import FAMILIES, TYPICAL, refusing
from tests.backends.tortoise.test_write_path_authorization import (  # noqa: F401
    writes,
)


class TestWriteErrorMasking(AbstractTestWriteErrorMasking):
    FAMILIES = FAMILIES
    TYPICAL = TYPICAL

    def failing(self, writes, error, verbs):  # noqa: F811
        return refusing(error, verbs)
