"""The regex guard refuses what it cannot analyse, and analyses what runs.

``check_regex`` decides from Python's parse of a pattern whether matching it
can backtrack for long. A pattern Python cannot parse was let through, to be
read by the database in a dialect of its own: ``(?c)(a+)+$`` is a PostgreSQL
option and a nested repeat, ``(?<n>a+)+$`` an ICU named group around one, and
``\\p{L}+\\p{L}+!`` two ICU repeats that trade letters for cubic time. A set
Python reads one way and warns it may read another (``[[:alpha:]]``, a POSIX
class to PostgreSQL and ICU) was analysed as Python's reading of it. Each is
now refused, since what is analysed has to be what the database runs.

A repeat of variable count runs its lookarounds again on every pass, and
``re.search`` starts the repeat again at every position, so what they read
multiplies the time by the value's length. Those reads are held to
``MAX_REGEX_LENGTH`` along with the counted repeats: nine ``(?=a{99})`` in
``(?:...a)*b``, 88 characters, took 200 ms on one 1,000-character value.
A counted repeat runs them again on every pass too, and the passes of a
repeat over what matches nothing read no further into the value, so nothing
bounds them but their counts: ``(?:(?:(?:a{0}){99}){99}){99}b``, 31
characters, took 3.2 s on one 256-character value, and 33 ms inside a
lookahead. Such steps are held to 1,000 at each place in the value.

``iRegex`` runs ``(?i)`` before the pattern on SQLite, so the guard analyses
that and says so.
"""

import pytest

from strawberry_orm.filters import MAX_REGEX_LENGTH, check_regex

UNPARSEABLE = [
    r"(?c)(a+)+$",
    r"(?b)a+",
    r"\m(a+)+\M",
    r"\y\w*\y",
    r"a(?i)b",
    r"(?<n>a+)+$",
    r"***:(a+)+$",
    r"\p{L}+\p{L}+!",
    r"a{1,2}{3}",
    "(",
    "a{4294967296}",
    "a{99999999999999999999}",
]

READ_ANOTHER_WAY = [
    r"[[:alpha:]]*[[:alpha:]]*!",
    r"[[:<:]]a",
    r"[a-z--[aeiou]]+",
    r"[a&&b]",
    r"[a||b]",
    r"[a~~b]",
    r"[[a-z]]",
]

LOOKAROUNDS_IN_A_REPEAT = [
    "(?:" + "(?=a{99})" * 9 + "a)*b",
    r"(?:(?=a{60})(?=a{60})a)*b",
    r"(?:a(?<=a{101}))*b",
    r"(?:(?!a{101})b)*c",
    r"(?:(?=a{101})a)*+b",
    r"(?:(?=a{51}){2}a)+b",
    r"(?:(?=(?:ab){51})a)*b",
    r"(?:(?>(?=a{101})a))*b",
]

#: Steps at one place in the value that no value's length bounds.
IN_PLACE = [
    r"(?:(?:(?:a{0}){99}){99}){99}b",
    r"(?=(?:(?:a{0}){99}){99})b",
    r"(?:(?:){99}){99}b",
    r"(?:(?:\b){99}){99}b",
    r"(?:(?=a{99})){11}b",
    r"(?:(?=a{99})a){11}b",
    r"(?:(?=a{99})a){1000}b",
    r"(?:(?=a{99})){5}(?:(?=b{99})){6}c",
]

STILL_ALLOWED = [
    "",
    r"^A",
    r"[^@]+@.+",
    r"\w+\d{4}",
    r"(?x) a b c",
    r"(?i)abc",
    r"(?:(?!foo).)*bar",
    r"(?:(?=a{90})a)*b",
    r"(?:(?=a{50})a)*b(?:(?=c{50})c)*d",
    r"(?=a{99})a",
    r"(?:(?=a{99})a)?b",
    r"(?:(?=a{99})a){3}b",
    r"(?:(?=a{99})a){10}b",
    r"(?:(?=a{99})){10}b",
    r"(?:\b){99}a",
    r"^(?=.*\d)(?=.*[a-z]).{8,}$",
    r"(?=a+)b",
]


class TestUnanalysablePatterns:
    @pytest.mark.parametrize("pattern", UNPARSEABLE)
    def test_a_pattern_python_cannot_parse_is_refused(self, pattern):
        with pytest.raises(ValueError, match="cannot be analysed"):
            check_regex(pattern)

    @pytest.mark.parametrize("pattern", UNPARSEABLE)
    def test_it_is_refused_case_insensitively_too(self, pattern):
        with pytest.raises(ValueError, match="cannot be analysed"):
            check_regex(pattern, ignore_case=True)

    @pytest.mark.parametrize("pattern", READ_ANOTHER_WAY)
    def test_a_set_python_may_read_another_way_is_refused(self, pattern):
        with pytest.raises(ValueError, match="cannot be analysed"):
            check_regex(pattern)

    def test_the_cause_is_kept_for_the_logs(self):
        with pytest.raises(ValueError) as refused:
            check_regex(r"(?c)(a+)+$")
        assert refused.value.__cause__ is not None


class TestLookaroundsInARepeat:
    @pytest.mark.parametrize("pattern", LOOKAROUNDS_IN_A_REPEAT)
    def test_lookarounds_reading_too_much_on_every_pass_are_refused(self, pattern):
        assert len(pattern) <= MAX_REGEX_LENGTH
        with pytest.raises(ValueError, match="lookarounds"):
            check_regex(pattern)

    @pytest.mark.parametrize("pattern", STILL_ALLOWED)
    def test_a_pattern_within_the_bounds_is_let_through(self, pattern):
        check_regex(pattern)

    def test_the_reads_of_every_repeat_add_up(self):
        check_regex(r"(?:(?=a{50})a)*b(?:(?=c{50})c)*d")
        with pytest.raises(ValueError, match="lookarounds"):
            check_regex(r"(?:(?=a{50})a)*b(?:(?=c{51})c)*d")

    def test_they_share_the_budget_of_counted_repeats(self):
        with pytest.raises(ValueError, match="counted repeats"):
            check_regex(r"a*(?=a{60})b(?:(?=c{50})c)*d")


class TestStepsInPlace:
    @pytest.mark.parametrize("pattern", IN_PLACE)
    def test_steps_no_value_bounds_are_held_to_a_cap(self, pattern):
        with pytest.raises(ValueError, match="at one place in a value"):
            check_regex(pattern)


class TestWhatIsAnalysed:
    @pytest.mark.parametrize("pattern", [r"[^@]+@.+", r"\w+\d{4}", "alice", ""])
    def test_ignoring_case_analyses_the_pattern_sqlite_runs(self, pattern):
        assert check_regex(pattern, ignore_case=True) == "(?i)" + pattern

    def test_a_pattern_that_would_set_flags_again_is_refused(self):
        with pytest.raises(ValueError, match="cannot be analysed"):
            check_regex(r"a(?x)b", ignore_case=True)

    def test_the_length_cap_is_on_what_the_client_sent(self):
        pattern = "a" * MAX_REGEX_LENGTH
        assert check_regex(pattern, ignore_case=True) == "(?i)" + pattern
