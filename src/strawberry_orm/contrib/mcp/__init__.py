"""Let AI agents query a GraphQL API in plain language over MCP.

Install with ``pip install "strawberry-orm[contrib.mcp]"``::

    from strawberry_orm.contrib.mcp import QueryAgent, mcp_server

    agent = QueryAgent(schema, model="anthropic:claude-sonnet-5-5")
    app = mcp_server(agent, context_getter=get_context).streamable_http_app()
"""

from .agent import PAUSE, QueryAgent, QueryAgentError, QueryRejected
from .clarification import Clarification
from .server import mcp_server

__all__ = [
    "PAUSE",
    "Clarification",
    "QueryAgent",
    "QueryAgentError",
    "QueryRejected",
    "mcp_server",
]
