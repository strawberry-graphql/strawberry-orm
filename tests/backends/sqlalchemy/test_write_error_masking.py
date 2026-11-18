"""A write that fails tells the client one plain message (SQLAlchemy)."""

from contextlib import contextmanager

from sqlalchemy import event

from tests.abstract.write_error_masking import AbstractTestWriteErrorMasking
from tests.backends.sqlalchemy.test_read_error_masking import FAMILIES, TYPICAL
from tests.backends.sqlalchemy.test_write_path_authorization import (  # noqa: F401
    writes,
)


class TestWriteErrorMasking(AbstractTestWriteErrorMasking):
    FAMILIES = FAMILIES
    TYPICAL = TYPICAL

    @contextmanager
    def failing(self, writes, error, verbs):  # noqa: F811
        def refuse(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith(verbs):
                raise error

        bind = writes.session.bind
        engine = getattr(bind, "sync_engine", bind)
        event.listen(engine, "before_cursor_execute", refuse)
        try:
            yield
        finally:
            event.remove(engine, "before_cursor_execute", refuse)
