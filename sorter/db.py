"""SQLite database wrapper + one-shot migration from legacy config.json.

Exposes a single `Database` class that owns:
  - a `sqlite3.Connection` with row_factory = sqlite3.Row
  - schema DDL via `PRAGMA user_version`
  - `ensure_initialized()` that creates the DB if missing and runs the
    one-shot import from `data/config.json` when present.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

from . import paths


SCHEMA_VERSION = 6

# SQLite's CREATE TABLE IF NOT EXISTS does not update an existing table. Keep
# every post-v1 model column here so an upgrade repairs the actual table shape
# rather than trusting a version stamp that may have advanced too early.
MODEL_COLUMN_DEFINITIONS = {
    "model_type": "TEXT NOT NULL DEFAULT 'Standard'",
    "community_model_uid": "TEXT",
    "model_version": "INTEGER NOT NULL DEFAULT 1",
    "enable_image_processing": "INTEGER NOT NULL DEFAULT 1",
    "image_processing_json": "TEXT",
    "training_config_json": "TEXT",
    "ai_model_config_json": "TEXT",
    "use_primer_mask": "INTEGER NOT NULL DEFAULT 0",
    "hide_primer": "INTEGER NOT NULL DEFAULT 1",
    "primer_mask_size": "INTEGER NOT NULL DEFAULT 135",
    "last_training_date": "TEXT",
    "last_training_duration": "INTEGER NOT NULL DEFAULT 0",
    "trained_image_count": "INTEGER NOT NULL DEFAULT 0",
    "training_confusion_table": "TEXT",
    "feedback_loop_enabled": "INTEGER NOT NULL DEFAULT 0",
    "feedback_loop_confidence_floor": "INTEGER NOT NULL DEFAULT 95",
    "feedback_loop_upload_mode": "TEXT NOT NULL DEFAULT 'Manual'",
    "model_path": "TEXT",
    "checkpoint_env_json": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
}

SCHEMA_DDL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS cartridges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  cartridge_id INTEGER NOT NULL REFERENCES cartridges(id) ON DELETE RESTRICT,
  model_mode TEXT NOT NULL
    CHECK(model_mode IN ('convnext_tiny','convnext_small','convnext_base','convnext_large')),
  model_type TEXT NOT NULL DEFAULT 'Standard'
    CHECK(model_type IN ('Standard','ReadOnly','CommunityManaged')),
  community_model_uid TEXT,
  model_version INTEGER NOT NULL DEFAULT 1,
  enable_image_processing INTEGER NOT NULL DEFAULT 1,
  image_processing_json TEXT,
  training_config_json TEXT,
  ai_model_config_json TEXT,
  use_primer_mask INTEGER NOT NULL DEFAULT 0,
  hide_primer INTEGER NOT NULL DEFAULT 1,
  primer_mask_size INTEGER NOT NULL DEFAULT 135,
  last_training_date TEXT,
  last_training_duration INTEGER NOT NULL DEFAULT 0,
  trained_image_count INTEGER NOT NULL DEFAULT 0,
  training_confusion_table TEXT,
  feedback_loop_enabled INTEGER NOT NULL DEFAULT 0,
  feedback_loop_confidence_floor INTEGER NOT NULL DEFAULT 95,
  feedback_loop_upload_mode TEXT NOT NULL DEFAULT 'Manual',
  model_path TEXT,
  checkpoint_env_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_models_cartridge ON models(cartridge_id);

-- Stable network-facing names for models served by the integrated API.
-- The alias points to the local model row rather than a copied/renamed
-- checkpoint, so community updates that retain the model id also retain the
-- server assignment.  RESTRICT prevents a served model from disappearing
-- without an explicit technician action.
CREATE TABLE IF NOT EXISTS api_model_aliases (
  alias TEXT PRIMARY KEY COLLATE NOCASE,
  model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE RESTRICT,
  preload INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_api_model_alias_model
  ON api_model_aliases(model_id);

-- Parent classifications: named groups that child headstamps roll up into.
-- Scoped per-model.
CREATE TABLE IF NOT EXISTS headstamp_parents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL DEFAULT 0,
  UNIQUE(model_id, name)
);
CREATE INDEX IF NOT EXISTS idx_headstamp_parents_model ON headstamp_parents(model_id);

CREATE TABLE IF NOT EXISTS headstamps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL DEFAULT 0,
  parent_id INTEGER REFERENCES headstamp_parents(id) ON DELETE SET NULL,
  UNIQUE(model_id, name)
);
CREATE INDEX IF NOT EXISTS idx_headstamps_model ON headstamps(model_id);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""
# NOTE: community feedback-loop captures are intentionally NOT tracked in the
# DB. They live as JPEGs under data/models/<id>/feedback_images/ and that folder
# is the queue (see sorter/feedback.py). A legacy DB may still carry an unused
# feedback_queue table from an earlier schema; it is simply ignored.


DEFAULT_CARTRIDGE_NAME = "9mm"
DEFAULT_MODEL_NAME = "Default"
DEFAULT_MODEL_MODE = "convnext_tiny"


class _LockedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        with self.connection.access_lock:
            super().execute(sql, parameters)
            self._pending = iter(super().fetchall() if self.description else [])
        return self

    def executemany(self, sql, parameters):
        with self.connection.access_lock:
            super().executemany(sql, parameters)
            self._pending = iter([])
        return self

    def executescript(self, sql):
        with self.connection.access_lock:
            super().executescript(sql)
            self._pending = iter([])
        return self

    def fetchone(self):
        return next(self._pending, None)

    def fetchall(self):
        return list(self._pending)

    def fetchmany(self, size=None):
        import itertools
        return list(itertools.islice(self._pending, self.arraysize if size is None else size))

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._pending)


class _LockedConnection(sqlite3.Connection):
    def cursor(self, factory=_LockedCursor):
        with self.access_lock:
            return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql):
        return self.cursor().executescript(sql)

    def commit(self):
        with self.access_lock:
            return super().commit()

    def rollback(self):
        with self.access_lock:
            return super().rollback()

    def close(self):
        with self.access_lock:
            return super().close()


class Database:
    """Owns a single sqlite3.Connection for the lifetime of the app."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else paths.db_path()
        self._conn: sqlite3.Connection | None = None
        # Worker threads (RunController, training manager, community
        # downloader) read/write the DB while the UI thread does the same.
        # sqlite3 raises "objects created in a thread can only be used in
        # that same thread" by default; check_same_thread=False allows the
        # cross-thread access and this RLock serialises multi-statement
        # transactions so two threads can't interleave a BEGIN/COMMIT pair.
        # Every execution and its result materialization uses this same lock.
        # Standalone repository calls cannot enter another thread's transaction.
        self._lock = threading.RLock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized; call ensure_initialized() first")
        return self._conn

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self.path,
                isolation_level=None,
                check_same_thread=False,
                factory=_LockedConnection,
            )
            self._conn.access_lock = self._lock
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run statements in a single transaction. Auto-commit/rollback.

        Reentrant: when called within an existing transaction, uses a
        SAVEPOINT so nested `with db.transaction()` blocks compose naturally.
        The RLock serialises across threads so concurrent transactions
        from worker threads (test_once, training subprocess wire-up,
        community downloads) and the UI thread don't interleave.
        """
        with self._lock:
            conn = self.conn
            if conn.in_transaction:
                sp_name = f"sp_{id(conn) & 0xFFFF}_{conn.total_changes & 0xFFFF}"
                conn.execute(f"SAVEPOINT {sp_name}")
                try:
                    yield conn
                    conn.execute(f"RELEASE SAVEPOINT {sp_name}")
                except Exception:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                    conn.execute(f"RELEASE SAVEPOINT {sp_name}")
                    raise
            else:
                conn.execute("BEGIN")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

    def ensure_initialized(self, legacy_config_json: Path | None = None) -> None:
        """Create the DB if missing, run DDL, and migrate from legacy JSON.

        If the DB is brand new and `legacy_config_json` is provided and exists,
        its contents are imported and the file is renamed to ``.bak``. If no
        legacy config exists, the DB is still seeded with a default cartridge
        and model so the app boots into a usable state.
        """
        was_fresh = not self.path.exists()
        conn = self.connect()
        # DDL is idempotent (IF NOT EXISTS) so re-running on an existing DB is safe.
        for stmt in SCHEMA_DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        # Apply all structural repairs and advance the stamp atomically. The
        # table shape is authoritative; an incorrect current stamp cannot hide
        # an incomplete older schema.
        with self.transaction():
            self._apply_column_migrations(conn)
            current_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if current_version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        if was_fresh:
            if legacy_config_json and Path(legacy_config_json).exists():
                self._migrate_from_json(Path(legacy_config_json))
            else:
                self._seed_defaults()

    # ----- migration ----------------------------------------------------------

    def _apply_column_migrations(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after a table's first release.

        Idempotent: each ALTER runs only when the column is absent, so this
        is safe to call on every startup (fresh DBs already have the column
        from the DDL and skip the ALTER).
        """
        model_cols = self._columns(conn, "models")
        for name, definition in MODEL_COLUMN_DEFINITIONS.items():
            if name not in model_cols:
                conn.execute(f"ALTER TABLE models ADD COLUMN {name} {definition}")

        headstamp_cols = self._columns(conn, "headstamps")
        if "parent_id" not in headstamp_cols:
            # NULL default keeps this a legal ALTER even with a REFERENCES clause.
            conn.execute(
                "ALTER TABLE headstamps ADD COLUMN parent_id INTEGER "
                "REFERENCES headstamp_parents(id) ON DELETE SET NULL"
            )

        parent_cols = self._columns(conn, "headstamp_parents")
        if parent_cols and "slot" not in parent_cols:
            conn.execute(
                "ALTER TABLE headstamp_parents ADD COLUMN slot INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _migrate_from_json(self, json_path: Path) -> None:
        """One-shot import. Reads `config.json`, writes rows, renames to `.bak`."""
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._seed_defaults()
            return

        api = raw.get("api") or {}
        with self.transaction() as conn:
            for section in ("api", "serial", "image_proc", "camera"):
                value = raw.get(section)
                if value is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                        (section, json.dumps(value)),
                    )

            cart_id = conn.execute(
                "INSERT INTO cartridges(name) VALUES (?)",
                (DEFAULT_CARTRIDGE_NAME,),
            ).lastrowid

            model_name = api.get("model") or DEFAULT_MODEL_NAME
            model_id = conn.execute(
                "INSERT INTO models(name, cartridge_id, model_mode) VALUES (?, ?, ?)",
                (model_name, cart_id, DEFAULT_MODEL_MODE),
            ).lastrowid

            for entry in raw.get("headstamps") or []:
                hs_name = entry.get("name")
                if not hs_name:
                    continue
                hs_slot = int(entry.get("slot", 0))
                conn.execute(
                    "INSERT OR IGNORE INTO headstamps(name, model_id, slot) "
                    "VALUES (?, ?, ?)",
                    (hs_name, model_id, hs_slot),
                )

            # Note: we intentionally do NOT seed settings['default_model_id'].
            # An absent key signals "AI Config mode" — the seeded model is
            # available but not active until the user activates it from the
            # Models tab.

        backup = json_path.with_suffix(json_path.suffix + ".bak")
        with contextlib.suppress(OSError):
            shutil.move(str(json_path), backup)

    def _seed_defaults(self) -> None:
        """Insert a starter cartridge + model so a fresh install is usable.

        The seeded model is NOT auto-activated; default runtime is AI Config
        mode (no `default_model_id` setting). The Models tab's synthetic
        "Use AI Config" row is the active selection out of the box.
        """
        with self.transaction() as conn:
            cart_id = conn.execute(
                "INSERT INTO cartridges(name) VALUES (?)",
                (DEFAULT_CARTRIDGE_NAME,),
            ).lastrowid
            conn.execute(
                "INSERT INTO models(name, cartridge_id, model_mode) VALUES (?, ?, ?)",
                (DEFAULT_MODEL_NAME, cart_id, DEFAULT_MODEL_MODE),
            )

    # ----- raw dump helper (debug) -------------------------------------------

    def dump_table(self, table: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(r) for r in rows]
