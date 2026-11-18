"""Record the warnings a block raises, for a test asserting on all of them."""

import warnings
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def recording(action: str = "always") -> Iterator[list[warnings.WarningMessage]]:
    """``warnings.catch_warnings(record=True)`` with *action* for every warning
    but ``ResourceWarning``.

    From Python 3.13 an SQLite connection left open warns as it is collected,
    and the collector runs whenever it likes, so a connection an earlier test
    left would warn inside any block.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter(action)
        warnings.simplefilter("ignore", ResourceWarning)
        yield caught
