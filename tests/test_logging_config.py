"""Tests for the iter 15 JSON structured logging."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from server.logging_config import (
    JsonFormatter,
    configure_json_logging,
    is_json_logging_enabled,
)


@pytest.fixture
def captured_log() -> Iterator[io.StringIO]:
    """Install a StringIO handler that captures JSON-formatted records."""
    buf = io.StringIO()
    handler = logging.StreamHandler(stream=buf)
    handler.setFormatter(JsonFormatter())
    handler._qr_capture = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    root.addHandler(handler)
    prev_level = root.level
    root.setLevel(logging.INFO)
    yield buf
    root.removeHandler(handler)
    root.setLevel(prev_level)


def test_json_formatter_basic(captured_log: io.StringIO) -> None:
    logging.getLogger("test.json").info("hello %s", "world")
    line = captured_log.getvalue().strip()
    obj = json.loads(line)
    assert obj["message"] == "hello world"
    assert obj["level"] == "INFO"
    assert obj["logger"] == "test.json"
    assert "ts" in obj
    # ts is ISO8601 with millisecond precision
    assert "T" in obj["ts"]
    assert obj["ts"].endswith("+00:00") or obj["ts"].endswith("Z")


def test_json_formatter_extra_fields(captured_log: io.StringIO) -> None:
    logging.getLogger("test.json").info(
        "model picked",
        extra={"model_id": "deepseek/deepseek-r1-0528", "tokens": 1234},
    )
    obj = json.loads(captured_log.getvalue().strip())
    assert obj["model_id"] == "deepseek/deepseek-r1-0528"
    assert obj["tokens"] == 1234


def test_json_formatter_exception(captured_log: io.StringIO) -> None:
    try:
        raise ValueError("simulated")
    except ValueError:
        logging.getLogger("test.json").exception("oops")
    obj = json.loads(captured_log.getvalue().strip())
    assert obj["message"] == "oops"
    assert "exc" in obj
    assert "ValueError: simulated" in obj["exc"]


def test_json_formatter_repr_for_non_serializable(captured_log: io.StringIO) -> None:
    class _Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    logging.getLogger("test.json").info("x", extra={"obj": _Opaque()})
    obj = json.loads(captured_log.getvalue().strip())
    assert obj["obj"] == "<opaque>"


def test_configure_json_logging_idempotent() -> None:
    root = logging.getLogger()
    # Pre-condition: no JSON handler.
    for h in list(root.handlers):
        if getattr(h, "_qr_json_handler", False):
            root.removeHandler(h)
    assert not is_json_logging_enabled()
    configure_json_logging("INFO")
    n1 = sum(1 for h in root.handlers if getattr(h, "_qr_json_handler", False))
    configure_json_logging("DEBUG")
    n2 = sum(1 for h in root.handlers if getattr(h, "_qr_json_handler", False))
    assert n1 == 1, "first call should add exactly one JSON handler"
    assert n2 == 1, "second call should NOT add a second JSON handler (idempotent)"


def test_json_formatter_unicode_safe(captured_log: io.StringIO) -> None:
    """Chinese + Spanish + emoji in a single record must round-trip."""
    logging.getLogger("test.json").info("中文 + español + 🚀")
    obj = json.loads(captured_log.getvalue().strip())
    assert "中文" in obj["message"]
    assert "español" in obj["message"]
    assert "🚀" in obj["message"]
