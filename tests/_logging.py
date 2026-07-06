"""Shared logging setup for tests.

Tests should write into the same ``bluepopcorn.log`` the daemon writes
to, with a ``[TEST]`` prefix on every record so test activity is
distinguishable from production at a glance:

    grep "\\[TEST\\]" bluepopcorn.log    # only test runs
    grep -v "\\[TEST\\]" bluepopcorn.log # only daemon traffic

The test process appends WITHOUT rotation — the daemon is the single
rotation owner. Two RotatingFileHandlers in separate processes are not
safe: each holds its own fd and rotates independently, clobbering each
other's ``.log.1`` and losing records. A plain append (O_APPEND, small
records) is atomic at the OS level on POSIX; if the daemon rotates
mid-test, the remaining test output lands in the rotated backup, which
is acceptable for test traffic.
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

    # File handler: same path as the daemon (so `tail -f bluepopcorn.log`
    # shows test runs interleaved with bot activity) but NO rotation —
    # the daemon is the single rotation owner (see module docstring).
    log_path = settings.resolve_path(settings.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path)
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
