"""The regex guard compares what repeats can match exactly.

SQLite matches a regex with Python's ``re`` in the server's process, holding
the GIL, so a pattern ``check_regex`` lets through must cost at most time
quadratic in a value's length. Each pattern refused here was let through and
took time cubic in it: ``\\w*\\d*+!`` took 3.4 seconds on 1,600 digits. A
possessive repeat was never compared with the repeats before it, character
sets were compared only on a sample of characters, the ASCII flag was
ignored, ``İ`` was not counted as an ``i``, and a counted repeat was taken
for a short one however large its count.
"""

import re
import sys

import pytest

from strawberry_orm.filters import check_regex

TRADES = "may not hold two repeats"
COUNTED = "counted repeats"


class TestPossessiveRepeats:
    @pytest.mark.parametrize(
        "pattern",
        [
            r"\w*\d*+!",
            r"[0-9a-z_]*\d++!",
            r".*x*+y",
            r"[^@]*\w*+!",
            r"(?i).*K*+y",
            r".*[a-z]*+9",
            r".*x{0,99}+y",
            r".*(?>x)*+y",
            r".*(?>x*+)y",
            r".*(?=x*+y)",
            r"\w*(?>\d*)!",
            r"\w*(?>!?\d+)!",
        ],
    )
    def test_a_possessive_repeat_is_compared_with_the_repeats_still_open(self, pattern):
        with pytest.raises(ValueError, match=TRADES):
            check_regex(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            r"[a-z]*\d*+!",
            r"[^!]*!*+y",
            r"\d*+\w*!",
            r"[a-z]*(?>\d*)!",
            r"\w*(?>!\d*)#",
            r"\w*!*+[a-z]",
        ],
    )
    def test_a_possessive_repeat_no_open_repeat_can_match_is_allowed(self, pattern):
        check_regex(pattern)

    def test_a_possessive_repeat_that_can_match_nothing_leaves_repeats_open(self):
        with pytest.raises(ValueError, match=TRADES):
            check_regex(r"\w*!*+\d*#")


class TestExactOverlap:
    @pytest.mark.parametrize(
        "pattern",
        [
            r"\w*[^\x00-\u4e00]*!",
            r"\d*[^\x00-\u0900]*!",
            r"[^\x00-\u4e00]*\w*!",
            r"\w*[^\x00-\u3000]*!",
            r"[^\x00-\u4e00]*\w*+!",
            "[\U0001f000-\U0010ffff]*\\w*!",
        ],
    )
    def test_sets_that_share_only_characters_outside_latin_script_are_refused(
        self, pattern
    ):
        with pytest.raises(ValueError, match=TRADES):
            check_regex(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            r"(?a:[^\d])*\d*!",
            r"(?a:\W)*\w*!",
            r"(?a)\W*(?u:\w)*!",
            r"(?a:\S)*\s*!",
        ],
    )
    def test_a_set_the_ascii_flag_widens_is_compared_as_widened(self, pattern):
        with pytest.raises(ValueError, match=TRADES):
            check_regex(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            "\u0130*i*!",
            "(?i)[a-z]*\u0130*!",
            "\u212a*[a-z]*!",
            "\u017f*S*!",
            "\u03d1*\u03b8*!",
        ],
    )
    def test_characters_re_matches_ignoring_case_count_as_one(self, pattern):
        with pytest.raises(ValueError, match=TRADES):
            check_regex(pattern)

    def test_every_pair_re_matches_ignoring_case_counts_as_one(self):
        cased = "".join(
            ch for ch in map(chr, range(sys.maxunicode + 1)) if ch.lower() != ch.upper()
        )
        for ch in cased:
            folded = re.compile("(?i)" + re.escape(ch))
            for other in {match.group() for match in folded.finditer(cased)} - {ch}:
                with pytest.raises(ValueError, match=TRADES):
                    check_regex(f"{re.escape(ch)}*{re.escape(other)}*!")

    @pytest.mark.parametrize(
        "pattern",
        [
            r"(?a)\w*[^\x00-\x7f]*!",
            r"(?a:\d)*[\u0660-\u0669]*!",
            "[\\x00-\u4dff]*[\u4e00-\u9fff]*!",
            "(?i)\u00df*s*!",
            r"\d*\D*!",
            r"\s*\S+",
            r"(?s).*",
        ],
    )
    def test_sets_with_no_character_in_common_are_allowed(self, pattern):
        check_regex(pattern)


class TestCountedRepeats:
    @pytest.mark.parametrize(
        "pattern",
        [
            r".*x{500}y",
            r".*x{60}x{60}y",
            r".*(?:x{60}){60}y",
            r".*(?=x{200})y",
            r".*(?:z|x{200})y",
            r".*(?>x{200})y",
            r".*x{200}+y",
            r"\w*\d{101}!",
            r".*(x{60}){2}y",
            r".*(?:(?>x)){200}y",
            r".*(?:\bx){200}y",
            r".*(?:(?=x{9})x){90}y",
            r".*(?:(?=xxxxxxxx)x){90}y",
        ],
    )
    def test_counted_repeats_a_repeat_before_them_can_match_are_held_to_the_cap(
        self, pattern
    ):
        with pytest.raises(ValueError, match=COUNTED):
            check_regex(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            r"\w+\d{4}",
            r"^\d{3}-\d{3}-\d{4}$",
            r"^[0-9a-f]{128}$",
            r"[a-z]*\d{500}",
            r"\w*\d{100}!",
            r"(\d{3})+",
            r"[^!]*x{60}!x{60}",
        ],
    )
    def test_a_counted_repeat_within_the_cap_or_after_no_open_repeat_is_allowed(
        self, pattern
    ):
        check_regex(pattern)


@pytest.mark.parametrize(
    "pattern",
    [r"(\d{3})+", "^(foo|bar)$", "[^@]+@.+", r"[^a-c\dx]*\d+", "\u00df+S+"],
)
def test_the_documented_allowed_patterns_are_still_allowed(pattern):
    check_regex(pattern)
