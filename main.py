"""Entry point for the all-about-ads MCP server."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Must be set before FastMCP() is constructed (happens at src.server import time).
# FASTMCP_ prefix is read by pydantic-settings inside FastMCP.
if os.getenv("MCP_TRANSPORT") == "streamable-http":
    os.environ.setdefault("FASTMCP_HOST", "0.0.0.0")
    os.environ.setdefault("FASTMCP_PORT", os.getenv("PORT", "8000"))

from src.server import mcp
import src.tools  # noqa: F401  (registers tools on the server)
import src.resources  # noqa: F401  (registers resources on the server)


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
