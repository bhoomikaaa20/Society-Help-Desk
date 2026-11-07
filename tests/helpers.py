"""Shared test setup. Every test gets its own temporary database file."""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import database  # noqa: E402

# Comfortably after the seed data, so results do not depend on the real clock.
FIXED_NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


class DeskTestCase(unittest.TestCase):
    """Provides self.conn on an isolated database seeded from the CSV files."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.db_path = Path(self._tempdir.name) / "test.db"
        self.assertNotEqual(self.db_path.resolve(), database.DEFAULT_DB_PATH.resolve())
        self.conn = database.open_database(self.db_path)
        self.addCleanup(self.conn.close)

    def run_cli(self, *args):
        """Run the real CLI in a subprocess against this test's database."""
        env = {**os.environ, "SOCIETY_DESK_DB": str(self.db_path), "PYTHONIOENCODING": "utf-8"}
        return subprocess.run(
            [sys.executable, str(TOOLS_DIR / "desk.py"), *args],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=PROJECT_ROOT,
        )

    def count(self, table):
        # Fixed table names from the tests only.
        return self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def budget(self, category):
        return self.conn.execute("SELECT * FROM budgets WHERE category = ?", (category,)).fetchone()
