from contextlib import nullcontext

from tests.abstract.client_values_in_warnings import (
    AbstractTestClientValuesInWarnings,
)
from tests.backends.sqlalchemy.test_write_path_authorization import (  # noqa: F401
    writes,
)


class TestClientValuesInWarnings(AbstractTestClientValuesInWarnings):
    @staticmethod
    def time_zones():
        return nullcontext()

    @staticmethod
    def stored(stamp):
        return stamp
