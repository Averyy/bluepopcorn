"""Entry point for BluePopcorn MCP server: uv run -m bluepopcorn.mcp [--stdio]"""

import argparse
import asyncio


def main() -> None:
    parser = argparse.ArgumentParser(description="BluePopcorn MCP server")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run in stdio mode (for local MCP clients like Claude Desktop)",
    )
    args = parser.parse_args()

    from .config import load_config
    config = load_config()

    if args.stdio:
        asyncio.run(run_stdio_mode(config))
    else:
        asyncio.run(run_http_mode(config))


async def run_stdio_mode(config) -> None:
    """Run in stdio mode (local MCP server)."""
    from .server import run_stdio_server
    await run_stdio_server(config)


async def run_http_mode(config) -> None:
    """Run in HTTP mode (remote deployment)."""
    from .http.app import run_http_server
    await run_http_server(config)


if __name__ == "__main__":
    main()
