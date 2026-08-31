"""Structured JSON logging for the QuotaMax Router.

iter 15: the router previously used stdlib ``logging`` with no
structured format. This module provides ``configure_json_logging()``
which replaces the default formatter with a JSON line emitter
(``{"ts": "...", "level": "...", "logger": "...", "message": "..."}``).

Why JSON?
- Easy to ingest in Loki, ELK, Datadog, etc.
- Preserves structured context (``extra={"model_id": "..."}``).
- Single-line-per-event (grep-friendly, tail-friendly).
- No external deps — stdlib only.

We don't pull in ``structlog`` or ``python-json-logger`` to keep the
dependency surface minimal (the OSS promise).

Usage:
    from server.logging_config import configure_json_logging
    configure_json_logging(level="INFO")
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

# Standard LogRecord attributes — everything else in the record is
# from ``extra={...}`` and is part of the structured payload.
_STANDARD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record.

    Fields:
      - ``ts``     — ISO8601 UTC timestamp with millisecond precision
      - ``level``  — DEBUG / INFO / WARNING / ERROR / CRITICAL
      - ``logger`` — logger name
      - ``message``— the formatted message
      - everything in ``record.__dict__`` that isn't a stdlib attr
        (this includes ``extra={...}`` from the call site)
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(
                record.created,
                tz=UTC,
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Inject extra fields (anything passed via logger.info("...", extra={...})).
        for k, v in record.__dict__.items():
            if k in _STANDARD_ATTRS or k.startswith("_"):
                continue
            # Skip recursion / large objects
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)[:200]
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info
        # JSONL consumers rely on one physical line per event.  Escaping all
        # non-ASCII characters also escapes Unicode line separators (NEL,
        # U+2028, U+2029), which ``ensure_ascii=False`` would emit literally
        # and allow to split a record across lines.
        return json.dumps(payload, ensure_ascii=True)


def configure_json_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: safe to call multiple times. Replaces any existing
    handler on the root logger with a single ``StreamHandler`` writing
    JSON to stderr (the container/12-factor convention).
    """
    root = logging.getLogger()
    # Remove existing handlers we may have added before.
    for h in list(root.handlers):
        if getattr(h, "_qr_json_handler", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler._qr_json_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper() if isinstance(level, str) else level)
    # Tone down noisy third-party loggers.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def is_json_logging_enabled() -> bool:
    """True if the JSON handler is currently installed on the root logger."""
    return any(getattr(h, "_qr_json_handler", False) for h in logging.getLogger().handlers)


# Auto-enable when ROUTER_LOG_FORMAT=json (the standard env var for this).
if os.environ.get("ROUTER_LOG_FORMAT", "").strip().lower() == "json":
    _level = os.environ.get("ROUTER_LOG_LEVEL", "INFO")
    configure_json_logging(_level)
