"""Entry point for the all-about-ads MCP server."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from src.server import mcp
import src.tools  # noqa: F401  (registers tools on the server)
import src.resources  # noqa: F401  (registers resources on the server)


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
