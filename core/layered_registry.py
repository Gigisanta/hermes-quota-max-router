"""Layered registry — Phase 17.

The auto-discovery produces hundreds of new models. We don't want
those polluting the curated seed (the spec's source-of-truth for
ranking). Instead:

  - `registry/models.json` stays the curated source (7 models, tier 1-99)
  - `registry/discovered.json` is the auto-discovered layer (545 models,
    tier 10-99, only added by the auto-updater)
  - `LayeredRegistry` merges both at runtime: curated always wins on
    conflict, discovered fills in the gaps.

This way:
  - Tests can use just the curated seed (predictable, deterministic)
  - Production merges both (broad coverage, free tier coverage)
  - A `remove_missing` from the discovered layer is safe (doesn't touch
    curated)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_registry import Model, ModelRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent
CURATED_PATH = REPO_ROOT / "registry" / "models.json"
DISCOVERED_PATH = REPO_ROOT / "registry" / "discovered.json"


def _db_dir() -> Path:
    """Resolve where registries should write their SQLite dbs.

    Order of precedence:
      1. ``QUOTA_DB_DIR`` env var (test isolation)
      2. ``registry/data/`` under the repo root (production default)
    """
    override = os.environ.get("QUOTA_DB_DIR", "").strip()
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return REPO_ROOT / "registry" / "data"


@dataclass
class LayeredRegistry:
    """Merges curated + discovered models. Curated takes priority on conflict."""

    curated: ModelRegistry
    discovered: ModelRegistry

    @classmethod
    def from_defaults(
        cls,
        curated_path: Path | None = None,
        discovered_path: Path | None = None,
    ) -> LayeredRegistry:
        cp = curated_path or CURATED_PATH
        dp = discovered_path or DISCOVERED_PATH
        db_dir = _db_dir()
        curated = ModelRegistry(
            db_path=db_dir / "registry_curated.sqlite",
            seed_path=cp,
        )
        discovered = ModelRegistry(
            db_path=db_dir / "registry_discovered.sqlite",
            seed_path=dp if dp.exists() else None,
        )
        return cls(curated=curated, discovered=discovered)

    def all(self) -> list[Model]:
        """Curated first (tier 1-99), then discovered (tier 10+)."""
        seen: set[str] = set()
        out: list[Model] = []
        for m in self.curated.all():
            out.append(m)
            seen.add(m.model_id)
        for m in self.discovered.all():
            if m.model_id in seen:
                continue  # curated wins
            out.append(m)
        return out

    def free_first(self) -> list[Model]:
        return [m for m in self.all() if m.is_free]

    def count(self) -> int:
        return len(self.all())

    def get(self, model_id: str) -> Model | None:
        m = self.curated.get(model_id)
        if m is not None:
            return m
        return self.discovered.get(model_id)

    def by_tier(self, free_only: bool = True) -> list[Model]:
        models = self.free_first() if free_only else self.all()
        return sorted(models, key=lambda m: m.tier_rank)

    def summary(self) -> dict[str, Any]:
        return {
            "curated_count": self.curated.count(),
            "discovered_count": self.discovered.count(),
            "merged_count": self.count(),
            "free_count": len(self.free_first()),
        }
