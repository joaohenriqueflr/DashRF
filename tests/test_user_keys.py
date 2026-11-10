"""Verify access-key migration and callbacks against an isolated database."""

from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


HAS_DASH = all(importlib.util.find_spec(name) for name in (
    "dash", "dash_ag_grid", "dash_bootstrap_components",
))


@unittest.skipUnless(HAS_DASH, "Run in the application's Dash environment.")
class UserKeysTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "data").mkdir()
        self.db = root / "data" / "fixed_income.db"
        self.records = [
            (3, "test-master-key", "Master", "master@example.test", "master", 1, "2026-01-01"),
            (8, "test-trader-key", "Trader", "trader@example.test", "trader", 1, "2026-02-01"),
            (12, "test-inactive-key", "Inactive", "inactive@example.test", "trader", 0, "2026-03-01"),
        ]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE RFQ_USER_KEYS (
                    KEY_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    SECRET_KEY TEXT UNIQUE, USR_NAME TEXT, EMAIL TEXT,
                    ROLE TEXT, FL_ACTIVE INTEGER, DT_CREATED TEXT
                );
                CREATE UNIQUE INDEX idx_rfq_user_keys_secret ON RFQ_USER_KEYS (SECRET_KEY);
            """)
            conn.executemany("INSERT INTO RFQ_USER_KEYS VALUES (?, ?, ?, ?, ?, ?, ?)", self.records)
            # The deleted key must not be reused after the rename.
            conn.execute("INSERT INTO RFQ_USER_KEYS (KEY_ID) VALUES (30)")
            conn.execute("DELETE FROM RFQ_USER_KEYS WHERE KEY_ID = 30")
        self.page = types.ModuleType("user_keys_test_page")
        self.page.__file__ = str(root / "pages" / "user_keys.py")
        sys.modules[self.page.__name__] = self.page
        self.addCleanup(sys.modules.pop, self.page.__name__, None)
        source = (Path(__file__).resolve().parents[1] / "pages" / "user_keys.py").read_text()
        with patch("dash.register_page"):
            exec(compile(source, self.page.__file__, "exec"), self.page.__dict__)

    def test_rename_preserves_credentials_and_is_repeatable(self):
        self.page.ensure_user_keys_table()
        self.page.ensure_user_keys_table()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM USER_KEYS ORDER BY KEY_ID").fetchall(), self.records)
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('RFQ_USER_KEYS', 'idx_rfq_user_keys_secret')"
            ).fetchone())
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(USER_KEYS)")}
            self.assertIn("idx_user_keys_secret", indexes)

    def test_existing_keys_still_activate_and_inactive_keys_are_rejected(self):
        from dash import no_update

        user, _, cleared = self.page.activate_key(1, " test-trader-key ")
        self.assertEqual(user, {"key_id": 8, "user": "Trader", "email": "trader@example.test", "role": "trader"})
        self.assertEqual(cleared, "")
        for key in ("test-inactive-key", "invalid-key"):
            with self.subTest(key=key):
                self.assertIs(self.page.activate_key(1, key)[0], no_update)

    def test_master_can_list_and_create_keys_after_rename(self):
        master = {"user": "Master", "role": "master"}
        self.assertEqual(len(self.page.load_keys_grid(0, master)), 3)
        refresh, message = self.page.create_key(1, "New Trader", "trader", master, 0)
        self.assertEqual(refresh, 1)
        self.assertIn("Key created for New Trader", message)
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute("SELECT KEY_ID, USR_NAME FROM USER_KEYS WHERE USR_NAME = 'New Trader'").fetchone()
        self.assertEqual(row, (31, "New Trader"))
