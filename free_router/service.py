"""Launchd entrypoint: load a private, literal environment file before app import."""

from __future__ import annotations

import argparse
import os
import re
import stat
from pathlib import Path

import uvicorn

_KEYS = {
    "REDIS_URL",
    "ROUTER_QUEUE_DB",
    "ROUTER_VERIFIED_MODELS",
    "ROUTER_DAILY_PEAK_FILE",
    "ROUTER_PRODUCTION_WORKLOADS",
    "ROUTER_PILOT_MODE",
    "ROUTER_TOKEN_JOURNAL",
    "ROUTER_TOKEN_SIMON_NEWS",
    "ROUTER_TOKEN_CACTUS_BRIEF",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "NOVITA_API_KEY",
    "SILICONFLOW_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
}
_ASSIGNMENT = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")


def load_private_env(path: Path) -> dict[str, str]:
    """Parse KEY=value without shell evaluation; refuse shared or linked files."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unsafe_service_env_file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("unsafe_service_env_permissions")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            lines = stream.readlines()
    finally:
        if fd >= 0:
            os.close(fd)
    env: dict[str, str] = {}
    for line in lines:
        line = line.rstrip("\r\n")
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT.fullmatch(line)
        if not match or match.group(1) not in _KEYS or match.group(1) in env:
            raise ValueError("invalid_service_env_assignment")
        if "\x00" in match.group(2):
            raise ValueError("invalid_service_env_value")
        env[match.group(1)] = match.group(2)
    return env


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m free_router.service")
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    # A later release can add keys without changing the launchd plist.
    private_env = load_private_env(args.env_file)
    for key in _KEYS:
        os.environ.pop(key, None)
    os.environ.update(private_env)
    uvicorn.run("free_router.app:app", host="127.0.0.1", port=8123, workers=1, access_log=False)


if __name__ == "__main__":
    main()
