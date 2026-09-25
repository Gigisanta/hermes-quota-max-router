"""Install one pinned, loopback-only router release as a user launchd service.

This prepares the control plane. Workloads stay disabled until the editorial
and provider gates are satisfied and their tokens are configured in clients.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import plistlib
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from free_router.service import load_private_env

LABEL = "com.hermaat.free-router"
ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = Path.home() / ".hermes/services/free-router"
SECRET_ENV = Path.home() / ".hermes/project-env/HerMaatOS/work/hermes-quota-max-router/.env"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)


def _atomic_private_write(path: Path, content: bytes) -> None:
    temporary = path.parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _create_private_env(state: Path, *, initial_install: bool = True) -> None:
    SECRET_ENV.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(SECRET_ENV.parent, 0o700)
    defaults = {
        "ROUTER_QUEUE_DB": state / "queue.sqlite3",
        "ROUTER_VERIFIED_MODELS": state / "verified-models.json",
        "ROUTER_DAILY_PEAK_FILE": state / "daily-peak.json",
        "ROUTER_PRODUCTION_WORKLOADS": "",
        "ROUTER_PILOT_MODE": "0",
        "ROUTER_TOKEN_JOURNAL": secrets.token_urlsafe(48),
        "ROUTER_TOKEN_SIMON_NEWS": secrets.token_urlsafe(48),
        "ROUTER_TOKEN_CACTUS_BRIEF": secrets.token_urlsafe(48),
    }
    env_existed = SECRET_ENV.exists()
    existing = load_private_env(SECRET_ENV) if env_existed else {}
    if initial_install and (
        existing.get("ROUTER_PRODUCTION_WORKLOADS", "").strip()
        or existing.get("ROUTER_PILOT_MODE") == "1"
    ):
        raise RuntimeError("preexisting_active_admission_requires_review")
    for key in ("ROUTER_QUEUE_DB", "ROUTER_VERIFIED_MODELS", "ROUTER_DAILY_PEAK_FILE"):
        value = existing.get(key)
        if value and not Path(value).is_absolute():
            raise RuntimeError(f"legacy_relative_path_requires_migration:{key}")
    values = {**defaults, **existing}
    for key in ("ROUTER_QUEUE_DB", "ROUTER_VERIFIED_MODELS", "ROUTER_DAILY_PEAK_FILE"):
        if not values[key]:
            values[key] = defaults[key]
    for key in ("ROUTER_TOKEN_JOURNAL", "ROUTER_TOKEN_SIMON_NEWS", "ROUTER_TOKEN_CACTUS_BRIEF"):
        if not values[key]:
            values[key] = defaults[key]
    if existing == values:
        return  # Preserve a valid file and every existing credential byte-for-byte.
    content = ("\n".join(f"{key}={value}" for key, value in values.items()) + "\n").encode()
    if env_existed:
        _atomic_private_write(SECRET_ENV, content)
    else:
        temporary = SECRET_ENV.parent / f".env.tmp.{os.getpid()}.{secrets.token_hex(8)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, SECRET_ENV)
            except FileExistsError:
                # An external writer won the race: verify it on the next run.
                raise RuntimeError("service_env_created_concurrently") from None
        finally:
            temporary.unlink(missing_ok=True)


def _install_release(releases: Path, commit: str) -> Path:
    target = releases / commit
    if target.exists():
        marker = target / ".release-ready"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == commit:
            return target
        raise RuntimeError("incomplete_release_requires_inspection")
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit], cwd=ROOT, check=True, capture_output=True
    ).stdout
    target.mkdir(mode=0o700)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            tar.extractall(target, filter="data")
        _run(sys.executable, "-m", "venv", str(target / ".venv"))
        _run(
            str(target / ".venv/bin/python"),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            ".",
            cwd=target,
        )
        _run(str(target / ".venv/bin/python"), "-c", "import free_router.service", cwd=target)
        (target / ".release-ready").write_text(commit + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(target)
        raise
    return target


def _plist(release: Path, logs: Path) -> bytes:
    data = {
        "Label": LABEL,
        "ProgramArguments": [
            str(release / ".venv/bin/python"),
            "-m",
            "free_router.service",
            "--env-file",
            str(SECRET_ENV),
        ],
        "WorkingDirectory": str(SERVICE_ROOT / "state"),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "EnvironmentVariables": {"ROUTER_RELEASE_SHA": release.name},
        "StandardOutPath": str(logs / "stdout.log"),
        "StandardErrorPath": str(logs / "stderr.log"),
    }
    return plistlib.dumps(data, sort_keys=True)


def _healthy(commit: str) -> bool:
    try:
        with build_opener(ProxyHandler({})).open(
            "http://127.0.0.1:8123/health", timeout=2
        ) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            return False
        return (
            payload.get("status") == "ok"
            and payload.get("redis") is True
            and payload.get("release") == commit
            and payload.get("service_configured") is True
        )
    except (OSError, ValueError):
        return False


def _port_in_use() -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", 8123)) == 0


def _wait_stopped(domain: str) -> None:
    for _ in range(100):
        job_loaded = (
            subprocess.run(
                ["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True
            ).returncode
            == 0
        )
        if not job_loaded and not _port_in_use():
            return
        time.sleep(0.1)
    raise RuntimeError("previous_service_did_not_stop")


def install() -> None:
    if sys.version_info < (3, 11, 4):  # noqa: UP036 - tar data filter needs Python 3.11.4+
        raise RuntimeError("python_3_11_4_required")
    if os.getuid() == 0:
        raise RuntimeError("install_as_regular_user")
    commit = _run("git", "rev-parse", "HEAD", cwd=ROOT).stdout.strip()
    if _run("git", "status", "--porcelain", cwd=ROOT).stdout:
        raise RuntimeError("commit_changes_before_install")
    state = SERVICE_ROOT / "state"
    logs = SERVICE_ROOT / "logs"
    releases = SERVICE_ROOT / "releases"
    for path in (SERVICE_ROOT, state, logs, releases):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    previous = PLIST.read_bytes() if PLIST.exists() else None
    domain = f"gui/{os.getuid()}"
    loaded = (
        subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True).returncode
        == 0
    )
    if loaded and previous is None:
        raise RuntimeError("loaded_service_missing_installed_plist")
    _create_private_env(state, initial_install=not loaded)
    release = _install_release(releases, commit)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    port_in_use = _port_in_use()
    if port_in_use and not loaded:
        raise RuntimeError("port_8123_in_use_by_another_service")
    try:
        _atomic_private_write(PLIST, _plist(release, logs))
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        _wait_stopped(domain)
        _run("launchctl", "bootstrap", domain, str(PLIST))
        for _ in range(60):
            if _healthy(commit):
                break
            time.sleep(1)
        else:
            raise RuntimeError("service_failed_health_gate")
    except Exception:
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        if previous is None:
            PLIST.unlink(missing_ok=True)
        else:
            _atomic_private_write(PLIST, previous)
        # Restore the on-disk service definition even when the new process
        # cannot release its port yet. Re-bootstrap only after it has stopped.
        _wait_stopped(domain)
        if previous is not None and loaded:
            _run("launchctl", "bootstrap", domain, str(PLIST))
        raise
    print(f"installed {LABEL} release={commit[:12]} on 127.0.0.1:8123")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("install", choices=["install"])
    parser.parse_args()
    install()
