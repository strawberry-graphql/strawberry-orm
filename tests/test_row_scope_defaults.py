"""What a backend of its own makes of a row scope until it says otherwise."""

import pytest

from strawberry_orm.backends._base import BaseBackend


def test_a_scope_allows_the_rows_it_returns():
    backend = BaseBackend()
    assert backend.call_row_scope(lambda query, info: [*query, info], [1], 2) == [1, 2]


def test_an_error_raised_by_a_scope_is_left_as_it_is():
    def refuse(query, info):
        raise LookupError("no tenant")

    with pytest.raises(LookupError, match="no tenant"):
        BaseBackend().call_row_scope(refuse, [], None)
