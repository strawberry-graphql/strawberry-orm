"""Every generated input, filter and order on every backend reads a name's words.

The audit found ``decryption_key``, ``otpauth_url``, ``smtp_pass``,
``oauth_refresh`` and ``master_seed`` writable in ``orm.input()`` and shown
without a warning, while ``password_hash`` was caught. They are left out of
generated inputs, filters and orders now, and a type showing one is warned
about, while ``keyboard_layout``, ``tokenizer_name`` and ``passenger_count``,
which hold no such word, stay.

Backends supply ``orm`` and ``models``, whose ``FbVault`` has ``name``, the
secret columns in ``SECRET`` and the ordinary ones in ``ORDINARY``.
"""

import warnings

from strawberry_orm.types import auto
from tests.abstract.mutation_node_input_relations import _schema, shape

SECRET = [
    "decryption_key",
    "otpauth_url",
    "smtp_pass",
    "oauth_refresh",
    "master_seed",
    "password_hash",
]
ORDINARY = ["name", "keyboard_layout", "tokenizer_name", "passenger_count"]
GRAPHQL = {
    "name": "name",
    "keyboard_layout": "keyboardLayout",
    "tokenizer_name": "tokenizerName",
    "passenger_count": "passengerCount",
}


class AbstractTestSensitiveNameWords:
    def test_generated_inputs_leave_out_the_secret_columns(self, orm, models):
        vault = models.FbVault
        schema = _schema(
            input=orm.input(vault),
            filter=orm.filter(vault),
            order=orm.order(vault),
        )
        ordinary = {GRAPHQL[name] for name in ORDINARY}
        assert set(shape(schema, "FbVaultInput")) == ordinary
        assert set(shape(schema, "FbVaultField")) == ordinary | {"id"}
        assert set(shape(schema, "FbVaultOrderField")) == ordinary | {"id"}

    def test_a_type_showing_them_is_warned_about_each(self, orm, models):
        shown = {"id": auto} | {name: auto for name in SECRET + ORDINARY}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            orm.type(models.FbVault)(
                type("FbVaultType", (), {"__annotations__": shown})
            )
        warned = {
            str(warning.message).split("'")[1]
            for warning in caught
            if "looks sensitive" in str(warning.message)
        }
        assert warned == set(SECRET)
