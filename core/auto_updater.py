"""Auto-Updater — Phase 4.

Keeps the Model Registry in sync with reality. Designed to be
testable WITHOUT web access: feeds are normal JSON files with the
same schema as `registry/models.json`. Production deployments can
plug in a `RemoteFeedProvider` that fetches from provider pricing
pages or curated APIs, but the core merge/versioning logic is pure
and deterministic.

Components:
  - `FeedProvider` protocol: returns a list of model dicts.
  - `LocalFeedProvider`: reads from a local JSON file (testing + dev).
  - `RegistryUpdater`: merges a feed into the SQLite registry safely,
    produces a changelog, bumps version, writes back to `models.json`.

Usage:
  updater = RegistryUpdater(registry, seed_path)
  result = updater.apply_feed(feed_models)
  print(result.changes)  # ['updated: deepseek/...', 'added: openrouter/...']
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .model_registry import Model, ModelRegistry

log = logging.getLogger(__name__)


class FeedProvider(Protocol):
    def fetch(self) -> list[dict]: ...


class LocalFeedProvider:
    """Reads a feed from a local JSON file. Schema matches models.json."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def fetch(self) -> list[dict]:
        with open(self.path) as f:
            data = json.load(f)
        models = data.get("models", data) if isinstance(data, dict) else data
        if not isinstance(models, list):
            raise ValueError(f"Feed at {self.path} is not a list of model dicts")
        return models


class StaticFeedProvider:
    """Returns a fixed in-memory list. Useful for tests."""

    def __init__(self, models: list[dict]) -> None:
        self._models = models

    def fetch(self) -> list[dict]:
        return list(self._models)


@dataclass
class UpdateResult:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    old_version: str = ""
    new_version: str = ""
    timestamp: str = ""

    @property
    def changes(self) -> list[str]:
        out: list[str] = []
        out.extend(f"added: {m}" for m in self.added)
        out.extend(f"updated: {m}" for m in self.updated)
        out.extend(f"removed: {m}" for m in self.removed)
        out.extend(f"error: {e}" for e in self.errors)
        return out

    @property
    def total(self) -> int:
        return len(self.added) + len(self.updated) + len(self.removed)


def _bump_version(old: str) -> str:
    """Bump a YYYY-MM-DD-style version. Always forward-only.

    The seed uses calendar versions; if the incoming feed's version
    is older, we still bump to "now" so the changelog is monotonic.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if not old:
        return today
    # If old is already today's date, append a sub-revision counter
    if old.startswith(today):
        suffix = old.split("-rev")[-1] if "-rev" in old else ""
        try:
            n = int(suffix) + 1
        except ValueError:
            n = 1
        return f"{today}-rev{n}"
    return today


class RegistryUpdater:
    """Merges feed data into the registry and rewrites the seed file.

    Update semantics:
      - If a `model_id` in the feed is NOT in the registry → ADD.
      - If it IS in the registry → UPDATE (only fields that differ).
      - If a registry `model_id` is NOT in the feed AND
        `remove_missing=True` → REMOVE. (default: False, to avoid
        accidentally wiping a model because a feed is incomplete.)
    """

    def __init__(
        self,
        registry: ModelRegistry,
        seed_path: Path,
        remove_missing: bool = False,
    ) -> None:
        self.registry = registry
        self.seed_path = Path(seed_path)
        self.remove_missing = remove_missing

    def apply_feed(self, feed: Iterable[dict]) -> UpdateResult:
        result = UpdateResult(timestamp=datetime.now(UTC).isoformat())
        existing = {m.model_id: m for m in self.registry.all()}
        feed_ids: set[str] = set()

        for raw in feed:
            try:
                incoming = Model.from_json(raw)
            except Exception as e:
                # Common causes: missing field, wrong type
                mid = raw.get("model_id", "<unknown>")
                result.errors.append(f"{mid}: {e}")
                continue

            feed_ids.add(incoming.model_id)
            current = existing.get(incoming.model_id)
            if current is None:
                self.registry.upsert(incoming)
                result.added.append(incoming.model_id)
            elif _models_differ(current, incoming):
                self.registry.upsert(incoming)
                result.updated.append(incoming.model_id)
            else:
                result.unchanged.append(incoming.model_id)

        if self.remove_missing:
            for mid in existing.keys() - feed_ids:
                self.registry.delete(mid)
                result.removed.append(mid)

        # Rewrite the seed file with the merged registry + new version
        result.old_version = self._read_seed_version()
        result.new_version = _bump_version(result.old_version)
        self._write_seed(result.new_version)
        return result

    # --- internals ---

    def _read_seed_version(self) -> str:
        if not self.seed_path.exists():
            return ""
        with open(self.seed_path) as f:
            return json.load(f).get("version", "")

    def _write_seed(self, new_version: str) -> None:
        payload = {
            "version": new_version,
            "source": "auto-updater",
            "note": "Regenerated by RegistryUpdater; manual edits may be overwritten.",
            "models": [m.to_dict() for m in self.registry.all()],
        }
        self.seed_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.seed_path, "w") as f:
            json.dump(payload, f, indent=2)
        log.info("Seed rewritten at version %s (%d models)", new_version, len(payload["models"]))


def _models_differ(a: Model, b: Model) -> bool:
    """Field-level diff. We ignore quota counters (current_remaining_tokens)
    because those are live and managed by the QuotaManager, not the seed."""
    live_fields = {
        "model_id",
        "provider",
        "display_name",
        "context_window",
        "input_price",
        "output_price",
        "is_free",
        "tier_rank",
        "strength_tags",
        "weakness_tags",
        "best_for",
        "performance_score",
        "notes",
        "daily_quota_tokens",
        "reset_schedule",
        "last_benchmark_date",
    }
    for fname in live_fields:
        if getattr(a, fname) != getattr(b, fname):
            return True
    return False


if __name__ == "__main__":
    from core.model_registry import ModelRegistry

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    reg = ModelRegistry()
    updater = RegistryUpdater(reg, Path("registry/models.json"))

    feed = LocalFeedProvider("registry/feed_sample.json").fetch()
    result = updater.apply_feed(feed)
    print(f"\nVersion: {result.old_version} → {result.new_version}")
    print(f"Added:    {result.added}")
    print(f"Updated:  {result.updated}")
    print(f"Removed:  {result.removed}")
    print(f"Unchanged:{len(result.unchanged)} models")
    if result.errors:
        print(f"Errors:   {result.errors}")
