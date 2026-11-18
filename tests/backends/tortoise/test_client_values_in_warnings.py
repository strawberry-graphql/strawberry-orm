import datetime
from contextlib import contextmanager

import pytest
from tortoise import fields, timezone

from strawberry_orm.backends.tortoise import _aware
from tests.abstract.client_values_in_warnings import (
    AbstractTestClientValuesInWarnings,
    no_warnings,
)
from tests.backends.tortoise.test_write_path_authorization import writes  # noqa: F401


class TestClientValuesInWarnings(AbstractTestClientValuesInWarnings):
    @staticmethod
    @contextmanager
    def time_zones():
        assert timezone.get_use_tz(), "Tortoise turns time zone support on by default"
        yield

    @staticmethod
    def stored(stamp):
        return timezone.make_aware(stamp)


def test_a_naive_time_is_read_as_tortoise_reads_it():
    """Tortoise warns about a naive time too, quoting it. SQLite's driver
    binds no time at all, so this compares the value with Tortoise's own."""
    field = fields.TimeField(null=True)
    field.model_field_name = "rings_at"
    at = datetime.time(7, 30)
    with pytest.warns(RuntimeWarning, match="naive time"):
        theirs = field.to_db_value(at, None)
    with no_warnings():
        ours = _aware(field, at)
        assert field.to_db_value(ours, None) == ours
    assert ours == theirs
    assert ours.tzinfo is not None
    assert _aware(field, None) is None
