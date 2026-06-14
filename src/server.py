"""FastMCP server instance shared by tools and resources."""

import os

from mcp.server.fastmcp import FastMCP

_host = os.getenv("FASTMCP_HOST", "127.0.0.1")
_port = int(os.getenv("FASTMCP_PORT", os.getenv("PORT", "8000")))

mcp = FastMCP("all-about-ads", host=_host, port=_port)
