#!/usr/bin/env python3
"""Install / register QuotaMax Router in the current user's Hermes Agent config.

Idempotent. Run from a fresh checkout or to re-apply after a config reset.

What it does:
  1. Symlinks the plugin into ~/.hermes/plugins/model-providers/ (if missing).
  2. Patches ~/.hermes/config.yaml to add:
       auxiliary.quotamax_subagent:  (provider=quotamax-router, model=auto)
       delegation.subagent_models.quotamax: quotamax-router/auto
  3. Backs up the config to ~/.hermes/config.yaml.backup-quotamax-<timestamp>.
  4. Verifies the plugin is discovered and the config is well-formed.

Usage:
  python scripts/install_hermes_plugin.py           # install
  python scripts/install_hermes_plugin.py --uninstall  # remove
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/Users/prueba/.hermes"))
PLUGIN_NAME = "quotamax-router"
SOURCE_PLUGIN = REPO_ROOT / "scripts" / "hermes_plugin" / PLUGIN_NAME
TARGET_PLUGIN = HERMES_HOME / "plugins" / "model-providers" / PLUGIN_NAME
CONFIG_PATH = HERMES_HOME / "config.yaml"


def install_plugin_symlink() -> None:
    """Symlink the plugin into Hermes' plugins tree."""
    if not SOURCE_PLUGIN.exists():
        print(f"FAIL: source plugin not found: {SOURCE_PLUGIN}", file=sys.stderr)
        sys.exit(1)
    if TARGET_PLUGIN.exists() or TARGET_PLUGIN.is_symlink():
        if TARGET_PLUGIN.is_symlink() and TARGET_PLUGIN.resolve() == SOURCE_PLUGIN.resolve():
            print(f"OK   : plugin already symlinked at {TARGET_PLUGIN}")
            return
        # Real (non-symlink) install — leave it alone, just notify.
        print(f"NOTE : {TARGET_PLUGIN} already exists (real, not symlink). Leaving in place.")
        return
    TARGET_PLUGIN.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(SOURCE_PLUGIN, TARGET_PLUGIN)
    print(f"OK   : plugin symlinked: {TARGET_PLUGIN} -> {SOURCE_PLUGIN}")


def uninstall_plugin_symlink() -> None:
    if TARGET_PLUGIN.is_symlink():
        TARGET_PLUGIN.unlink()
        print(f"OK   : plugin symlink removed: {TARGET_PLUGIN}")
    elif TARGET_PLUGIN.exists():
        print(f"NOTE : {TARGET_PLUGIN} is a real directory, not a symlink. Leaving in place.")
    else:
        print(f"OK   : plugin already absent: {TARGET_PLUGIN}")


def patch_config_yaml() -> None:
    """Idempotently add auxiliary.quotamax_subagent and subagent_models.quotamax."""
    if not CONFIG_PATH.exists():
        print(f"WARN: {CONFIG_PATH} does not exist; skipping config patch.", file=sys.stderr)
        return

    # Backup first.
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = CONFIG_PATH.with_suffix(f".yaml.backup-quotamax-{ts}")
    shutil.copy2(CONFIG_PATH, backup)
    print(f"OK   : config backed up to {backup}")

    try:
        import yaml
    except ImportError:
        print("FAIL: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}

    # 1) auxiliary.quotamax_subagent
    aux = cfg.setdefault("auxiliary", {})
    aux.setdefault(
        "quotamax_subagent",
        {
            "provider": "quotamax-router",
            "model": "auto",
            "base_url": "${QUOTAMAX_BASE_URL:-http://127.0.0.1:8088/v1}",
            "api_key": "${QUOTAMAX_API_KEY:-}",
            "api_mode": "chat_completions",
            "timeout": 60,
            "extra_body": {},
        },
    )
    print("OK   : auxiliary.quotamax_subagent set")

    # 2) delegation.subagent_models.quotamax
    deleg = cfg.setdefault("delegation", {})
    sub = deleg.setdefault("subagent_models", {})
    sub.setdefault("quotamax", "quotamax-router/auto")
    print("OK   : delegation.subagent_models.quotamax set")

    out = yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False, allow_unicode=True)
    CONFIG_PATH.write_text(out)
    print("OK   : config.yaml updated")


def unpatch_config_yaml() -> None:
    if not CONFIG_PATH.exists():
        return
    try:
        import yaml
    except ImportError:
        return
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    aux = cfg.get("auxiliary", {})
    if "quotamax_subagent" in aux:
        del aux["quotamax_subagent"]
        print("OK   : removed auxiliary.quotamax_subagent")
    sub = cfg.get("delegation", {}).get("subagent_models", {})
    if "quotamax" in sub:
        del sub["quotamax"]
        print("OK   : removed delegation.subagent_models.quotamax")
    out = yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False, allow_unicode=True)
    CONFIG_PATH.write_text(out)
    print("OK   : config.yaml updated")


def verify() -> None:
    print("\n=== Verification ===")
    sys.path.insert(0, str(HERMES_HOME / "hermes-agent"))
    import providers  # type: ignore

    providers._discovered = False
    providers._REGISTRY.clear()
    from providers import get_provider_profile  # type: ignore

    profile = get_provider_profile(PLUGIN_NAME)
    if profile is None:
        print("FAIL: plugin not discovered by Hermes", file=sys.stderr)
        sys.exit(1)
    print(f"OK   : plugin profile discovered: {profile.name}")
    print(f"OK   : aliases: {profile.aliases}")
    models = profile.fetch_models(timeout=5.0)
    if not models:
        print("FAIL: fetch_models returned empty (router unreachable?)", file=sys.stderr)
        sys.exit(1)
    print(f"OK   : fetch_models returned {len(models)} models from {profile.base_url}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--uninstall", action="store_true", help="Remove the integration")
    args = p.parse_args()

    print(f"REPO_ROOT       : {REPO_ROOT}")
    print(f"HERMES_HOME     : {HERMES_HOME}")
    print(f"SOURCE_PLUGIN   : {SOURCE_PLUGIN}")
    print(f"TARGET_PLUGIN   : {TARGET_PLUGIN}")
    print(f"CONFIG_PATH     : {CONFIG_PATH}")
    print()

    if args.uninstall:
        uninstall_plugin_symlink()
        unpatch_config_yaml()
        return 0

    install_plugin_symlink()
    patch_config_yaml()
    verify()
    print()
    print("🎉 QuotaMax Router installed in Hermes Agent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
