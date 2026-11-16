"""Generated inputs, filters and orders leave out sensitive-looking columns.

Which names look sensitive is a heuristic. The original terms (``password``,
``secret``, ``token``, ``admin``, ...) match anywhere in a name; newer ones
(``pin``, ``salt``, ``totp``, ``groups``, ...) only as whole words of it, so
``totp_seed`` is left out while ``pinned`` stays. ``include=`` keeps a column
the heuristic would drop.

Backends supply ``orm`` and ``models``, whose ``Account`` has ``name``,
``pinned``, ``password_hash``, ``is_admin`` and ``totp_seed`` columns.
"""

import pytest
from strawberry import relay

from strawberry_orm.types import auto
from tests.abstract.mutation_node_input_relations import _schema, shape


class AbstractTestSensitiveNames:
    def test_generated_inputs_leave_out_sensitive_looking_columns(self, orm, models):
        account = models.Account
        schema = _schema(
            input=orm.input(account),
            filter=orm.filter(account),
            order=orm.order(account),
        )
        assert shape(schema, "AccountInput") == {"name": "String", "pinned": "Boolean"}
        assert shape(schema, "AccountField") == {
            "id": "IntComparisonLookup",
            "name": "StringLookupNoRegex",
            "pinned": "BooleanLookup",
        }
        assert shape(schema, "AccountOrderField") == {
            "id": "Ordering",
            "name": "Ordering",
            "pinned": "Ordering",
        }

    def test_include_keeps_a_column_the_heuristic_drops(self, orm, models):
        schema = _schema(input=orm.input(models.Account, include=["name", "totp_seed"]))
        assert shape(schema, "AccountInput") == {
            "name": "String",
            "totpSeed": "String",
        }

    def test_a_type_showing_one_is_warned_about(self, orm, models):
        with pytest.warns(UserWarning, match="Field 'totp_seed' on Account looks"):

            @orm.type(models.Account)
            class AccountNode(relay.Node):
                id: relay.NodeID[int]
                totp_seed: auto
