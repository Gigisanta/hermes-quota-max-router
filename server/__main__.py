"""Hermes QuotaMax Router — FastAPI server entry point.

Usage:
    python -m server.app            # run with defaults
    uvicorn server.app:build_app --factory
    pip install -e . && quotamax    # via the [project.scripts] entry
"""
from __future__ import annotations

import os

from server.app import main

if __name__ == "__main__":
    # iter 15: opt-in to JSON structured logging if requested.
    if os.environ.get("ROUTER_LOG_FORMAT", "").strip().lower() == "json":
        from server.logging_config import configure_json_logging
        configure_json_logging(os.environ.get("ROUTER_LOG_LEVEL", "INFO"))
    raise SystemExit(main())
