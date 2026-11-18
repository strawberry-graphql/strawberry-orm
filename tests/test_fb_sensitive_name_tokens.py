"""Sensitive-looking names are recognised by their words, not one by one.

The backstop listed ``<word>_key`` names one qualifier at a time and ``pass``
only inside ``password``, ``passwd``, ``pass_phrase`` and ``pass_code``, so
``decryption_key``, ``aes_key``, ``key_material``, ``master_seed``,
``smtp_pass``, ``refresh_tok``, ``oauth_refresh`` and ``authorization`` stayed
writable in a generated ``orm.input()`` and were shown by ``@orm.type``
without the ``warn_sensitive`` warning. A name is now split into words, at
``_``, ``-`` and camelCase boundaries, and a run of letters such as
``aeskey`` into the words it is made of, after a word it does not know when
two or more follow (``awssecretaccesskey``). A word that names a secret on its
own (``token``, ``salt``, ``authorization``) marks the name, and so does a
noun such as ``key``, ``seed``, ``pass`` or ``material`` right after a word
that makes it one (``aes``, ``db``, ``master``, ``key``).

Telling words apart keeps ordinary names out: ``passenger``, ``compass`` and
``passage`` hold no word ``pass``, ``keyboard``, ``monkey``, ``turkey`` and
``keystone`` no word ``key``, and ``tokenizer_name`` names a tokenizer, not a
token. A word ending in one that names a secret on its own still counts
(``fcmtoken``, ``sysadmin``), so ``parole`` is left out as a ``role``, and
``token_count`` stays out of inputs: a word cannot tell an LLM's tokens from
an API's. ``include=`` keeps either.
"""

import warnings

from sqlalchemy import Integer, String
from sqlalchemy.orm import DeclarativeBase, mapped_column
from strawberry.utils.str_converters import to_camel_case

from strawberry_orm import StrawberryORM, auto
from strawberry_orm.backends._base import _SENSITIVE_PATTERNS

QUALIFIERS = [
    "decryption",
    "encryption",
    "aes",
    "rsa",
    "tls",
    "ssl",
    "private",
    "signing",
    "recovery",
    "master",
    "wrapped",
    "kms",
    "api",
    "secret",
    "access",
    "refresh",
    "session",
    "otp",
    "totp",
    "mfa",
    "smtp",
    "db",
]
NOUNS = [
    "key",
    "keys",
    "seed",
    "secret",
    "token",
    "tok",
    "pass",
    "passwd",
    "password",
    "pin",
    "salt",
    "hash",
    "material",
]


def _spellings(*words):
    """snake_case, camelCase, PascalCase, SCREAMING_SNAKE and kebab-case."""
    return [
        "_".join(words),
        words[0] + "".join(word.title() for word in words[1:]),
        "".join(word.title() for word in words),
        "_".join(words).upper(),
        "-".join(words),
    ]


PAIRS = [(qualifier, noun) for qualifier in QUALIFIERS for noun in NOUNS]
OWN_WORDS = [("otpauth", "url"), ("authorization",), ("credentials",), ("bearer",)]
GENERATED = sorted(
    {name for words in [*PAIRS, *OWN_WORDS] for name in _spellings(*words)}
)

#: Each was let through.
REPORTED = [
    "decryption_key",
    "aes_key",
    "tls_key",
    "wrapped_key",
    "key_material",
    "recovery_key",
    "master_seed",
    "otpauth_url",
    "smtp_pass",
    "db_pass",
    "refresh_tok",
    "oauth_refresh",
    "authorization",
]

#: Run together, a name is split into the words it is made of.
RUN_TOGETHER = [
    "aeskey",
    "dbpass",
    "smtppass",
    "masterseed",
    "keymaterial",
    "refreshtok",
    "oauthrefresh",
    "otpauthurl",
    "decryptionkey",
    "fcmtoken",
    "sysadmin",
    "ed25519_key",
    "aes256Key",
    "TLSKey",
    "DBPass",
    "OTPAuthURL",
    "user_aes_key",
    "aes_key_v2",
    "bearerToken",
    "authorization_header",
    "openapi_key",
    # After a word it does not know.
    "awssecretaccesskey",
    "githubrefreshtok",
    "appleaeskey",
]

BENIGN = [
    "passenger",
    "passengerCount",
    "keyboard",
    "keyboard_layout",
    "monkey",
    "monkeyPatch",
    "compass",
    "compass_heading",
    "passage",
    "passageText",
    "turkey",
    "keystone",
    "keystoneId",
    "tokenizer_name",
    "tokenizerName",
    "detokenized_text",
    # A noun no qualifier makes a secret.
    "primary_key",
    "foreign_key",
    "public_key",
    "sort_key",
    "cache_key",
    "partition_key",
    "idempotency_key",
    "raw_material",
    "course_materials",
    "boarding_pass",
    "bus_pass",
    "pass_rate",
    "tik_tok",
    "random_seed",
    # A qualifier with no secret after it.
    "api_version",
    "api_url",
    "session_count",
    "db_name",
    "dbname",
    "smtp_host",
    "tls_enabled",
    "master_branch",
    "recovery_email",
    "refresh_interval",
    "access_level",
    "private_notes",
    "signing_date",
    # A word that only begins with, or holds, one naming a secret.
    "secretary",
    "hashtag",
    "badminton",
    "administrative_area",
    "staffing_level",
    "classname",
    "controller",
    "hockey",
    "donkey",
    "turnkey",
    "hotkey",
    "keypad",
    "keycode",
    "bypass",
    "trespass",
    "encompass",
    "saltwater",
    # One known word after one it does not know.
    "cobalt",
    "whiskey",
    "jockey",
    "overpass",
]


BACKEND = StrawberryORM.for_sqlalchemy(dialect="sqlite", lazy_resolution="off")._backend


class TestTheWordsOfAName:
    def test_a_generated_secret_name_is_recognised(self):
        assert [
            name for name in GENERATED if not _SENSITIVE_PATTERNS.search(name)
        ] == []

    def test_a_reported_or_run_together_name_is_recognised(self):
        missed = REPORTED + RUN_TOGETHER
        assert [name for name in missed if not _SENSITIVE_PATTERNS.search(name)] == []

    def test_an_ordinary_name_is_not(self):
        assert [name for name in BENIGN if _SENSITIVE_PATTERNS.search(name)] == []


class TestWhereTheBackstopApplies:
    def test_a_generated_input_leaves_it_out(self):
        secret = GENERATED + REPORTED + RUN_TOGETHER
        exclude = BACKEND._exclude_generated_sensitive_field
        assert [name for name in secret if exclude(name, None) is not True] == []

    def test_a_generated_input_keeps_an_ordinary_name(self):
        exclude = BACKEND._exclude_generated_sensitive_field
        assert [name for name in BENIGN if exclude(name, None) is not False] == []

    def test_include_keeps_one(self):
        assert (
            BACKEND._exclude_generated_sensitive_field("aes_key", ["aes_key"]) is False
        )


def _identifiers(names):
    """Names a model can have as attributes, one for each GraphQL name."""
    kept = {}
    for name in sorted(names):
        if name.isidentifier():
            kept.setdefault(to_camel_case(name), name)
    return sorted(kept.values())


SECRET_COLUMNS = _identifiers(GENERATED + REPORTED + RUN_TOGETHER)
ORDINARY_COLUMNS = _identifiers(BENIGN)


class _Base(DeclarativeBase):
    pass


FbWideVault = type(
    "FbWideVault",
    (_Base,),
    {
        "__tablename__": "fb_wide_vault",
        "id": mapped_column(Integer, primary_key=True),
        **{name: mapped_column(String) for name in SECRET_COLUMNS + ORDINARY_COLUMNS},
    },
)


class TestEveryColumnOfAModel:
    def test_a_generated_input_leaves_out_every_secret_column(self):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
        )
        fields = set(orm.input(FbWideVault).__annotations__)
        assert fields & set(SECRET_COLUMNS) == set()
        assert set(ORDINARY_COLUMNS) <= fields

    def test_a_type_showing_them_warns_for_each_secret_and_no_other(self):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
        )
        shown = {"id": auto} | {
            name: auto for name in SECRET_COLUMNS + ORDINARY_COLUMNS
        }
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            orm.type(FbWideVault)(
                type("FbWideVaultType", (), {"__annotations__": shown})
            )
        warned = {
            str(warning.message).split("'")[1]
            for warning in caught
            if "looks sensitive" in str(warning.message)
        }
        assert warned == set(SECRET_COLUMNS)
