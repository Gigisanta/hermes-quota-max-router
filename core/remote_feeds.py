"""Remote feed provider — Phase 16.

Pulls live data from provider catalog endpoints and turns them into
the same shape as `registry/models.json`. The RegistryUpdater consumes
the result without caring whether it came from a local file, a remote
HTTP endpoint, or a curated static catalog.

This module is the actual "discovery" the spec asks for. With the
`Catalogs` module, we get:

  - OpenRouter public models (100+ entries, real pricing)
  - HuggingFace warm-inference models (up to 200 entries, all free)
  - Curated static fallback (registry/models.json itself)

Network failures degrade gracefully: if OpenRouter is down, we still
get HF + curated. If both network sources fail, we still have curated.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .catalogs import CATALOGS, CatalogEntry

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 15.0
USER_AGENT = "hermes-quota-max-router/0.1 (auto-discovery)"


class RemoteFeedProvider:
    """Fetches models from one or more catalog endpoints.

    Usage:
        provider = RemoteFeedProvider(timeout_s=10)
        models = provider.fetch_all()
        # → list of dicts, each a model in registry/models.json schema
    """

    def __init__(
        self,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        catalogs: list[CatalogEntry] | None = None,
        curated_path: Path | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.catalogs = catalogs or CATALOGS
        self.curated_path = curated_path

    def fetch_all(self) -> list[dict]:
        """Fetch from every catalog; aggregate results; return flat list."""
        seen: set[str] = set()
        out: list[dict] = []
        errors: list[str] = []
        for cat in self.catalogs:
            try:
                entries = self._fetch_one(cat)
                added = 0
                for e in entries:
                    mid = e.get("model_id")
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    out.append(e)
                    added += 1
                log.info("catalog %s: %d models", cat.name, added)
            except Exception as e:  # noqa: BLE001
                msg = f"{cat.name}: {type(e).__name__}: {e}"
                errors.append(msg)
                log.warning("catalog %s failed: %s", cat.name, e)
        if not out and errors:
            raise RuntimeError(
                f"All catalogs failed: {'; '.join(errors)}"
            )
        return out

    def _fetch_one(self, cat: CatalogEntry) -> list[dict]:
        """Fetch + parse a single catalog."""
        if cat.name == "curated_static":
            return self._load_curated(cat)
        return self._fetch_http(cat)

    def _load_curated(self, cat: CatalogEntry) -> list[dict]:
        path = self.curated_path
        if path is None:
            # Default to the in-repo seed
            from .catalogs import CATALOGS as _c  # noqa: F401
            # Walk up to find registry/models.json
            here = Path(__file__).resolve().parent.parent
            path = here / "registry" / "models.json"
        if not path.exists():
            log.warning("curated catalog missing: %s", path)
            return []
        with open(path) as f:
            data = json.load(f)
        return cat.parser(data)

    def _fetch_http(self, cat: CatalogEntry) -> list[dict]:
        """GET cat.endpoint, parse JSON, run through cat.parser."""
        with httpx.Client(
            timeout=self.timeout_s,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            r = client.get(cat.endpoint)
            r.raise_for_status()
            data = r.json()
        return cat.parser(data)
