"""Entry point for the all-about-ads MCP server."""

import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# When PORT is set (any cloud host), switch to HTTP automatically.
# Set FASTMCP_HOST before importing src.server so FastMCP() picks it up.
if os.getenv("PORT"):
    os.environ.setdefault("MCP_TRANSPORT", "streamable-http")
    os.environ.setdefault("FASTMCP_HOST", "0.0.0.0")

try:
    from src.server import mcp
    import src.tools  # noqa: F401
    import src.resources  # noqa: F401
except Exception:
    traceback.print_exc()
    sys.exit(1)


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    try:
        mcp.run(transport=transport)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
