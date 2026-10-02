import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sorter.db import Database, MODEL_COLUMN_DEFINITIONS, SCHEMA_VERSION
from sorter.repository import ModelRepo


def create_legacy_database(path: Path, *, stamped_version: int = 1) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE cartridges (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE models (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          cartridge_id INTEGER NOT NULL REFERENCES cartridges(id),
          model_mode TEXT NOT NULL
        );
        INSERT INTO cartridges(id, name) VALUES (1, '9mm');
        INSERT INTO models(id, name, cartridge_id, model_mode)
          VALUES (1, 'Legacy production model', 1, 'convnext_tiny');
        """
    )
    conn.execute(f"PRAGMA user_version = {int(stamped_version)}")
    conn.commit()
    conn.close()


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "casesorter.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_legacy_model_table_is_completed_without_losing_rows(self) -> None:
        create_legacy_database(self.path)
        db = Database(self.path)
        db.ensure_initialized()

        columns = db._columns(db.conn, "models")
        self.assertTrue(set(MODEL_COLUMN_DEFINITIONS).issubset(columns))
        model = ModelRepo(db).get(1)
        self.assertIsNotNone(model)
        self.assertEqual("Legacy production model", model.name)
        self.assertEqual("Standard", model.model_type)
        self.assertEqual(1, model.model_version)
        self.assertEqual(SCHEMA_VERSION, db.conn.execute("PRAGMA user_version").fetchone()[0])
        db.close()

    def test_current_stamp_cannot_hide_an_incomplete_table(self) -> None:
        create_legacy_database(self.path, stamped_version=SCHEMA_VERSION)
        db = Database(self.path)
        db.ensure_initialized()
        self.assertTrue(
            set(MODEL_COLUMN_DEFINITIONS).issubset(db._columns(db.conn, "models"))
        )
        db.close()

    def test_failed_migration_rolls_back_columns_and_version_stamp(self) -> None:
        create_legacy_database(self.path)
        original = Database._apply_column_migrations

        def fail_after_one(db: Database, conn: sqlite3.Connection) -> None:
            conn.execute("ALTER TABLE models ADD COLUMN partial_test TEXT")
            raise RuntimeError("simulated migration failure")

        db = Database(self.path)
        with patch.object(Database, "_apply_column_migrations", fail_after_one):
            with self.assertRaises(RuntimeError):
                db.ensure_initialized()
        db.close()

        check = sqlite3.connect(self.path)
        columns = {row[1] for row in check.execute("PRAGMA table_info(models)")}
        version = check.execute("PRAGMA user_version").fetchone()[0]
        check.close()
        self.assertNotIn("partial_test", columns)
        self.assertEqual(1, version)
        self.assertIsNotNone(original)

    def test_future_version_stamp_is_not_downgraded(self) -> None:
        create_legacy_database(self.path, stamped_version=SCHEMA_VERSION + 10)
        db = Database(self.path)
        db.ensure_initialized()
        self.assertEqual(
            SCHEMA_VERSION + 10,
            db.conn.execute("PRAGMA user_version").fetchone()[0],
        )
        db.close()


if __name__ == "__main__":
    unittest.main()
