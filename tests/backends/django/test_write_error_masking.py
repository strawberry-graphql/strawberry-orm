"""A write that fails tells the client one plain message (Django)."""

from contextlib import contextmanager
from unittest.mock import patch

from django.db.backends.utils import CursorWrapper

from tests.abstract.write_error_masking import AbstractTestWriteErrorMasking
from tests.backends.django.test_read_error_masking import FAMILIES, TYPICAL
from tests.backends.django.test_write_path_authorization import (  # noqa: F401
    writes,
)


class TestWriteErrorMasking(AbstractTestWriteErrorMasking):
    FAMILIES = FAMILIES
    TYPICAL = TYPICAL

    @contextmanager
    def failing(self, writes, error, verbs):  # noqa: F811
        def refusing(run):
            def refuse(cursor, sql, *args, **kwargs):
                if str(sql).lstrip().upper().startswith(verbs):
                    raise error
                return run(cursor, sql, *args, **kwargs)

            return refuse

        with (
            patch.object(CursorWrapper, "execute", refusing(CursorWrapper.execute)),
            patch.object(
                CursorWrapper, "executemany", refusing(CursorWrapper.executemany)
            ),
        ):
            yield
