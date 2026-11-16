"""Which column and relation names generated inputs treat as sensitive-looking.

The original terms match anywhere in a name. The newer ones match only as
whole words of a snake_case, kebab-case or camelCase name, so they catch the
credential, identity number or privilege the name is about without sweeping up
ordinary names that happen to contain the letters.
"""

import pytest

from strawberry_orm.backends._base import _SENSITIVE_PATTERNS

SENSITIVE = [
    # Matched anywhere, as before.
    "password_hash",
    "hashedPassword",
    "password2",
    "client_secret",
    "refresh_token",
    "apiKey",
    "ssn",
    "is_admin",
    "isSuperuser",
    "user_permissions",
    "roles",
    # Credentials and second factors.
    "pwd",
    "passphrase",
    "pass_phrase",
    "passcode",
    "salt",
    "password_salt",
    "otp_code",
    "totp_seed",
    "totpSeed",
    "isTOTPEnabled",
    "hotp",
    "mfa_seed",
    "recovery_codes",
    "backupCodes",
    "pin",
    "card_pin",
    "userPIN",
    "pin_code",
    "PINCode",
    "jwt",
    "session_key",
    "api-key",
    "access_key",
    "aws_access_key_id",
    "AWSAccessKeyId",
    "signing_key",
    "encryptionKey",
    "auth_key",
    "license_key",
    "credentials",
    # Identity and payment numbers.
    "social_security_number",
    "national_id",
    "tax_id",
    "taxId",
    "passport_number",
    "card_number",
    "cvv",
    "cvv2",
    "cvc",
    "iban",
    "bank_account",
    # What a principal may do.
    "groups",
    "user_groups",
    "scopes",
    "oauth_scopes",
    "acl",
    "aclEntries",
    "privileges",
]

ORDINARY = [
    "name",
    "email",
    "title",
    "body",
    "word_count",
    "is_published",
    "tags",
    # Contain a newer term, but not as a word.
    "pinned",
    "is_pinned",
    "pinnedAt",
    "spinner",
    "opinion",
    "pins",
    "pin_count",
    "pinCount",
    "salty_snack",
    "basalt",
    "topics",
    "hotpot",
    "passenger",
    "author_key",
    "keyboard",
    "syntax_id",
    "international_id",
    "scorecard_number",
    "credentialed",
    "telescope",
    "oracle",
    # A newer term in a form that names ordinary data.
    "group",
    "group_id",
    "groupName",
    "groupSettings",
    "subgroups",
    "scope",
    "scoped_name",
    "session_id",
    # Weaker signals, left to the type's exclude=.
    "is_active",
    "is_verified",
    "email_verified",
    "date_of_birth",
    "dob",
    "nonce",
    "plan",
    "tier",
    "balance",
]


class TestSensitiveNames:
    @pytest.mark.parametrize("name", SENSITIVE)
    def test_a_sensitive_looking_name_is_matched(self, name):
        assert _SENSITIVE_PATTERNS.search(name)

    @pytest.mark.parametrize("name", ORDINARY)
    def test_an_ordinary_name_is_not(self, name):
        assert _SENSITIVE_PATTERNS.search(name) is None
