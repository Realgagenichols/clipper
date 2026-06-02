"""clipper — MCP server to drive a Flipper Zero over USB serial."""

from __future__ import annotations

import logging
import os
import sys


def setup_logging(level: int | None = None) -> None:
    if level is None:
        env = os.environ.get("CLIPPER_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, env, logging.INFO)
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)
