"""Shared logging setup for tests.

Tests should write into the same ``bluepopcorn.log`` the daemon writes
to, with a ``[TEST]`` prefix on every record so test activity is
distinguishable from production at a glance:

    grep "\\[TEST\\]" bluepopcorn.log    # only test runs
    grep -v "\\[TEST\\]" bluepopcorn.log # only daemon traffic

Same RotatingFileHandler config as the daemon (5MB / 3 backups). The
test process and the daemon both write to the file. Python file writes
of small log records are atomic at the OS level on POSIX, and rotation
in either process is safe because both use the same handler config —
worst case is interleaving within a single line, which we'd notice.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Any


def setup_test_logging(
    settings: Any,
    *,
    verbose: bool,
    label: str,
) -> None:
    """Wire test logging to write into ``bluepopcorn.log`` with a [TEST] prefix.

    ``label`` distinguishes which test produced the record (e.g. the
    daemon log shows ``[TEST request-honesty]`` vs ``[TEST conversations]``).
    Set ``verbose=True`` to surface DEBUG-level events from bluepopcorn
    modules during the run.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter(
        f"%(asctime)s %(levelname)-8s [TEST {label}] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler: same path & rotation policy as the daemon, so
    # `tail -f bluepopcorn.log` shows test runs interleaved with bot
    # activity in real time.
    log_path = settings.resolve_path(settings.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=3,
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Stdout handler for the developer running the test interactively.
    if sys.stdout.isatty():
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(fmt)
        root.addHandler(stdout_handler)

    # Quiet noisy libraries (httpx connection logs etc.)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
