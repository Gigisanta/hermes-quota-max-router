"""The launchd wrapper must not evaluate shell input or read shared secrets."""

from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path

import pytest

from free_router import service
from free_router.service import load_private_env
from scripts import install_service


def _private_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def test_private_env_is_literal_and_does_not_execute(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    path = tmp_path / ".env"
    _private_file(path, f"GROQ_API_KEY=$(touch {marker})\nROUTER_PRODUCTION_WORKLOADS=\n")
    result = load_private_env(path)
    assert result["GROQ_API_KEY"].startswith("$(touch ")
    assert result["ROUTER_PRODUCTION_WORKLOADS"] == ""
    assert not marker.exists()


def test_private_env_rejects_permissions_and_symlink(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    _private_file(path, "ROUTER_PILOT_MODE=0\n")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        load_private_env(path)
    path.chmod(0o600)
    link = tmp_path / "linked.env"
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_private_env(link)


@pytest.mark.parametrize(
    "content",
    ["UNKNOWN_KEY=x\n", "ROUTER_PILOT_MODE=0\nROUTER_PILOT_MODE=1\n", "export GROQ_API_KEY=x\n"],
)
def test_private_env_rejects_unknown_duplicate_or_shell_lines(tmp_path: Path, content: str) -> None:
    path = tmp_path / ".env"
    _private_file(path, content)
    with pytest.raises(ValueError, match="assignment"):
        load_private_env(path)


def test_service_ignores_ambient_keys_from_other_projects(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / ".env"
    _private_file(path, "ROUTER_PILOT_MODE=0\n")
    monkeypatch.setenv("GROQ_API_KEY", "unrelated-project-key")
    monkeypatch.setattr(sys, "argv", ["service", "--env-file", str(path)])
    captured = {}

    def fake_run(*args, **kwargs):
        captured["key"] = os.getenv("GROQ_API_KEY")
        captured["pilot"] = os.getenv("ROUTER_PILOT_MODE")

    monkeypatch.setattr(service.uvicorn, "run", fake_run)
    service.main()
    assert captured == {"key": None, "pilot": "0"}


def test_install_backfills_state_paths_and_tokens_without_rotating_existing_key(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / "secrets" / ".env"
    env_file.parent.mkdir()
    _private_file(
        env_file,
        "GROQ_API_KEY=existing-provider-key\nROUTER_TOKEN_JOURNAL=existing-journal-token\n",
    )
    monkeypatch.setattr(install_service, "SECRET_ENV", env_file)
    state = tmp_path / "state"
    state.mkdir()

    install_service._create_private_env(state)
    values = load_private_env(env_file)
    assert values["GROQ_API_KEY"] == "existing-provider-key"
    assert values["ROUTER_TOKEN_JOURNAL"] == "existing-journal-token"
    assert values["ROUTER_TOKEN_SIMON_NEWS"]
    assert values["ROUTER_TOKEN_CACTUS_BRIEF"]
    assert values["ROUTER_QUEUE_DB"] == str(state / "queue.sqlite3")
    assert env_file.stat().st_mode & 0o777 == 0o600
    before = env_file.read_bytes()
    install_service._create_private_env(state)
    assert env_file.read_bytes() == before


def test_install_rejects_legacy_relative_state_path(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "secrets" / ".env"
    env_file.parent.mkdir()
    _private_file(env_file, "ROUTER_QUEUE_DB=var/queue.sqlite3\n")
    monkeypatch.setattr(install_service, "SECRET_ENV", env_file)
    with pytest.raises(RuntimeError, match="legacy_relative_path_requires_migration"):
        install_service._create_private_env(tmp_path / "state")
    assert load_private_env(env_file)["ROUTER_QUEUE_DB"] == "var/queue.sqlite3"


def test_first_install_rejects_preexisting_active_admission(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "secrets" / ".env"
    env_file.parent.mkdir()
    _private_file(env_file, "ROUTER_PRODUCTION_WORKLOADS=journal\nROUTER_PILOT_MODE=1\n")
    monkeypatch.setattr(install_service, "SECRET_ENV", env_file)
    before = env_file.read_bytes()
    with pytest.raises(RuntimeError, match="preexisting_active_admission_requires_review"):
        install_service._create_private_env(tmp_path / "state", initial_install=True)
    assert env_file.read_bytes() == before

    install_service._create_private_env(tmp_path / "state", initial_install=False)
    values = load_private_env(env_file)
    assert values["ROUTER_PRODUCTION_WORKLOADS"] == "journal"
    assert values["ROUTER_PILOT_MODE"] == "1"


def test_install_health_rejects_nonobject_json(monkeypatch) -> None:
    class Opener:
        def open(self, *_args, **_kwargs):
            return BytesIO(b"[]")

    monkeypatch.setattr(install_service, "build_opener", lambda *_args: Opener())
    assert install_service._healthy("expected-release") is False
