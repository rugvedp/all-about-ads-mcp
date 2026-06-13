"""Entry point for the all-about-ads MCP server (stdio transport)."""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from src.server import mcp
import src.tools  # noqa: F401  (registers tools on the server)
import src.resources  # noqa: F401  (registers resources on the server)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
