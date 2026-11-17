from __future__ import annotations

import warnings

import pytest


@pytest.fixture(autouse=True)
def _servers_here_are_never_served():
    """Most servers these tests build have mutation tools and no
    authentication; ``test_auth_warning`` checks the warning that brings."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="This MCP server's tools can change data"
        )
        yield
