"""Exercise GWM restrictions with isolated identities and signed login cookies."""

import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from services import access


class ProfileAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "access.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE USER_KEYS (KEY_ID INTEGER PRIMARY KEY, USR_NAME TEXT, EMAIL TEXT, ROLE TEXT, FL_ACTIVE INTEGER)")
            conn.executemany("INSERT INTO USER_KEYS VALUES (?,?,?,?,?)", [
                (1, "GWM User", "gwm@example.test", "gwm", 1),
                (2, "Trader", "trader@example.test", "trader", 1),
                (3, "Inactive", "inactive@example.test", "gwm", 0),
            ])
        self.patch = patch.object(access, "DATABASE_PATH", self.db)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.server = Flask(__name__)
        self.server.secret_key = "access-tests-only"
        callbacks = {}
        for module in access.GWM_RESTRICTED_MODULES:
            def callback():
                pass
            callback.__module__ = module
            callbacks[module] = {"callback": callback}
        self.pages = {path: {"path": path, "layout": f"Restricted content: {path}"}
                      for path in access.GWM_RESTRICTED_PATHS}
        self.pages["/rfq"] = {"path": "/rfq", "layout": "RFQ content"}
        with patch("dash.page_registry", self.pages):
            access.install_access_controls(SimpleNamespace(server=self.server, callback_map=callbacks))
        self.server.add_url_rule("/rfq", view_func=lambda: "RFQ content")
        for index, path in enumerate((*access.GWM_RESTRICTED_PATHS, "/routines/workspace")):
            self.server.add_url_rule(path, endpoint=f"restricted-{index}", view_func=lambda: "Restricted content")
        self.server.add_url_rule("/_dash-update-component", endpoint="callback", methods=["POST"], view_func=lambda: {"executed": True})
        self.client = self.server.test_client()

    def login(self, key_id):
        with self.client.session_transaction() as session:
            session["user_key_id"] = key_id

    def test_default_signing_key_is_private_and_shared_across_restarts_and_workers(self):
        path = Path(self.temp.name) / "instance" / "session-secret"
        with patch.dict("os.environ", {"DASH_SECRET_KEY": ""}):
            with ThreadPoolExecutor(max_workers=4) as workers:
                keys = list(workers.map(lambda _: access.load_session_secret(path), range(8)))
            self.assertEqual(len(set(keys)), 1)
            self.assertEqual(len(keys[0]), 64)
            self.assertEqual(access.load_session_secret(path), keys[0])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_configured_signing_key_takes_priority_without_creating_a_file(self):
        path = Path(self.temp.name) / "instance" / "session-secret"
        with patch.dict("os.environ", {"DASH_SECRET_KEY": "configured-test-secret"}):
            self.assertEqual(access.load_session_secret(path), "configured-test-secret")
        self.assertFalse(path.exists())

    def test_stored_role_wins_over_modified_browser_role(self):
        self.assertTrue(access.is_gwm({"key_id": 1, "user": "GWM User", "role": "master"}))
        self.assertFalse(access.is_gwm({"key_id": 2, "user": "Trader", "role": "gwm"}))
        self.assertIsNone(access.current_profile({"key_id": 3, "user": "Inactive"}))
        self.assertIsNone(access.current_profile({"key_id": 1, "user": "Someone else"}))
        for user in (None, [], "invalid"):
            self.assertIsNone(access.current_profile(user))

    def test_gwm_direct_urls_redirect_to_rfq(self):
        self.login(1)
        for path in (*access.GWM_RESTRICTED_PATHS, "/routines/workspace?routine=NTNB_RUN"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, "/rfq")
        self.assertEqual(self.client.get("/rfq").status_code, 200)

    def test_gwm_server_callbacks_are_blocked_even_with_forged_or_missing_ui_state(self):
        self.login(1)
        for module in access.GWM_RESTRICTED_MODULES:
            for state in ([], [{"id": "current-user", "property": "data", "value": {"role": "master", "user": "Trader", "key_id": 2}}]):
                response = self.client.post("/_dash-update-component", json={"output": module, "state": state})
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("executed", response.json)

    def test_page_layouts_reject_gwm_during_in_app_navigation(self):
        for path in access.GWM_RESTRICTED_PATHS:
            layout = self.pages[path]["layout"](current_user={"role": "gwm", "user": "GWM User"})
            self.assertIn("unavailable", str(layout))
            self.assertNotIn(f"Restricted content: {path}", str(layout))
        self.assertEqual(self.pages["/rfq"]["layout"], "RFQ content")

    def test_existing_trader_access_is_preserved_and_roles_are_rechecked(self):
        self.login(2)
        for path in access.GWM_RESTRICTED_PATHS:
            self.assertEqual(self.client.get(path).status_code, 200)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE USER_KEYS SET ROLE='gwm' WHERE KEY_ID=2")
        self.assertEqual(self.client.get("/fx-trading").status_code, 302)

    def test_revoked_signed_identity_does_not_open_restricted_pages(self):
        self.login(1)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE USER_KEYS SET FL_ACTIVE=0 WHERE KEY_ID=1")
        self.assertEqual(self.client.get("/fx-trading").status_code, 302)
        response = self.client.post("/_dash-update-component", json={"output": "pages.routines"})
        self.assertEqual(response.status_code, 403)

    def test_clearing_cookie_and_submitting_another_identity_does_not_grant_access(self):
        self.assertEqual(self.client.get("/fx-trading").status_code, 302)
        response = self.client.post("/_dash-update-component", json={
            "output": "pages.routines", "state": [{"id": "current-user", "property": "data",
                "value": {"key_id": 2, "user": "Trader", "role": "trader"}}],
        })
        self.assertEqual(response.status_code, 403)

    def test_navigation_hides_both_groups_and_labels_gwm_profile(self):
        # Load only these real app callbacks, avoiding production database initialization.
        source = Path(__file__).resolve().parents[1] / "app.py"
        tree = ast.parse(source.read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"restrict_navigation", "update_user_fab"}]
        for function in functions:
            function.decorator_list = []
        namespace = {"internal_access_denied": access.internal_access_denied, "re": __import__("re")}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        user = {"user": "GWM User", "role": "gwm"}
        self.assertEqual(namespace["restrict_navigation"](user), ({"display": "none"}, {"display": "none"}))
        self.assertEqual(namespace["restrict_navigation"]({"user": "Trader", "role": "trader"}), ({}, {}))
        self.assertEqual(namespace["update_user_fab"](user)[4], "GWM")
