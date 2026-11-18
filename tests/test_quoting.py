"""A client's value is quoted in a message cut to a length a log line holds."""

import pytest

from strawberry_orm._quoting import MOST_QUOTED, quoted, quoted_list


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", "'abc'"),
        ("x" * MOST_QUOTED, repr("x" * MOST_QUOTED)),
        ("x" * 50_000, f"{'x' * MOST_QUOTED!r}…"),
        (12, "12"),
        (10**5_000, "a 16610-bit integer"),
        (b"y" * 100, f"{repr(b'y' * 100)[:MOST_QUOTED]}…"),
    ],
    ids=["short", "at the limit", "long", "int", "long int", "bytes"],
)
def test_a_value_is_quoted_cut_short(value, expected):
    assert quoted(value) == expected


def test_a_list_names_its_first_values_and_counts_the_rest():
    assert quoted_list(["a", "b"]) == "['a', 'b']"
    assert (
        quoted_list([str(n) for n in range(8)])
        == "['0', '1', '2', '3', '4', and 3 more]"
    )
