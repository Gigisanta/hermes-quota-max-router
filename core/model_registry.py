"""Model Registry — Phase 1.

Loads, queries, and persists model metadata. SQLite is the source of truth
for the MVP; the JSON seed in `registry/models.json` is loaded on first init
and treated as a snapshot.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "registry" / "models.json"
DB_PATH = REPO_ROOT / "data" / "registry.sqlite"


def _default_db_path() -> Path:
    """Resolve the default SQLite path, honoring ``QUOTA_DB_DIR`` for tests.

    ``QUOTA_DB_DIR`` lets the test suite point registries at a tmp_path
    unique to the session, so running pytest never mutates the shipped
    ``data/registry.sqlite`` file.
    """
    override = os.environ.get("QUOTA_DB_DIR", "").strip()
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p / "registry.sqlite"
    return DB_PATH


@dataclass
class Model:
    model_id: str
    provider: str
    display_name: str
    context_window: int
    input_price: float
    output_price: float
    is_free: bool
    tier_rank: int
    strength_tags: list[str]
    weakness_tags: list[str]
    best_for: list[str]
    performance_score: float
    notes: str = ""
    daily_quota_tokens: int | None = None
    current_remaining_tokens: int | None = None
    last_reset: str | None = None
    reset_schedule: str | None = None
    last_benchmark_date: str | None = None

    @classmethod
    def from_json(cls, d: dict) -> Model:
        return cls(
            model_id=d["model_id"],
            provider=d["provider"],
            display_name=d["display_name"],
            context_window=int(d["context_window"]),
            input_price=float(d["input_price"]),
            output_price=float(d["output_price"]),
            is_free=bool(d["is_free"]),
            tier_rank=int(d["tier_rank"]),
            strength_tags=list(d.get("strength_tags", [])),
            weakness_tags=list(d.get("weakness_tags", [])),
            best_for=list(d.get("best_for", [])),
            performance_score=float(d.get("performance_score", 0.0)),
            notes=d.get("notes", ""),
            daily_quota_tokens=d.get("daily_quota_tokens"),
            current_remaining_tokens=d.get("current_remaining_tokens"),
            last_reset=d.get("last_reset"),
            reset_schedule=d.get("reset_schedule"),
            last_benchmark_date=d.get("last_benchmark_date"),
        )

    def to_dict(self) -> dict:
        return asdict(self)


class ModelRegistry:
    """SQLite-backed model registry with JSON seed bootstrap."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS models (
        model_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        display_name TEXT NOT NULL,
        context_window INTEGER NOT NULL,
        input_price REAL NOT NULL,
        output_price REAL NOT NULL,
        is_free INTEGER NOT NULL,
        tier_rank INTEGER NOT NULL,
        strength_tags TEXT NOT NULL,
        weakness_tags TEXT NOT NULL,
        best_for TEXT NOT NULL,
        performance_score REAL NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        daily_quota_tokens INTEGER,
        current_remaining_tokens INTEGER,
        last_reset TEXT,
        reset_schedule TEXT,
        last_benchmark_date TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_models_free ON models(is_free, tier_rank);
    """

    def __init__(self, db_path: Path | str | None = None, seed_path: Path | None = SEED_PATH) -> None:
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        if self.count() == 0 and seed_path is not None and seed_path.exists():
            self._load_seed(seed_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()

    def _load_seed(self, path: Path) -> int:
        with open(path) as f:
            data = json.load(f)
        models = [Model.from_json(m) for m in data.get("models", [])]
        self.upsert_many(models)
        return len(models)

    # --- CRUD ---
    def upsert(self, model: Model) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO models VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(model_id) DO UPDATE SET
                    provider=excluded.provider,
                    display_name=excluded.display_name,
                    context_window=excluded.context_window,
                    input_price=excluded.input_price,
                    output_price=excluded.output_price,
                    is_free=excluded.is_free,
                    tier_rank=excluded.tier_rank,
                    strength_tags=excluded.strength_tags,
                    weakness_tags=excluded.weakness_tags,
                    best_for=excluded.best_for,
                    performance_score=excluded.performance_score,
                    notes=excluded.notes,
                    daily_quota_tokens=excluded.daily_quota_tokens,
                    current_remaining_tokens=excluded.current_remaining_tokens,
                    last_reset=excluded.last_reset,
                    reset_schedule=excluded.reset_schedule,
                    last_benchmark_date=excluded.last_benchmark_date
                """,
                (
                    model.model_id,
                    model.provider,
                    model.display_name,
                    model.context_window,
                    model.input_price,
                    model.output_price,
                    1 if model.is_free else 0,
                    model.tier_rank,
                    json.dumps(model.strength_tags),
                    json.dumps(model.weakness_tags),
                    json.dumps(model.best_for),
                    model.performance_score,
                    model.notes,
                    model.daily_quota_tokens,
                    model.current_remaining_tokens,
                    model.last_reset,
                    model.reset_schedule,
                    model.last_benchmark_date,
                ),
            )
            conn.commit()

    def upsert_many(self, models: Iterable[Model]) -> None:
        for m in models:
            self.upsert(m)

    def get(self, model_id: str) -> Model | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM models WHERE model_id = ?", (model_id,)).fetchone()
        if not row:
            return None
        return self._row_to_model(row)

    def delete(self, model_id: str) -> bool:
        """Remove a model. Returns True if a row was actually deleted."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM models WHERE model_id = ?", (model_id,))
            conn.commit()
            return cur.rowcount > 0

    def count_by_field(self, field: str, value) -> int:
        """Count rows where `field = value`. Field must be in the safe allowlist."""
        safe = {"is_free", "tier_rank", "provider"}
        if field not in safe:
            raise ValueError(f"count_by_field: field {field!r} not allowed")
        with self._connect() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM models WHERE {field} = ?", (value,)).fetchone()[0])

    def all(self) -> list[Model]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM models ORDER BY is_free DESC, tier_rank ASC").fetchall()
        return [self._row_to_model(r) for r in rows]

    def free_first(self) -> list[Model]:
        return [m for m in self.all() if m.is_free]

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM models").fetchone()[0])

    @staticmethod
    def _row_to_model(row: tuple) -> Model:
        return Model(
            model_id=row[0],
            provider=row[1],
            display_name=row[2],
            context_window=row[3],
            input_price=row[4],
            output_price=row[5],
            is_free=bool(row[6]),
            tier_rank=row[7],
            strength_tags=json.loads(row[8]),
            weakness_tags=json.loads(row[9]),
            best_for=json.loads(row[10]),
            performance_score=row[11],
            notes=row[12],
            daily_quota_tokens=row[13],
            current_remaining_tokens=row[14],
            last_reset=row[15],
            reset_schedule=row[16],
            last_benchmark_date=row[17],
        )


if __name__ == "__main__":
    reg = ModelRegistry()
    print(f"Loaded {reg.count()} models into {reg.db_path}")
    for m in reg.free_first()[:3]:
        print(f"  [FREE ] {m.model_id} (rank {m.tier_rank})")
    for m in [m for m in reg.all() if not m.is_free]:
        print(f"  [PAID ] {m.model_id}")
