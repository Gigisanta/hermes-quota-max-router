"""Hermes QuotaMax Router — entrypoint shim.

Phase 0: delegates to LiteLLM's proxy server (config in config/config.yaml).
Phase 3+: this file will host the FastAPI app that fronts LiteLLM with the
Orchestrator, Quota Manager and Registry.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def phase0_run() -> None:
    """Launch the LiteLLM proxy in foreground (Phase 0 baseline)."""
    import subprocess

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    cmd = [
        "litellm",
        "--config", str(REPO_ROOT / "config" / "config.yaml"),
        "--port", os.environ.get("ROUTER_PORT", "4000"),
    ]
    print(f"[Phase 0] starting: {' '.join(cmd)}")
    sys.exit(subprocess.call(cmd, env=env, cwd=str(REPO_ROOT)))


def main() -> None:
    p = argparse.ArgumentParser(prog="hermes-quota-max-router")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="Run LiteLLM proxy (Phase 0)").set_defaults(fn=phase0_run)
    sub.add_parser("registry", help="Print registry summary").set_defaults(
        fn=lambda: __import__("core.model_registry", fromlist=["ModelRegistry"]).ModelRegistry()
        and None
    )

    args = p.parse_args()
    args.fn()


if __name__ == "__main__":
    main()
