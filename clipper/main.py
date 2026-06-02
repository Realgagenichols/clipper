"""clipper CLI entry point."""

from __future__ import annotations

import argparse
import logging

from clipper import setup_logging

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipper",
        description=(
            "MCP server that drives a Flipper Zero over USB serial. "
            "Configuration is read from environment variables: "
            "CLIPPER_LOG_LEVEL, CLIPPER_SAFETY (or legacy CLIPPER_ALLOW_EMIT), "
            "CLIPPER_SGHZ_ALLOWED_MHZ, CLIPPER_FLIPPER_PORT."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "mcp-stdio",
        help=(
            "Run the MCP server over stdio "
            "(for `claude mcp add clipper -- uv run clipper mcp-stdio`)"
        ),
    )
    return parser


def cmd_mcp_stdio() -> int:
    from clipper.mcp import run_stdio

    log.info("starting clipper MCP server on stdio")
    run_stdio()
    return 0


def main() -> int:
    setup_logging()
    args = build_parser().parse_args()
    if args.command == "mcp-stdio":
        return cmd_mcp_stdio()
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
