"""Client values quoted in messages, cut to a length a log line can hold.

A refusal raised in this library reaches the client with its message, and
Strawberry logs every error at ERROR, so a message quoting what a client sent
would carry a value of any length into both, on every request.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: How many characters of a client's value a message quotes.
MOST_QUOTED = 40
#: How many of a client's values a message names.
MOST_NAMED = 5


def quoted(value: Any) -> str:
    """``repr()`` of *value*, cut to :data:`MOST_QUOTED` characters."""
    if isinstance(value, str):
        if len(value) <= MOST_QUOTED:
            return repr(value)
        return f"{value[:MOST_QUOTED]!r}…"
    if isinstance(value, int) and value.bit_length() > 128:
        # repr() of a long int is refused past 4,300 digits.
        return f"a {value.bit_length()}-bit integer"
    text = repr(value)
    return text if len(text) <= MOST_QUOTED else f"{text[:MOST_QUOTED]}…"


def quoted_list(values: Sequence[Any]) -> str:
    """The first :data:`MOST_NAMED` of *values*, quoted, and how many more there are."""
    shown = ", ".join(quoted(value) for value in values[:MOST_NAMED])
    more = len(values) - MOST_NAMED
    return f"[{shown}, and {more} more]" if more > 0 else f"[{shown}]"
