"""Startup / shutdown / background-loop helpers for the QuotaMax Router.

iter 15: extracted from server/app.py:build_app() (the "god-object"
refactor). The quota auto-reset scheduler is a long-lived background
task started on app startup and cancelled on shutdown. Kept in its own
module so the loop is independently testable and tunable.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

log = logging.getLogger(__name__)


def make_quota_reset_loop(
    reset_fn: Callable[[], int],
    interval_s: float | None = None,
) -> Callable[[], asyncio.Task[None]]:
    """Return an async function that, when called, starts the quota
    auto-reset loop and returns the running ``asyncio.Task``.

    The loop calls ``reset_fn()`` every ``interval_s`` seconds (default
    1 hour, env override ``ROUTER_QUOTA_RESET_INTERVAL_S``). The return
    value is the number of models whose quota was reset; if non-zero,
    the loop logs the count. Errors are logged and swallowed — the
    loop is a maintenance task and should not crash the server.

    Disable with ``ROUTER_QUOTA_RESET_DISABLED=1``.
    """
    if interval_s is None:
        try:
            interval_s = float(os.environ.get("ROUTER_QUOTA_RESET_INTERVAL_S", "3600"))
        except ValueError:
            interval_s = 3600.0
    if interval_s <= 0:
        interval_s = 3600.0  # safety floor

    async def _loop() -> None:
        while True:
            try:
                n = reset_fn()
                if n:
                    log.info("quota_reset_loop: reset %d model(s)", n)
            except Exception as e:
                log.warning("quota_reset_loop: %s", e)
            await asyncio.sleep(interval_s)

    def _start() -> asyncio.Task[None]:
        return asyncio.create_task(_loop())

    return _start


def is_quota_reset_disabled() -> bool:
    """True if the env var ``ROUTER_QUOTA_RESET_DISABLED=1`` is set."""
    return os.environ.get("ROUTER_QUOTA_RESET_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
