"""Entry point for the all-about-ads MCP server."""

import sys
print(">> main.py started", flush=True)

import os
import traceback
from pathlib import Path

print(f">> MCP_TRANSPORT={os.getenv('MCP_TRANSPORT')} PORT={os.getenv('PORT')}", flush=True)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")

    # Must be set before FastMCP() is constructed (happens at src.server import time).
    # FASTMCP_ prefix is read by pydantic-settings inside FastMCP.
    if os.getenv("MCP_TRANSPORT") == "streamable-http":
        os.environ.setdefault("FASTMCP_HOST", "0.0.0.0")
        os.environ.setdefault("FASTMCP_PORT", os.getenv("PORT", "8000"))

    print(f">> FASTMCP_HOST={os.getenv('FASTMCP_HOST')} FASTMCP_PORT={os.getenv('FASTMCP_PORT')}", flush=True)

    from src.server import mcp
    import src.tools  # noqa: F401
    import src.resources  # noqa: F401

    print(">> imports done, starting server", flush=True)

except Exception:
    traceback.print_exc()
    sys.exit(1)


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    print(f">> mcp.run(transport={transport})", flush=True)
    try:
        mcp.run(transport=transport)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
