"""Serve a GraphQL API's operations as MCP tools.

Install with ``pip install "strawberry-orm[contrib.mcp]"``::

    from strawberry_orm.contrib.mcp import mcp_server

    server = mcp_server(schema, operations=OPERATIONS, context_getter=get_context)
    app = server.streamable_http_app()
"""

from .server import GraphQLTool, mcp_server

__all__ = ["GraphQLTool", "mcp_server"]
