"""Exercise the actual Dash HTTP callbacks against a temporary database."""

from contextlib import closing
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services.prompts import PromptStore
from services.access import load_session_secret

HAS_DASH = all(importlib.util.find_spec(name) for name in (
    "dash", "dash_ag_grid", "dash_bootstrap_components", "dash_mantine_components",
))


@unittest.skipUnless(HAS_DASH, "Run UI tests in the application's Dash environment.")
class RFQAITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dash import Dash, dcc
        import dash_mantine_components as dmc

        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / "data").mkdir()
        cls.db = cls.root / "data" / "fixed_income.db"
        with closing(sqlite3.connect(cls.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE RFQ_REQUESTERS (NM_REQUESTER TEXT);
                INSERT INTO RFQ_REQUESTERS VALUES ('Alice');
                CREATE TABLE RFQ_COUNTERPARTY (TP_TRADE TEXT, NM_COUNTERPARTY TEXT, FL_ACTIVE INTEGER);
                INSERT INTO RFQ_COUNTERPARTY VALUES ('Sovereign Bond', 'Test Broker', 1);
                CREATE TABLE USER_KEYS (KEY_ID INTEGER PRIMARY KEY, USR_NAME TEXT, EMAIL TEXT, ROLE TEXT, FL_ACTIVE INTEGER);
                INSERT INTO USER_KEYS VALUES (9001, 'GWM Test User', 'gwm@example.test', 'gwm', 1);
                INSERT INTO USER_KEYS VALUES (9002, 'Trader Test User', 'trader@example.test', 'trader', 1);
                INSERT INTO USER_KEYS VALUES (9003, 'Master Test User', 'master@example.test', 'master', 1);
                CREATE TABLE FIXED_INCOME_SECURITY_CLASSIFICATION (
                    NM_SECURITY TEXT, NM_ISSUER TEXT, DT_MATURITY TEXT,
                    NM_INDEX TEXT, NM_SECTOR TEXT, NM_INSTRUMENT_TYPE TEXT
                );
                INSERT INTO FIXED_INCOME_SECURITY_CLASSIFICATION VALUES
                    ('LTN 01/01/2028', 'Treasury', '2028-01-01', 'PRE', 'Sovereign', 'LTN'),
                    ('LFT 01/03/2029', 'Treasury', '2029-03-01', 'SELIC', 'Sovereign', 'LFT'),
                    ('TEST11', 'Test Issuer', '2030-01-01', 'CDI', 'Utilities', 'Debenture');
                CREATE TABLE RFQ (
                    RFQ_ID INTEGER PRIMARY KEY, DT_INPUT TEXT, USR_INPUT TEXT,
                    REQUESTER TEXT, CLASS TEXT, DIRECTION TEXT, TYPE TEXT, SEC_NAME TEXT,
                    DT_MATURITY TEXT, QTY INTEGER, VOLUME REAL, "LIMIT" REAL,
                    STR_RESPONSE TEXT, FL_ACTIVE INTEGER, USR_CLOSED TEXT, DT_CLOSED TEXT
                );
            """)
        cls.app = Dash("rfq_ai_tests", suppress_callback_exceptions=True,
                       assets_folder=str(Path(__file__).resolve().parents[1] / "assets"))
        cls.app.server.secret_key = "rfq-tests-only"
        cls.page = types.ModuleType("rfq_ai_test_page")
        cls.page.__file__ = str(cls.root / "pages" / "request_for_quote.py")
        sys.modules[cls.page.__name__] = cls.page
        source = (Path(__file__).resolve().parents[1] / "pages" / "request_for_quote.py").read_text()
        with patch("dash.register_page"):
            exec(compile(source, cls.page.__file__, "exec"), cls.page.__dict__)
        cls.sovereign_assets = cls.page.load_sovereign_assets()
        cls.corporate_options = cls.page.load_corporate_security_options()
        store = PromptStore(cls.db)
        for asset in cls.page.RFQ_FIELDS:
            store.seed("rfq", f"fill_{asset}", f"Test-only {asset} extraction", f"sqlite-{asset}-model")
        cls.app.layout = dmc.MantineProvider([
            dcc.Store(id="current-user", data={"user": "AI Test User"}), cls.page.layout,
        ])
        cls.app._setup_server()
        cls.client = cls.app.server.test_client()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(cls.page.__name__, None)
        cls.temp.cleanup()

    def dispatch(self, output_fragment, values, changed, match_trade=None, expected_status=200, client=None):
        values = {"rfq-sovereign-assets.data": self.sovereign_assets, "rfq-corporate-options.data": self.corporate_options, **values}
        matches = [(key, spec) for key, spec in self.app.callback_map.items() if output_fragment in key and spec.get("callback")]
        self.assertEqual(len(matches), 1)
        key, spec = matches[0]
        outputs = spec["output"] if isinstance(spec["output"], list) else [spec["output"]]
        def output_id(component_id):
            if isinstance(component_id, dict):
                return {key: match_trade if key == "trade" else value for key, value in component_id.items()}
            return component_id
        output_specs = [{"id": output_id(out.component_id), "property": out.component_property} for out in outputs]
        response = (client or self.client).post("/_dash-update-component", json={
            "output": key,
            "outputs": output_specs if isinstance(spec["output"], list) else output_specs[0],
            "inputs": [{**item, "value": values.get(item["id"] + "." + item["property"])} for item in spec["inputs"]],
            "state": [{**item, "value": values.get(item["id"] + "." + item["property"])} for item in spec["state"]],
            "changedPropIds": changed,
        })
        self.assertEqual(response.status_code, expected_status, response.get_data(as_text=True))
        if expected_status == 204:
            return None
        return response.json["response"]

    def setUp(self):
        with self.client.session_transaction() as session:
            session.clear()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM RFQ")
            conn.execute("DELETE FROM AI_REQUEST_LOG")

    def seed_profile_quotes(self):
        responses = [
            {"broker name": "Test Broker", "broker_response": "99", "timestamp": "2026-11-10T10:00"},
            {"broker name": "Test Broker", "broker_response": "6.20", "timestamp": "2026-11-10T10:01"},
            {"broker name": "Other Broker", "broker_response": "6.40", "timestamp": "2026-11-10T10:02"},
            {"broker name": "Inactive Broker", "broker_response": "100", "timestamp": "2026-11-10T10:03"},
        ]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany("INSERT INTO RFQ_COUNTERPARTY VALUES (?,?,?)", [
                ("Sovereign Bond", "Other Broker", 1), ("Corporate Bond", "Other Broker", 1),
                ("Corporate Bond", "Test Broker", 1), ("Sovereign Bond", "Inactive Broker", 0),
            ])
            for number, asset_class, direction, payload, active in [
                (501, "Sovereign Bond", "Buy", responses, 1),
                (502, "Sovereign Bond", "Sell", responses, 1),
                (503, "Corporate Bond", "Buy", responses, 1),
                (504, "Corporate Bond", "Sell", responses, 1),
                (505, "Sovereign Bond", "Buy", [], 1),
                (506, "Sovereign Bond", "Buy", responses, 0),
            ]:
                conn.execute('''INSERT INTO RFQ (RFQ_ID,CLASS,DIRECTION,STR_RESPONSE,FL_ACTIVE,DT_INPUT,
                    REQUESTER,USR_INPUT,TYPE,SEC_NAME,QTY,"LIMIT") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (number, asset_class, direction, json.dumps(payload), active, "2026-11-10T10:00",
                     "Trader", "Trader", "Mkt Color", "Test security", 10, 6.3))

        def cleanup():
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("DELETE FROM RFQ_COUNTERPARTY WHERE NM_COUNTERPARTY IN ('Other Broker','Inactive Broker') OR TP_TRADE='Corporate Bond'")
        self.addCleanup(cleanup)

    def test_gwm_receives_only_best_quote_for_both_fixed_income_tabs(self):
        self.seed_profile_quotes()
        for tab in ("sovereign", "corporate"):
            response = self.dispatch("rfq-grid.rowData", {
                "rfq-tabs.value": tab, "rfq-active-switch.checked": True,
                "current-user.data": {"key_id": 9001, "user": "GWM Test User", "role": "gwm"},
            }, ["current-user.data", "rfq-tabs.value"])
            grid = response["rfq-grid"]
            columns = grid["columnDefs"]
            quotes = [col for col in columns if col.get("cellRenderer") == "RfqQuoteCell"]
            self.assertEqual(len(quotes), 1)
            self.assertEqual(quotes[0]["headerName"], "Best")
            self.assertFalse(quotes[0]["editable"])
            self.assertEqual(quotes[0]["cellClassRules"]["rfq-quote-highlight"], "rfqQuoteHighlight(params)")
            self.assertFalse(any(col["field"].startswith("cp_") for col in columns))
            for row in grid["rowData"]:
                expected = "--" if row["rfq_id"] == 505 else "6.40|10:02" if row["direction"] == "Buy" else "6.20|10:01"
                self.assertEqual(row["best_quote"], expected)
                self.assertFalse(any(name.startswith("cp_") for name in row))
            serialized = json.dumps(response)
            for private in ("Test Broker", "Other Broker", "Inactive Broker", "STR_RESPONSE"):
                self.assertNotIn(private, serialized)

    def test_traders_keep_counterparty_columns_and_login_changes_refresh_the_view(self):
        self.seed_profile_quotes()
        for role in ("trader", "master"):
            response = self.dispatch("rfq-grid.rowData", {
                "rfq-tabs.value": "sovereign", "rfq-active-switch.checked": True,
                "current-user.data": {"user": "Trader", "role": role},
            }, ["current-user.data"])
            fields = {col["field"] for col in response["rfq-grid"]["columnDefs"]}
            self.assertIn(self.page._counterparty_slug("Test Broker"), fields)
            self.assertIn(self.page._counterparty_slug("Other Broker"), fields)
            self.assertNotIn("best_quote", fields)
            actions = next(col for col in response["rfq-grid"]["columnDefs"]
                           if col.get("field") == "actions")
            self.assertFalse(actions["cellRendererParams"]["mktColorOnly"])
        logged_out = self.dispatch("rfq-grid.rowData", {"current-user.data": None}, ["current-user.data"])
        self.assertEqual(logged_out["rfq-grid"]["rowData"], [])

    def test_gwm_cannot_request_raw_quotes_by_changing_browser_role(self):
        self.seed_profile_quotes()
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9001
        response = self.dispatch("rfq-grid.rowData", {
            "rfq-tabs.value": "sovereign", "rfq-active-switch.checked": True,
            "current-user.data": {"role": "master", "user": "Someone else"},
        }, ["rfq-tabs.value"])
        self.assertTrue(all(not key.startswith("cp_") for row in response["rfq-grid"]["rowData"] for key in row))
        self.assertIn("best_quote", {col["field"] for col in response["rfq-grid"]["columnDefs"]})

    def test_gwm_best_quotes_also_apply_to_closed_rfqs(self):
        self.seed_profile_quotes()
        response = self.dispatch("rfq-grid.rowData", {
            "rfq-tabs.value": "sovereign", "rfq-active-switch.checked": False,
            "current-user.data": {"user": "GWM Test User", "role": "gwm"},
        }, ["rfq-active-switch.checked"])
        self.assertEqual([row["rfq_id"] for row in response["rfq-grid"]["rowData"]], [506])
        self.assertEqual(response["rfq-grid"]["rowData"][0]["best_quote"], "6.40|10:02")

    def test_gwm_cannot_edit_counterparty_quotes_with_a_direct_callback(self):
        self.seed_profile_quotes()
        with closing(sqlite3.connect(self.db)) as conn:
            before = conn.execute("SELECT STR_RESPONSE FROM RFQ WHERE RFQ_ID=501").fetchone()[0]
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9001
        self.dispatch("rfq-error-notification.message", {
            "rfq-grid.cellValueChanged": {"colId": self.page._counterparty_slug("Test Broker"),
                                          "data": {"rfq_id": 501}, "newValue": "7.5|10:45"},
            "rfq-active-switch.checked": True, "current-user.data": {"role": "trader", "user": "Trader"},
        }, ["rfq-grid.cellValueChanged"], expected_status=204)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT STR_RESPONSE FROM RFQ WHERE RFQ_ID=501").fetchone()[0], before)

    def test_gwm_can_create_rfq_with_authenticated_requester(self):
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9001
        response = self.dispatch("rfq-notification-container.sendNotifications", {
            **self.sovereign_state([{"direction": "Buy", "bond_name": "LTN",
                                    "maturity": "LTN 01/01/2028", "quantity": 10}]),
            "rfq-submit-sovereign.n_clicks": 1, "current-user.data": {"user": "Forged User", "role": "master"},
        }, ["rfq-submit-sovereign.n_clicks"])
        self.assertIn("rfq-refresh", response)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT REQUESTER, USR_INPUT, TYPE FROM RFQ").fetchone(),
                             ("GWM Test User", "GWM Test User", "Mkt Color"))

    def test_best_quote_parser_matches_existing_highlight_rules(self):
        quotes = ["--", "pending", "6.25%|09:00", "6.5|09:01", "Infinity", "1e999"]
        self.assertEqual(self.page.best_rfq_quote(quotes, "Buy"), "6.5|09:01")
        self.assertEqual(self.page.best_rfq_quote(quotes, "Sell"), "6.25%|09:00")
        self.assertEqual(self.page.best_rfq_quote(["pending", "--"], "Buy"), "--")
        self.assertEqual(self.page.best_rfq_quote(quotes, None), "--")
        self.assertEqual(self.page.best_rfq_quote(["6.5|09:00", "6.5|10:00"], "Buy"), "6.5|09:00")

    def test_initial_layout_contains_no_quote_data_or_counterparty_names(self):
        grid = self.page.rfq_components()["rfq-grid"]
        self.assertEqual(grid.rowData, [])
        self.assertFalse(any(col["field"].startswith("cp_") for col in grid.columnDefs))

    def test_each_row_contains_ready_to_copy_text_for_the_browser_click(self):
        self.seed_profile_quotes()
        for kind in ("Sovereign Bond", "Corporate Bond"):
            counterparties = self.page.load_counterparties(kind)
            for best_only in (False, True):
                rows = self.page.load_rfq_rows(kind, counterparties, best_only=best_only)
                self.assertTrue(rows)
                for row in rows:
                    side = "bid" if row["direction"] == "Sell" else "offer"
                    self.assertEqual(row["rfq_copy_text"], f"{side} Test security 10q D0")
                    for name in ("Test Broker", "Other Broker", "Inactive Broker"):
                        self.assertNotIn(name, row["rfq_copy_text"])
        components = self.page.rfq_components()
        self.assertNotIn("rfq-copy-clipboard", components)

    def test_close_action_still_saves_and_copy_is_handled_in_the_browser(self):
        self.seed_profile_quotes()
        callback_key = next(key for key, spec in self.app.callback_map.items()
                            if getattr(spec.get("callback"), "__name__", None) == "handle_rfq_action")
        self.dispatch(callback_key, {
            "rfq-grid.cellRendererData": {"value": {"action": "copy", "rfq_id": 501}},
            "rfq-active-switch.checked": True, "current-user.data": {"user": "Test User"},
        }, ["rfq-grid.cellRendererData"], expected_status=204)
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9002
        result = self.dispatch(callback_key, {
            "rfq-grid.cellRendererData": {"value": {"action": "close", "rfq_id": 501}},
            "rfq-active-switch.checked": True, "current-user.data": {"user": "Forged User"},
            "rfq-refresh.data": 0,
        }, ["rfq-grid.cellRendererData"])
        self.assertEqual(result["rfq-refresh"]["data"], 1)
        with closing(sqlite3.connect(self.db)) as conn:
            active, user = conn.execute("SELECT FL_ACTIVE, USR_CLOSED FROM RFQ WHERE RFQ_ID=501").fetchone()
        self.assertEqual((active, user), (0, "Trader Test User"))

    def test_gwm_has_mkt_color_only_conclude_action_in_every_rfq_tab(self):
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9001
        for tab in ("sovereign", "corporate", "derivatives"):
            with self.subTest(tab=tab):
                response = self.dispatch("rfq-grid.rowData", {
                    "rfq-tabs.value": tab, "rfq-active-switch.checked": True,
                    "current-user.data": {"user": "Forged User", "role": "master"},
                }, ["rfq-tabs.value"])
                actions = next(col for col in response["rfq-grid"]["columnDefs"]
                               if col.get("field") == "actions")
                self.assertTrue(actions["cellRendererParams"]["mktColorOnly"])

    def test_signed_users_keep_their_grid_after_a_server_restart(self):
        self.seed_profile_quotes()
        path = self.root / ".instance" / "session-secret"
        with patch.dict(os.environ, {"DASH_SECRET_KEY": ""}), \
                patch.dict(self.app.server.config, {
                    "DASHRF_PROFILE_ACCESS": True, "SECRET_KEY": load_session_secret(path),
                }):
            for key_id, role in ((9001, "gwm"), (9002, "trader"), (9003, "master")):
                with self.subTest(role=role):
                    with self.client.session_transaction() as session:
                        session["user_key_id"] = key_id
                    # A restarted server loads the same key and accepts the original cookie.
                    self.app.server.secret_key = load_session_secret(path)
                    values = {"current-user.data": {"user": "Browser User", "role": "master"}}
                    for tab in ("sovereign", "corporate"):
                        response = self.dispatch("rfq-grid.rowData", {
                            **values, "rfq-tabs.value": tab, "rfq-active-switch.checked": True,
                        }, ["rfq-tabs.value"])
                        self.assertTrue(response["rfq-grid"]["rowData"])
                        actions = next(col for col in response["rfq-grid"]["columnDefs"]
                                       if col.get("field") == "actions")
                        self.assertEqual(actions["cellRendererParams"]["mktColorOnly"], role == "gwm")
                    auth = self.dispatch("rfq-auth-content.style", values, ["current-user.data"])
                    self.assertEqual(auth["rfq-auth-content"]["style"], {"display": "block"})
                    self.assertEqual(auth["rfq-auth-message"]["style"], {"display": "none"})

    def test_expired_session_displays_sign_in_message_instead_of_an_empty_grid(self):
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9001
        with patch.dict(self.app.server.config, {
            "DASHRF_PROFILE_ACCESS": True, "SECRET_KEY": "a-different-startup-key",
        }):
            response = self.dispatch("rfq-auth-content.style", {
                "current-user.data": {"user": "GWM Test User", "role": "gwm"},
            }, ["current-user.data"])
            self.assertEqual(response["rfq-auth-content"]["style"], {"display": "none"})
            self.assertIn("session has expired", response["rfq-auth-message"]["children"])
            self.assertEqual(response["rfq-auth-message"]["style"], {"display": "block"})

    def test_gwm_can_conclude_only_stored_mkt_color_type_in_all_asset_classes(self):
        callback_key = next(key for key, spec in self.app.callback_map.items()
                            if getattr(spec.get("callback"), "__name__", None) == "handle_rfq_action")
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9001
        for asset_class in ("Sovereign Bond", "Corporate Bond", "Derivatives"):
            for quote_type in ("Mkt Color", " MKT COLOR ", "Trade", "Live", None, ""):
                with self.subTest(asset_class=asset_class, quote_type=quote_type):
                    with closing(sqlite3.connect(self.db)) as conn, conn:
                        rfq_id = conn.execute(
                            "INSERT INTO RFQ (CLASS,TYPE,FL_ACTIVE) VALUES (?,?,1)",
                            (asset_class, quote_type),
                        ).lastrowid
                    allowed = str(quote_type or "").strip().lower() == "mkt color"
                    result = self.dispatch(callback_key, {
                        "rfq-grid.cellRendererData": {"value": {
                            "action": "close", "rfq_id": rfq_id, "quote_format": "Mkt Color",
                        }},
                        "rfq-active-switch.checked": True,
                        "current-user.data": {"user": "Forged User", "role": "master"},
                        "rfq-refresh.data": 0,
                    }, ["rfq-grid.cellRendererData"], expected_status=200 if allowed else 204)
                    with closing(sqlite3.connect(self.db)) as conn:
                        active, user, closed_at = conn.execute(
                            "SELECT FL_ACTIVE,USR_CLOSED,DT_CLOSED FROM RFQ WHERE RFQ_ID=?", (rfq_id,),
                        ).fetchone()
                    if allowed:
                        self.assertEqual(result["rfq-refresh"]["data"], 1)
                        self.assertEqual((active, user), (0, "GWM Test User"))
                        self.assertIsNotNone(closed_at)
                    else:
                        self.assertEqual((active, user, closed_at), (1, None, None))

    def test_trader_and_master_can_still_conclude_other_quote_types(self):
        callback_key = next(key for key, spec in self.app.callback_map.items()
                            if getattr(spec.get("callback"), "__name__", None) == "handle_rfq_action")
        for key_id, username in ((9002, "Trader Test User"), (9003, "Master Test User")):
            with self.client.session_transaction() as session:
                session["user_key_id"] = key_id
            for quote_type in ("Mkt Color", "Trade", "Live", None):
                with self.subTest(username=username, quote_type=quote_type):
                    with closing(sqlite3.connect(self.db)) as conn, conn:
                        rfq_id = conn.execute("INSERT INTO RFQ (TYPE,FL_ACTIVE) VALUES (?,1)",
                                              (quote_type,)).lastrowid
                    self.dispatch(callback_key, {
                        "rfq-grid.cellRendererData": {"value": {"action": "close", "rfq_id": rfq_id}},
                        "rfq-active-switch.checked": True, "rfq-refresh.data": 0,
                    }, ["rfq-grid.cellRendererData"])
                    with closing(sqlite3.connect(self.db)) as conn:
                        self.assertEqual(conn.execute(
                            "SELECT FL_ACTIVE,USR_CLOSED FROM RFQ WHERE RFQ_ID=?", (rfq_id,),
                        ).fetchone(), (0, username))

    def test_live_poll_shares_quote_updates_between_signed_users(self):
        self.seed_profile_quotes()
        reader = self.app.server.test_client()
        with reader.session_transaction() as session:
            session["user_key_id"] = 9001
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9003
        values = {"rfq-live-interval.n_intervals": 1, "rfq-tabs.value": "sovereign",
                  "rfq-active-switch.checked": True, "rfq-refresh.data": 0,
                  "current-user.data": {"user": "Forged User", "role": "master"}}
        before = self.dispatch("rfq-grid.rowData", values, ["rfq-tabs.value"], client=reader)["rfq-grid"]
        self.dispatch("rfq-error-notification.message", {
            "rfq-grid.cellValueChanged": {"colId": self.page._counterparty_slug("Test Broker"),
                                          "data": {"rfq_id": 501}, "newValue": "7.5|10:45"},
            "rfq-active-switch.checked": True, "rfq-refresh.data": 0,
        }, ["rfq-grid.cellValueChanged"])
        response = self.dispatch("rfq-live-snapshot.data", {
            **values, "rfq-grid.rowData": before["rowData"], "rfq-grid.columnDefs": before["columnDefs"],
        }, ["rfq-live-interval.n_intervals"], client=reader)
        self.assertEqual(set(response), {"rfq-live-snapshot"})
        snapshot = response["rfq-live-snapshot"]["data"]
        updated = next(row for row in snapshot["rows"] if row["rfq_id"] == 501)
        self.assertEqual(updated["best_quote"], "7.5|10:45")
        self.assertNotIn("Test Broker", json.dumps(snapshot))
        self.assertFalse(any(key.startswith("cp_") for row in snapshot["rows"] for key in row))
        self.dispatch("rfq-live-snapshot.data", {
            **values, "rfq-grid.rowData": snapshot["rows"], "rfq-grid.columnDefs": snapshot["columns"],
        }, ["rfq-live-interval.n_intervals"], expected_status=204, client=reader)

    def test_live_poll_shares_created_and_concluded_rfqs_across_all_tabs(self):
        reader = self.app.server.test_client()
        with reader.session_transaction() as session:
            session["user_key_id"] = 9002
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9003
        forms = {
            "sovereign": self.sovereign_state([{"direction": "Buy", "bond_name": "LTN",
                                                 "maturity": "LTN 01/01/2028", "quantity": 10}]),
            "corporate": self.corporate_state([{"direction": "Sell", "bond_type": "Corporate Bonds",
                                                 "security": "TEST11", "volume": 5000}]),
            "derivatives": {"rfq-direction-derivatives.value": "Buy", "rfq-quote-type-derivatives.value": "Trade",
                            "rfq-bond-name-derivatives.value": "DOL", "rfq-maturity-derivatives.value": 3,
                            "rfq-maturity-unit-derivatives.value": "M", "rfq-volume-derivatives.value": 1000000,
                            "rfq-limit-derivatives.value": 0.05, "rfq-strategy-derivatives.value": "Call",
                            "rfq-strike-solve-derivatives.value": ["solve"], "rfq-barrier-type-derivatives.value": "No Barrier"},
        }
        close_key = next(key for key, spec in self.app.callback_map.items()
                         if getattr(spec.get("callback"), "__name__", None) == "handle_rfq_action")
        for tab, form in forms.items():
            with self.subTest(tab=tab):
                self.dispatch("rfq-notification-container.sendNotifications", {
                    **form, f"rfq-submit-{tab}.n_clicks": 1, "rfq-refresh.data": 0,
                }, [f"rfq-submit-{tab}.n_clicks"])
                values = {"rfq-live-interval.n_intervals": 1, "rfq-tabs.value": tab,
                          "rfq-active-switch.checked": True, "rfq-refresh.data": 0}
                snapshot = self.dispatch("rfq-live-snapshot.data", values,
                                         ["rfq-live-interval.n_intervals"], client=reader)["rfq-live-snapshot"]["data"]
                self.assertEqual(len(snapshot["rows"]), 1)
                rfq_id = snapshot["rows"][0]["rfq_id"]
                self.dispatch(close_key, {
                    "rfq-grid.cellRendererData": {"value": {"action": "close", "rfq_id": rfq_id}},
                    "rfq-active-switch.checked": True, "rfq-refresh.data": 0,
                }, ["rfq-grid.cellRendererData"])
                closed = self.dispatch("rfq-live-snapshot.data", {
                    **values, "rfq-grid.rowData": snapshot["rows"], "rfq-grid.columnDefs": snapshot["columns"],
                }, ["rfq-live-interval.n_intervals"], client=reader)["rfq-live-snapshot"]["data"]
                self.assertEqual(closed["rows"], [])
                history = self.dispatch("rfq-live-snapshot.data", {
                    **values, "rfq-active-switch.checked": False,
                }, ["rfq-live-interval.n_intervals"], client=reader)["rfq-live-snapshot"]["data"]
                self.assertEqual([row["rfq_id"] for row in history["rows"]], [rfq_id])

    def test_live_poll_is_disabled_for_guests_and_does_not_control_forms(self):
        auth = self.dispatch("rfq-auth-content.style", {}, ["current-user.data"])
        self.assertTrue(auth["rfq-live-interval"]["disabled"])
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9001
        auth = self.dispatch("rfq-auth-content.style", {}, ["current-user.data"])
        self.assertFalse(auth["rfq-live-interval"]["disabled"])
        components = self.page.rfq_components()
        self.assertEqual(components["rfq-live-interval"].interval, 2000)
        for callback in self.app._callback_list:
            if any(item["id"] in ("rfq-live-interval", "rfq-live-snapshot") for item in callback["inputs"]):
                self.assertNotIn(".opened", callback["output"])
                self.assertNotIn("rfq-refresh.data", callback["output"])

    def test_live_poll_rechecks_profile_changes_and_revocation(self):
        self.seed_profile_quotes()
        with self.client.session_transaction() as session:
            session["user_key_id"] = 9002
        values = {"rfq-live-interval.n_intervals": 1, "rfq-tabs.value": "sovereign",
                  "rfq-active-switch.checked": True, "current-user.data": {"role": "master"}}
        initial = self.dispatch("rfq-grid.rowData", values, ["rfq-tabs.value"])["rfq-grid"]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE USER_KEYS SET ROLE='gwm' WHERE KEY_ID=9002")
        try:
            snapshot = self.dispatch("rfq-live-snapshot.data", {
                **values, "rfq-grid.rowData": initial["rowData"], "rfq-grid.columnDefs": initial["columnDefs"],
            }, ["rfq-live-interval.n_intervals"])["rfq-live-snapshot"]["data"]
            self.assertTrue(snapshot["rows"])
            self.assertTrue(all("best_quote" in row for row in snapshot["rows"]))
            self.assertNotIn("Test Broker", json.dumps(snapshot))
            actions = next(col for col in snapshot["columns"] if col.get("field") == "actions")
            self.assertTrue(actions["cellRendererParams"]["mktColorOnly"])
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE USER_KEYS SET FL_ACTIVE=0 WHERE KEY_ID=9002")
            revoked = self.dispatch("rfq-live-snapshot.data", {
                **values, "rfq-grid.rowData": snapshot["rows"], "rfq-grid.columnDefs": snapshot["columns"],
            }, ["rfq-live-interval.n_intervals"])["rfq-live-snapshot"]["data"]
            self.assertEqual(revoked["rows"], [])
        finally:
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE USER_KEYS SET ROLE='trader',FL_ACTIVE=1 WHERE KEY_ID=9002")

    def test_schema_removes_legacy_requesters_and_preserves_rfq_history(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'RFQ_REQUESTERS'"
            ).fetchone())
            conn.execute("CREATE TABLE RFQ_REQUESTERS (NM_REQUESTER TEXT)")
            conn.execute("INSERT INTO RFQ_REQUESTERS VALUES ('Legacy User')")
            conn.execute("INSERT INTO RFQ (REQUESTER, USR_INPUT) VALUES ('Legacy User', 'Legacy User')")
        self.page.ensure_rfq_schema()
        self.page.ensure_rfq_schema()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'RFQ_REQUESTERS'"
            ).fetchone())
            self.assertEqual(conn.execute("SELECT REQUESTER, USR_INPUT FROM RFQ").fetchall(),
                             [('Legacy User', 'Legacy User')])

    def ai_dispatch(self, asset_class, patch_values, *, relevant=True, count=1, conflicts=False, unresolved=None,
                    text="Fill one complete test RFQ", usage=None, trades=None):
        fields = self.page.rfq_empty_form(asset_class, "AI Test User")
        fields.update(patch_values)
        data = {
            "asset_class": asset_class, "is_rfq_request": relevant,
            "request_count": count, "has_conflicts": conflicts,
            "fields": fields, "unresolved_fields": unresolved or [],
        }
        if asset_class in self.page.MULTI_TRADE_ASSETS:
            data.pop("fields")
            data.pop("unresolved_fields")
            entries = trades if trades is not None else [patch_values] * count
            data["trades"] = [
                {"fields": {**self.page.rfq_empty_form(asset_class, "AI Test User"), **entry},
                 "unresolved_fields": unresolved or []} for entry in entries
            ]
            if trades is not None:
                data["request_count"] = len(trades)
        values = {
            f"rfq-ai-apply-{asset_class}.n_clicks": 1,
            f"rfq-ai-text-{asset_class}.value": text,
            f"rfq-form-{asset_class}.opened": True,
            "current-user.data": {"user": "AI Test User"},
        }
        self.service, self.sdk_client = self.sdk_service(data, usage=usage)
        with patch.object(self.page, "OpenAIService", return_value=self.service):
            return self.dispatch(f"rfq-ai-status-{asset_class}.children", values, [f"rfq-ai-apply-{asset_class}.n_clicks"])

    def sdk_service(self, data, *, raw=None, usage=None):
        """Exercise real transport/audit while replacing only the SDK network call."""
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            status="completed", output=[], output_text=raw if raw is not None else json.dumps(data),
            model="test-model", id="test-response", usage=usage,
        )
        service = self.page.OpenAIService(client=client, database_path=self.db)
        extract = service.extract
        watched = Mock()

        def call(**kwargs):
            result = extract(**kwargs)
            watched.return_value = result
            return result

        watched.side_effect = call
        service.extract = watched
        return service, client

    @staticmethod
    def pattern_key(component_id, prop):
        return json.dumps(component_id, sort_keys=True, separators=(",", ":")) + "." + prop

    def sovereign_state(self, trades, trade_ids=None):
        return self.fixed_income_state("sovereign", trades, trade_ids)

    def corporate_state(self, trades, trade_ids=None):
        return self.fixed_income_state("corporate", trades, trade_ids)

    def fixed_income_state(self, asset_class, trades, trade_ids=None):
        trade_ids = trade_ids or [f"test-{number}" for number in range(len(trades))]
        ids, values = [], []
        for trade_id, fields in zip(trade_ids, trades):
            for name in self.page.RFQ_FIELDS[asset_class]:
                ids.append(self.page.rfq_trade_field_id(asset_class, name, trade_id))
                values.append(fields.get(name))
        pattern = {"type": f"rfq-{asset_class}-field", "field": ["ALL"], "trade": ["ALL"]}
        return {self.pattern_key(pattern, "id"): ids, self.pattern_key(pattern, "value"): values}

    def maturity_dispatch(self, bond, current_value, assets=None, trade_id="test-0"):
        bond_pattern = {"type": "rfq-sovereign-field", "field": "bond_name", "trade": ["MATCH"]}
        maturity_pattern = {"type": "rfq-sovereign-field", "field": "maturity", "trade": ["MATCH"]}
        result = self.dispatch('"field":"maturity"', {
            self.pattern_key(bond_pattern, "value"): bond,
            self.pattern_key(maturity_pattern, "value"): current_value,
            "rfq-sovereign-assets.data": self.sovereign_assets if assets is None else assets,
        }, [self.pattern_key(self.page.sovereign_field_id("bond_name", trade_id), "value")], match_trade=trade_id)
        return result[self.pattern_key(self.page.sovereign_field_id("maturity", trade_id), "value").rsplit(".", 1)[0]]

    def manage_trades(self, trades=None, changed=None, asset_class="sovereign", **overrides):
        changed = changed or f"rfq-form-{asset_class}.opened"
        values = {f"rfq-form-{asset_class}.opened": True, "current-user.data": {"user": "AI Test User"},
                  **self.fixed_income_state(asset_class, trades or []), **overrides}
        return self.dispatch(f"rfq-{asset_class}-trades.children", values, [changed])[f"rfq-{asset_class}-trades"]["children"]

    @staticmethod
    def block_fields(block):
        fields = {}
        def visit(node):
            if isinstance(node, list):
                for child in node:
                    visit(child)
            elif isinstance(node, dict):
                props = node.get("props", {})
                component_id = props.get("id")
                if isinstance(component_id, dict) and component_id.get("type") in ("rfq-sovereign-field", "rfq-corporate-field"):
                    fields[component_id["field"]] = props
                visit(props.get("children"))
        visit(block)
        return fields

    def test_ai_panel_precedes_trade_fields_and_actions(self):
        for kind in self.page.RFQ_FIELDS:
            modal = self.page.rfq_form_modal(kind, "Test", ["form", "actions"])
            self.assertEqual(modal.children[0].id, f"rfq-ai-panel-{kind}")
            self.assertEqual(modal.children[1].id, f"rfq-fields-{kind}")
            self.assertEqual(modal.children[2], "actions")
            self.assertTrue(modal.keepMounted)

    def test_sovereign_values_survive_dependent_dropdown_updates(self):
        result = self.ai_dispatch("sovereign", {
            "direction": "Sell", "requester": "Someone Else",
            "bond_name": "LTN", "maturity": "LTN 01/01/2028", "quantity": 5000,
        })
        draft = result["rfq-ai-result-sovereign"]["data"]
        self.assertEqual(draft["trades"][0]["requester"], "AI Test User")
        blocks = self.manage_trades(changed="rfq-ai-result-sovereign.data", **{"rfq-ai-result-sovereign.data": draft})
        fields = self.block_fields(blocks[0])
        self.assertEqual(fields["bond_name"]["value"], "LTN")
        maturity = self.maturity_dispatch("LTN", fields["maturity"]["value"])
        self.assertEqual(maturity["value"], "LTN 01/01/2028")

    def test_sovereign_catalog_single_query_per_modal_open(self):
        statements = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        def catalog_queries():
            return [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
                    and "FIXED_INCOME_SECURITY_CLASSIFICATION" in sql]

        with patch.object(self.page.sqlite3, "connect", side_effect=traced_connect):
            opened = self.dispatch("rfq-sovereign-assets.data", {
                "rfq-form-sovereign.opened": True,
            }, ["rfq-form-sovereign.opened"])
            assets = opened["rfq-sovereign-assets"]["data"]
            self.assertEqual(assets, [
                {"asset_class": "LTN", "maturity": "2028-01-01", "security": "LTN 01/01/2028"},
                {"asset_class": "LFT", "maturity": "2029-03-01", "security": "LFT 01/03/2029"},
            ])
            for bond, security in [("LTN", "LTN 01/01/2028"), ("LFT", "LFT 01/03/2029")]:
                filtered = self.maturity_dispatch(bond, None, assets=assets)
                self.assertEqual([item["value"] for item in filtered["options"]], [security])
            contract = self.page.rfq_json_contract("sovereign", "Test User", sovereign_assets=assets)
            self.assertEqual(contract["maturity_options_by_bond"]["LTN"][0]["value"], "LTN 01/01/2028")
            response = self.ai_dispatch("sovereign", {
                "direction": "Buy", "bond_name": "LTN", "maturity": "LTN 01/01/2028", "quantity": 10,
            })
            fields = response["rfq-ai-result-sovereign"]["data"]["trades"][0]
            state = self.sovereign_state([fields])
            submitted = self.dispatch("rfq-notification-container.sendNotifications", {
                **state, "rfq-submit-sovereign.n_clicks": 1, "rfq-sovereign-assets.data": assets,
                "current-user.data": {"user": "Test User"}, "rfq-refresh.data": 0,
            }, ["rfq-submit-sovereign.n_clicks"])
            self.assertEqual(submitted["rfq-refresh"]["data"], 1)
            closed = self.dispatch("rfq-sovereign-assets.data", {
                "rfq-form-sovereign.opened": False,
            }, ["rfq-form-sovereign.opened"])
            self.assertEqual(closed["rfq-sovereign-assets"]["data"], [])
            self.assertEqual(len(catalog_queries()), 1)
            self.dispatch("rfq-sovereign-assets.data", {
                "rfq-form-sovereign.opened": True,
            }, ["rfq-form-sovereign.opened"])
            self.assertEqual(len(catalog_queries()), 2)

    def test_requester_is_read_only_and_always_session_username(self):
        components = self.page.rfq_components()
        for kind in self.page.RFQ_FIELDS:
            self.assertTrue(components[f"rfq-requester-{kind}"].readOnly)
        result = self.dispatch("rfq-requester-derivatives.value", {
            "current-user.data": {"user": "Signed In Username"},
        }, ["current-user.data"])
        self.assertEqual(result["rfq-requester-derivatives"]["value"], "Signed In Username")
        for kind in self.page.MULTI_TRADE_ASSETS:
            blocks = self.manage_trades(asset_class=kind, **{"current-user.data": {"user": "Signed In Username"}})
            self.assertEqual(self.block_fields(blocks[0])["requester"]["value"], "Signed In Username")

    def test_corporate_and_derivatives_apply_without_creating_records(self):
        corporate = self.ai_dispatch("corporate", {
            "direction": "Buy", "bond_type": "Corporate Bonds",
            "security": "TEST11", "volume": 1000000,
        })
        self.assertEqual(corporate["rfq-ai-result-corporate"]["data"]["trades"][0]["security"], "TEST11")
        derivatives = self.ai_dispatch("derivatives", {
            "direction": "Buy", "security": "DOL", "maturity": 3,
            "maturity_unit": "M", "volume": 10000000, "premium": 0.05,
            "strategy": "Call", "strike_solve": True,
        })
        self.assertEqual(derivatives["rfq-strike-solve-derivatives"]["value"], ["solve"])
        self.assertEqual(derivatives["rfq-volume-derivatives"]["value"], 10000000)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM RFQ").fetchone()[0], 0)

    def test_new_trade_clears_omitted_fields_and_sends_no_previous_state(self):
        first = self.ai_dispatch("corporate", {"direction": "Buy", "security": "TEST11", "quantity": 100, "yield_limit": 13})
        self.assertEqual(first["rfq-ai-result-corporate"]["data"]["trades"][0]["quantity"], 100)
        second = self.ai_dispatch("corporate", {"direction": "Sell", "security": "TEST11", "volume": 1000000})
        self.assertIsNone(second["rfq-ai-result-corporate"]["data"]["trades"][0]["quantity"])
        self.assertIsNone(second["rfq-ai-result-corporate"]["data"]["trades"][0]["yield_limit"])
        payload = self.service.extract.call_args.kwargs["payload"]
        self.assertNotIn("current_form", payload)
        self.assertNotIn("previous_response", payload)
        self.assertEqual(set(payload), {"request_text", "requester_username", "form_contract"})

    def test_configuration_error_resets_all_trade_fields(self):
        values = {
            "rfq-ai-apply-sovereign.n_clicks": 1, "rfq-ai-text-sovereign.value": "Buy LTN",
            "rfq-form-sovereign.opened": True, "current-user.data": {"user": "Test User"},
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            result = self.dispatch("rfq-ai-status-sovereign.children", values, ["rfq-ai-apply-sovereign.n_clicks"])
        draft = result["rfq-ai-result-sovereign"]["data"]
        self.assertEqual(draft["trades"], [])
        blocks = self.manage_trades(changed="rfq-ai-result-sovereign.data", **{"rfq-ai-result-sovereign.data": draft})
        self.assertIsNone(self.block_fields(blocks[0])["quantity"]["value"])
        self.assertIsNone(self.block_fields(blocks[0])["direction"]["value"])
        self.assertIn("unavailable", str(result["rfq-ai-status-sovereign"]))

    def test_conflicting_and_unrelated_requests_leave_form_empty(self):
        for kwargs in [{"conflicts": True}, {"relevant": False, "count": 0}]:
            with self.subTest(kwargs=kwargs):
                result = self.ai_dispatch("corporate", {"direction": "Buy", "quantity": 50, "security": "TEST11"}, **kwargs)
                self.assertEqual(result["rfq-ai-result-corporate"]["data"]["trades"], [])

    def test_contracts_have_distinct_json_fields_and_use_exact_form_options(self):
        schemas = {}
        for kind in self.page.RFQ_FIELDS:
            contract = self.page.rfq_json_contract(kind, "Test User")
            schemas[kind] = self.page.rfq_field_schema(contract)
            self.assertEqual(schemas[kind]["requester"]["enum"], ["Test User"])
        self.assertIn("bond_name", schemas["sovereign"])
        self.assertNotIn("security_text", schemas["sovereign"])
        self.assertIn("security_text", schemas["corporate"])
        self.assertNotIn("strike", schemas["corporate"])
        self.assertIn("strike", schemas["derivatives"])
        components = self.page.rfq_components()
        ticker = components["rfq-bond-name-derivatives"]
        original = ticker.options
        try:
            ticker.options = [{"label": "New Ticker", "value": "NEW"}]
            contract = self.page.rfq_json_contract("derivatives", "Test User")
            self.assertEqual(contract["output_schema"]["properties"]["fields"]["properties"]["security"]["enum"], ["NEW", None])
        finally:
            ticker.options = original
        corporate = self.page.rfq_json_contract("corporate", "Test User")
        self.assertEqual(corporate["field_definitions"]["security"]["options"], self.page.load_corporate_security_options())
        self.assertTrue(corporate["field_definitions"]["yield_limit"]["optional"])
        self.assertEqual(corporate["validation_rules"]["one_of"], [["quantity", "volume"]])

    def test_fixed_income_quote_type_is_fixed_and_has_no_form_control(self):
        components = self.page.rfq_components()
        for kind in ("sovereign", "corporate"):
            self.assertNotIn(f"rfq-quote-type-{kind}", components)
            self.assertNotIn("quote_type", self.page.RFQ_FIELDS[kind])
            contract = self.page.rfq_json_contract(kind, "Test User")
            self.assertEqual(contract["fixed_values"]["quote_type"], "Mkt Color")
            self.assertNotIn("quote_type", self.page.rfq_field_schema(contract))
            self.assertNotIn("quote_type", self.page.rfq_missing_fields(kind, {}))
        submit = next(spec for key, spec in self.app.callback_map.items()
                      if "rfq-notification-container.sendNotifications" in key and spec.get("callback"))
        self.assertFalse(any(item["id"] in ("rfq-quote-type-sovereign", "rfq-quote-type-corporate") for item in submit["state"]))

    def test_ai_filled_fixed_income_rfqs_save_as_market_color(self):
        forms = {
            "sovereign": {"direction": "Buy", "bond_name": "LTN", "maturity": "LTN 01/01/2028", "quantity": 10},
            "corporate": {"direction": "Sell", "bond_type": "Banking Instruments", "security_text": "CDB Test Bank", "volume": 500000},
        }
        for kind, fields in forms.items():
            response = self.ai_dispatch(kind, fields)
            data = response[f"rfq-ai-result-{kind}"]["data"]
            populated = data["trades"][0]
            self.assertNotIn("quote_type", populated)
            state = self.fixed_income_state(kind, [populated])
            result = self.dispatch("rfq-notification-container.sendNotifications", {
                **state, "current-user.data": {"user": "AI Test User"}, "rfq-refresh.data": 0,
                f"rfq-submit-{kind}.n_clicks": 1,
            }, [f"rfq-submit-{kind}.n_clicks"])
            self.assertEqual(result["rfq-refresh"]["data"], 1, str(result))
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute('SELECT CLASS, TYPE, REQUESTER FROM RFQ').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(quote_type == "Mkt Color" and requester == "AI Test User" for _, quote_type, requester in rows))

    def test_invalid_catalog_numeric_and_ambiguous_values_are_not_applied(self):
        result = self.ai_dispatch("sovereign", {
            "direction": "Long", "maturity": "LTN 2099", "quantity": 1.5, "volume": -1,
        })
        fields = result["rfq-ai-result-sovereign"]["data"]["trades"][0]
        for name in ["direction", "maturity", "quantity", "volume"]:
            self.assertIsNone(fields[name])
        result = self.ai_dispatch("corporate", {"security": "TEST11", "volume": 100}, unresolved=["security"])
        self.assertIsNone(result["rfq-ai-result-corporate"]["data"]["trades"][0]["security"])
        self.assertEqual(result["rfq-ai-result-corporate"]["data"]["trades"][0]["volume"], 100)

    def test_model_and_instructions_are_loaded_from_sqlite_for_each_form(self):
        for kind in self.page.RFQ_FIELDS:
            self.ai_dispatch(kind, {})
            prompt = self.service.extract.call_args.kwargs["prompt"]
            self.assertEqual(prompt.model, f"sqlite-{kind}-model")
            self.assertEqual(prompt.instructions, f"Test-only {kind} extraction")
            self.assertEqual(prompt.key, f"fill_{kind}")

    def test_empty_and_oversized_input_do_not_call_the_model(self):
        for text in ["", "  ", "x" * 201, "😀" * 101]:
            service = Mock()
            with self.assertRaises(self.page.AIError):
                self.page.extract_rfq("sovereign", text, "Test User", service=service)
            service.extract.assert_not_called()
        self.assertEqual(self.audit_rows(), [])

    def test_compact_ai_input_and_counter_use_200_character_limit(self):
        components = self.page.rfq_components()
        for kind in self.page.RFQ_FIELDS:
            control = components[f"rfq-ai-text-{kind}"]
            self.assertEqual(control.maxLength, 200)
            self.assertEqual(control.rows, 2)
            self.assertEqual(components[f"rfq-ai-count-{kind}"].children, "0 / 200")
        counters = [item for item in self.app._callback_list if "rfq-ai-count-" in item["output"]]
        self.assertEqual(len(counters), 3)
        self.assertTrue(all(item.get("clientside_function") for item in counters))

    def test_quantities_and_volumes_display_separators_but_keep_numeric_values(self):
        components = self.page.rfq_components()
        fields = ["rfq-qty-sovereign", "rfq-volume-sovereign", "rfq-qty-corporate",
                  "rfq-volume-corporate", "rfq-volume-derivatives"]
        for field in fields:
            control = components[field]
            self.assertEqual(control.to_plotly_json()["type"], "NumberInput")
            self.assertEqual(control.thousandSeparator, ",")
            self.assertTrue(control.hideControls)
            self.assertEqual(control.allowDecimal, "-qty-" not in field)
        response = self.ai_dispatch("corporate", {
            "direction": "Buy", "bond_type": "Corporate Bonds", "security": "TEST11",
            "quantity": 1234, "volume": 1234567.89,
        })
        self.assertEqual(response["rfq-ai-result-corporate"]["data"]["trades"][0]["quantity"], 1234)
        self.assertEqual(response["rfq-ai-result-corporate"]["data"]["trades"][0]["volume"], 1234567.89)
        self.assertEqual(self.page._parse_quantity("1,234"), 1234)
        self.assertEqual(self.page._parse_number("1,234,567.89"), 1234567.89)

    def test_exactly_200_characters_are_accepted(self):
        for text in ["x" * 200, "😀" * 100]:
            response = self.ai_dispatch("corporate", {"direction": "Buy"}, text=text)
            self.service.extract.assert_called_once()
            self.assertTrue(response["rfq-ai-result-corporate"]["data"]["review"])
            self.assertEqual(self.service.extract.call_args.kwargs["payload"]["request_text"], text)

    def test_only_populated_results_request_review_highlight(self):
        for kind in self.page.RFQ_FIELDS:
            response = self.ai_dispatch(kind, {"direction": "Buy"})
            self.assertTrue(response[f"rfq-ai-result-{kind}"]["data"]["review"])
            response = self.ai_dispatch(kind, {})
            self.assertFalse(response[f"rfq-ai-result-{kind}"]["data"]["review"])
            response = self.ai_dispatch(kind, {"direction": "Buy"}, count=2, conflicts=True)
            self.assertFalse(response[f"rfq-ai-result-{kind}"]["data"]["review"])

    def audit_rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM AI_REQUEST_LOG ORDER BY LOG_ID")]

    def test_full_prompt_log_matches_the_exact_sdk_request(self):
        fields = self.page.rfq_empty_form("corporate", "Test User")
        fields.update(direction="Buy", bond_type="Corporate Bonds", security="TEST11", volume=500000)
        output = json.dumps({"asset_class": "corporate", "is_rfq_request": True, "request_count": 1,
                             "has_conflicts": False, "trades": [{"fields": fields, "unresolved_fields": []}]})
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            status="completed", output=[], output_text=output, model="returned-model", id="test-id",
            usage=SimpleNamespace(input_tokens=2400, output_tokens=180, total_tokens=2580),
        )
        service = self.page.OpenAIService(client=client, database_path=self.db)
        self.page.extract_rfq("corporate", "Comprar 500k TEST11", "Test User", service=service)
        row = self.audit_rows()[0]
        snapshot = json.loads(row["FULL_PROMPT"])
        self.assertEqual(snapshot, client.responses.create.call_args.kwargs)
        self.assertEqual(snapshot["instructions"], "Test-only corporate extraction")
        self.assertEqual(snapshot["model"], "sqlite-corporate-model")
        payload = json.loads(snapshot["input"])
        self.assertEqual(payload["request_text"], "Comprar 500k TEST11")
        self.assertEqual(payload["requester_username"], "Test User")
        contract = payload["form_contract"]
        self.assertEqual(contract["field_definitions"]["security"]["options"], self.page.load_corporate_security_options())
        self.assertEqual(snapshot["text"]["format"]["schema"], contract["output_schema"])
        self.assertEqual(row["MODEL_OUTPUT"], output)
        self.assertEqual((row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]), (2400, 180, 2580))
        self.assertNotIn("api_key", snapshot)

    def test_ai_input_errors_show_without_history_and_model_warnings_remain_logged(self):
        response = self.ai_dispatch("corporate", {}, text="")
        self.service.extract.assert_not_called()
        self.assertIn("Describe one complete RFQ", str(response["rfq-ai-status-corporate"]))
        self.assertEqual(self.audit_rows(), [])
        response = self.ai_dispatch("corporate", {"direction": "Buy"})
        shown = self.page.rfq_alert_text(response["rfq-ai-status-corporate"]["children"])
        self.assertIn("Please specify", shown)
        request = self.audit_rows()[-1]
        self.assertEqual(request["STATUS"], "invalid_response")
        self.assertEqual(request["ERROR_TYPE"], "MissingRFQDetails")
        self.assertEqual(request["ERROR_MESSAGE"], shown)

    def test_submission_validation_alerts_are_displayed(self):
        response = self.dispatch("rfq-notification-container.sendNotifications", {
            "rfq-submit-corporate.n_clicks": 1, "current-user.data": {"user": "Test User"},
        }, ["rfq-submit-corporate.n_clicks"])
        notifications = response["rfq-notification-container"]["sendNotifications"]
        self.assertTrue(notifications)
        self.assertTrue(all(item["color"] == "red" and item["action"] == "show" for item in notifications))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM RFQ").fetchone()[0], 0)
        self.assertEqual(self.audit_rows(), [])

    def test_page_initialization_and_manual_alerts_create_no_additional_log_tables(self):
        self.dispatch("rfq-notification-container.sendNotifications", {
            "rfq-submit-corporate.n_clicks": 1, "current-user.data": {"user": "Test User"},
        }, ["rfq-submit-corporate.n_clicks"])
        with closing(sqlite3.connect(self.db)) as conn:
            logs = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%_LOG'"
            )}
        self.assertEqual(logs, {"AI_REQUEST_LOG"})

    def test_submission_database_failure_displays_error_after_rollback(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TRIGGER test_reject_rfq BEFORE INSERT ON RFQ BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        try:
            response = self.dispatch("rfq-notification-container.sendNotifications", {
                "rfq-submit-corporate.n_clicks": 1, "current-user.data": {"user": "Test User"},
                **self.corporate_state([{"direction": "Buy", "bond_type": "Corporate Bonds",
                                         "security": "TEST11", "volume": 500000}]),
            }, ["rfq-submit-corporate.n_clicks"])
            message = response["rfq-notification-container"]["sendNotifications"][0]["message"]
            self.assertEqual(message, "Unable to create RFQs. No trades were saved. Try again.")
        finally:
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("DROP TRIGGER test_reject_rfq")

    def test_broker_validation_alert_is_displayed_and_quote_is_unchanged(self):
        original = json.dumps([{"broker name": "Test Broker", "broker_response": "100", "timestamp": "2026-01-01T10:00"}])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO RFQ (RFQ_ID, CLASS, STR_RESPONSE) VALUES (42, 'Sovereign Bond', ?)", (original,))
        response = self.dispatch("rfq-error-notification.message", {
            "rfq-grid.cellValueChanged": {"colId": self.page._counterparty_slug("Test Broker"),
                                          "data": {"rfq_id": 42}, "newValue": "100|99:99"},
            "rfq-active-switch.checked": True, "current-user.data": {"user": "Test User"},
        }, ["rfq-grid.cellValueChanged"])
        self.assertEqual(response["rfq-error-notification"]["action"], "show")
        self.assertEqual(response["rfq-error-notification"]["message"], "Invalid timestamp. Use HH:MM.")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT STR_RESPONSE FROM RFQ WHERE RFQ_ID=42").fetchone()[0], original)

    def test_ai_history_update_failure_does_not_hide_original_alert(self):
        with patch.object(self.page.AIRequestLog, "mark_invalid", side_effect=sqlite3.OperationalError("test failure")):
            response = self.ai_dispatch("corporate", {"direction": "Buy"})
        self.assertIn("Please specify", str(response["rfq-ai-status-corporate"]))

    def test_every_form_logs_text_user_time_and_original_json_without_submission(self):
        text = "  Comprar 1 milhão; referência João — test audit  "
        for kind in self.page.RFQ_FIELDS:
            response = self.ai_dispatch(kind, {"direction": "Buy"}, text=text)
            row = self.audit_rows()[-1]
            self.assertEqual(row["FEATURE"], "rfq")
            self.assertEqual(row["FORM_TYPE"], kind)
            self.assertEqual(row["USERNAME"], "AI Test User")
            self.assertEqual(row["PROMPT_TEXT"], text)
            self.assertEqual(row["STATUS"], "invalid_response")
            self.assertEqual(row["ERROR_TYPE"], "MissingRFQDetails")
            self.assertEqual(json.loads(row["MODEL_OUTPUT"]), self.service.extract.return_value.data)
            self.assertNotIn("RESPONSE_ID", row)
            self.assertNotIn("RAW_OUTPUT", row)
            self.assertNotIn("JSON_OUTPUT", row)
            self.assertEqual(row["MODEL"], "test-model")
            self.assertEqual(datetime.fromisoformat(row["DT_STARTED"]).utcoffset(), timezone.utc.utcoffset(None))
            self.assertGreaterEqual(datetime.fromisoformat(row["DT_FINISHED"]), datetime.fromisoformat(row["DT_STARTED"]))
            self.assertEqual(response[f"rfq-ai-result-{kind}"]["data"]["metadata"]["log_id"], row["LOG_ID"])
        self.assertEqual(len(self.audit_rows()), 3)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM RFQ").fetchone()[0], 0)

    def test_identical_requests_create_separate_audit_rows(self):
        self.ai_dispatch("corporate", {"direction": "Buy"})
        self.ai_dispatch("corporate", {"direction": "Buy"})
        first, second = self.audit_rows()
        self.assertNotEqual(first["LOG_ID"], second["LOG_ID"])
        self.assertEqual(first["PROMPT_TEXT"], second["PROMPT_TEXT"])

    def test_cancel_does_not_remove_audit_record(self):
        self.ai_dispatch("sovereign", {"direction": "Buy"})
        result = self.dispatch("rfq-form-sovereign.opened", {
            "rfq-cancel-sovereign.n_clicks": 1, "rfq-tabs.value": "sovereign",
            "current-user.data": {"user": "AI Test User"},
        }, ["rfq-cancel-sovereign.n_clicks"])
        self.assertFalse(result["rfq-form-sovereign"]["opened"])
        self.assertEqual(len(self.audit_rows()), 1)

    def test_rejected_conflicting_trade_keeps_original_model_json(self):
        response = self.ai_dispatch("corporate", {
            "direction": "Buy", "security": "TEST11", "requester": "Model supplied name",
        }, count=2, conflicts=True)
        self.assertEqual(response["rfq-ai-result-corporate"]["data"]["trades"], [])
        row = self.audit_rows()[0]
        original = json.loads(row["MODEL_OUTPUT"])
        self.assertEqual(original["request_count"], 2)
        self.assertEqual(original["trades"][0]["fields"]["security"], "TEST11")
        self.assertEqual(original["trades"][0]["fields"]["requester"], "Model supplied name")
        self.assertEqual(row["USERNAME"], "AI Test User")

    def test_multiple_requests_warning_is_visible_in_the_ai_request_log(self):
        response = self.ai_dispatch("sovereign", {}, count=2, conflicts=True,
                                    text="compro b50 500k compro b40",
                                    usage={"input_tokens": 1890, "output_tokens": 420, "total_tokens": 2310})
        shown = response["rfq-ai-status-sovereign"]["children"]["props"]["children"]
        request = self.audit_rows()[0]
        self.assertIn("Conflicting details were found within a trade", shown)
        self.assertEqual(request["STATUS"], "invalid_response")
        self.assertEqual(request["ERROR_TYPE"], "ConflictingRFQs")
        self.assertEqual(request["ERROR_MESSAGE"], shown)
        self.assertEqual(json.loads(request["MODEL_OUTPUT"]), self.service.extract.return_value.data)
        self.assertIsNotNone(request["FULL_PROMPT"])
        self.assertEqual((request["INPUT_TOKENS"], request["OUTPUT_TOKENS"], request["TOTAL_TOKENS"]), (1890, 420, 2310))

    def test_attempt_is_committed_before_model_call_and_transport_failure_logged(self):
        service, client = self.sdk_service({})

        class APITimeoutError(TimeoutError):
            pass

        def fail_request(**kwargs):
            row = self.audit_rows()[0]
            self.assertEqual(row["STATUS"], "running")
            self.assertIsNone(row["DT_FINISHED"])
            raise APITimeoutError("Sensitive transport detail")

        client.responses.create.side_effect = fail_request
        with self.assertRaises(self.page.AIError):
            self.page.extract_rfq("sovereign", "Buy LTN", "Test User", service=service)
        row = self.audit_rows()[0]
        self.assertEqual(row["STATUS"], "failed")
        self.assertEqual(row["ERROR_TYPE"], "APITimeoutError")
        self.assertEqual(row["MODEL"], "sqlite-sovereign-model")
        self.assertIsNone(row["MODEL_OUTPUT"])
        self.assertEqual((row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]), (None, None, None))
        self.assertIsNotNone(row["DT_FINISHED"])

    def test_malformed_json_is_preserved_in_single_output_column(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            status="completed", output=[], output_text='{ "fields": unfinished',
            model="returned-model", id="broken-response",
            usage=SimpleNamespace(input_tokens=500, output_tokens=90, total_tokens=590),
        )
        service = self.page.OpenAIService(client=client, database_path=self.db)
        with self.assertRaises(self.page.AIError):
            self.page.extract_rfq("corporate", "Buy TEST11", "Test User", service=service)
        row = self.audit_rows()[0]
        self.assertEqual(row["STATUS"], "invalid_response")
        self.assertEqual(row["ERROR_TYPE"], "InvalidJSON")
        self.assertEqual(row["MODEL_OUTPUT"], '{ "fields": unfinished')
        self.assertEqual(row["MODEL"], "returned-model")
        self.assertEqual((row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]), (500, 90, 590))

    def test_incomplete_model_response_still_logs_token_usage(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            status="incomplete", output=[], output_text='{"fields":', model="returned-model", id="partial",
            usage=SimpleNamespace(input_tokens=321, output_tokens=4000, total_tokens=4321),
        )
        with self.assertRaises(self.page.AIError):
            self.page.extract_rfq("corporate", "Buy 500k TEST11", "Test User", service=self.page.OpenAIService(client=client, database_path=self.db))
        row = self.audit_rows()[0]
        self.assertEqual(row["STATUS"], "failed")
        self.assertEqual(row["ERROR_TYPE"], "IncompleteResponse")
        self.assertEqual((row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]), (321, 4000, 4321))

    def test_json_rejected_by_form_is_logged_before_error_is_returned(self):
        raw = '{\n  "unexpected": true\n}'
        service, _ = self.sdk_service({"unexpected": True}, raw=raw,
                                      usage={"input_tokens": 1100, "output_tokens": 95, "total_tokens": 1195})
        with self.assertRaises(self.page.AIError):
            self.page.extract_rfq("corporate", "Buy TEST11", "Test User", service=service)
        row = self.audit_rows()[0]
        self.assertEqual(row["STATUS"], "invalid_response")
        self.assertEqual(row["MODEL_OUTPUT"], raw)
        self.assertEqual((row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]), (1100, 95, 1195))

    def test_no_model_call_if_request_cannot_be_logged(self):
        service, client = self.sdk_service({})
        with patch.object(self.page.AIRequestLog, "begin", side_effect=sqlite3.OperationalError("test database unavailable")):
            with self.assertRaises(sqlite3.Error):
                self.page.extract_rfq("sovereign", "Buy LTN", "Test User", service=service)
        client.responses.create.assert_not_called()

    def test_manual_maturity_change_does_not_reapply_old_ai_result(self):
        result = self.maturity_dispatch("LFT", "LTN 01/01/2028")
        self.assertIsNone(result["value"])

    def test_new_modals_start_with_blank_trade_controls(self):
        for kind in self.page.RFQ_FIELDS:
            values = self.page.rfq_empty_form(kind, "Signed In Username")
            self.assertEqual(values.pop("requester"), "Signed In Username")
            for field, value in values.items():
                if field in self.page.SOLVE_FIELDS:
                    self.assertFalse(value)
                else:
                    self.assertIsNone(value, f"{kind}.{field} must open blank")

    def test_add_and_remove_blocks_preserve_live_edits_and_stable_ids(self):
        trades = [
            {"direction": "Buy", "bond_name": "LTN", "maturity": "LTN 01/01/2028", "quantity": 1250},
            {"direction": "Sell", "bond_name": "LFT", "maturity": "LFT 01/03/2029", "volume": 500000},
        ]
        blocks = self.manage_trades(trades, changed="rfq-sovereign-add.n_clicks", **{"rfq-sovereign-add.n_clicks": 1})
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0]["props"]["id"]["trade"], "test-0")
        self.assertEqual(self.block_fields(blocks[0])["quantity"]["value"], 1250)
        self.assertEqual(self.block_fields(blocks[1])["volume"]["value"], 500000)
        self.assertIsNone(self.block_fields(blocks[2])["direction"]["value"])
        remove_id = {"type": "rfq-sovereign-remove", "trade": "test-0"}
        remove_pattern = {"type": "rfq-sovereign-remove", "trade": ["ALL"]}
        blocks = self.manage_trades(trades, changed=self.pattern_key(remove_id, "n_clicks"), **{
            self.pattern_key(remove_pattern, "n_clicks"): [1, 0],
        })
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["props"]["id"]["trade"], "test-1")
        self.assertEqual(self.block_fields(blocks[0])["maturity"]["value"], "LFT 01/03/2029")
        self.assertEqual(blocks[0]["props"]["children"][0]["props"]["children"][0]["props"]["children"], "Trade 1")

    def test_deleting_final_block_leaves_one_fresh_trade(self):
        remove_id = {"type": "rfq-sovereign-remove", "trade": "test-0"}
        remove_pattern = {"type": "rfq-sovereign-remove", "trade": ["ALL"]}
        blocks = self.manage_trades([{"direction": "Buy", "quantity": 100}],
                                   changed=self.pattern_key(remove_id, "n_clicks"), **{
                                       self.pattern_key(remove_pattern, "n_clicks"): [1],
                                   })
        self.assertEqual(len(blocks), 1)
        fields = self.block_fields(blocks[0])
        self.assertIsNone(fields["quantity"]["value"])
        self.assertIsNone(fields["direction"]["value"])
        self.assertEqual(fields["requester"]["value"], "AI Test User")

    def test_mounting_remove_buttons_does_not_remove_or_reset_trades(self):
        from dash.exceptions import PreventUpdate

        for opened in [True, False]:
            for trigger in [None, {"type": "rfq-sovereign-remove", "trade": "test-0"},
                            {"type": "rfq-sovereign-remove", "trade": ["ALL"]}]:
                context = SimpleNamespace(triggered_id=trigger, triggered_prop_ids={})
                with patch.object(self.page, "ctx", context), self.assertRaises(PreventUpdate):
                    self.page.manage_sovereign_trades(
                        1, [0, 0], opened, self.sovereign_assets, None, 0,
                        {"user": "Test User"}, [], [],
                    )

    def test_opening_and_ai_apply_reset_every_existing_trade(self):
        old = [{"direction": "Buy", "quantity": 100, "yield_limit": 12}, {"direction": "Sell", "volume": 200000}]
        for changed in ["rfq-form-sovereign.opened", "rfq-ai-apply-sovereign.n_clicks"]:
            blocks = self.manage_trades(old, changed=changed, **{
                "rfq-ai-result-sovereign.data": {"trades": old}, "rfq-ai-apply-sovereign.n_clicks": 1,
            })
            self.assertEqual(len(blocks), 1)
            for name, props in self.block_fields(blocks[0]).items():
                if name != "requester":
                    self.assertIsNone(props["value"])

    def test_ai_populates_separate_trades_and_logs_one_complete_batch(self):
        trades = [
            {"direction": "Buy", "bond_name": "LTN", "maturity": "LTN 01/01/2028", "volume": 500000},
            {"direction": "Sell", "bond_name": "LFT", "maturity": "LFT 01/03/2029", "quantity": 25, "yield_limit": 12.5},
        ]
        response = self.ai_dispatch("sovereign", {}, trades=trades, text="buy 500k LTN 2028; sell 25q LFT 2029 12.5%",
                                    usage={"input_tokens": 2000, "output_tokens": 500, "total_tokens": 2500})
        self.service.extract.assert_called_once()
        draft = response["rfq-ai-result-sovereign"]["data"]
        self.assertEqual(len(draft["trades"]), 2)
        blocks = self.manage_trades(changed="rfq-ai-result-sovereign.data", **{"rfq-ai-result-sovereign.data": draft})
        first, second = map(self.block_fields, blocks)
        self.assertEqual(first["volume"]["value"], 500000)
        self.assertIsNone(first["quantity"]["value"])
        self.assertIsNone(first["yield_limit"]["value"])
        self.assertEqual(second["quantity"]["value"], 25)
        self.assertIsNone(second["volume"]["value"])
        self.assertEqual(self.maturity_dispatch("LTN", first["maturity"]["value"], trade_id="first")["value"], "LTN 01/01/2028")
        self.assertEqual(self.maturity_dispatch("LFT", second["maturity"]["value"], trade_id="second")["value"], "LFT 01/03/2029")
        audit = self.audit_rows()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["STATUS"], "completed")
        self.assertEqual(json.loads(audit[0]["MODEL_OUTPUT"])["request_count"], 2)
        self.assertEqual(audit[0]["TOTAL_TOKENS"], 2500)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM RFQ").fetchone()[0], 0)
        submitted = self.dispatch("rfq-notification-container.sendNotifications", {
            **self.sovereign_state(draft["trades"]), "current-user.data": {"user": "AI Test User"},
            "rfq-submit-sovereign.n_clicks": 1, "rfq-refresh.data": 0,
        }, ["rfq-submit-sovereign.n_clicks"])
        self.assertEqual(submitted["rfq-refresh"]["data"], 1)
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT SEC_NAME, DIRECTION, QTY, VOLUME, REQUESTER, TYPE FROM RFQ ORDER BY RFQ_ID").fetchall()
        self.assertEqual(rows, [
            ("LTN 01/01/2028", "Buy", None, 500000, "AI Test User", "Mkt Color"),
            ("LFT 01/03/2029", "Sell", 25, None, "AI Test User", "Mkt Color"),
        ])

    def test_partial_batch_missing_details_are_reported_per_trade(self):
        response = self.ai_dispatch("sovereign", {}, trades=[
            {"direction": "Buy", "bond_name": "LTN", "maturity": "LTN 01/01/2028", "volume": 500000},
            {"direction": "Sell", "bond_name": "LFT", "maturity": "LFT 01/03/2029"},
        ])
        draft = response["rfq-ai-result-sovereign"]["data"]
        self.assertEqual(len(draft["trades"]), 2)
        self.assertEqual(draft["trades"][0]["volume"], 500000)
        self.assertIsNone(draft["trades"][1]["volume"])
        shown = self.page.rfq_alert_text(response["rfq-ai-status-sovereign"]["children"])
        self.assertIn("Trade 2: quantity or volume", shown)
        self.assertNotIn("Trade 1:", shown)
        self.assertEqual(self.audit_rows()[0]["ERROR_TYPE"], "MissingRFQDetails")

    def test_corporate_blocks_preserve_edits_and_independent_security_modes(self):
        trades = [
            {"direction": "Buy", "bond_type": "Corporate Bonds", "security": "TEST11", "volume": 500000},
            {"direction": "Sell", "bond_type": "Banking Instruments", "security_text": "CDB Test Bank", "quantity": 25},
        ]
        blocks = self.manage_trades(trades, asset_class="corporate", changed="rfq-corporate-add.n_clicks", **{
            "rfq-corporate-add.n_clicks": 1,
        })
        self.assertEqual(len(blocks), 3)
        first, second, blank = map(self.block_fields, blocks)
        self.assertEqual(first["security"]["value"], "TEST11")
        self.assertEqual(first["security"]["options"], self.corporate_options)
        self.assertEqual(second["security_text"]["value"], "CDB Test Bank")
        self.assertEqual(second["quantity"]["value"], 25)
        self.assertIsNone(blank["direction"]["value"])
        bond_pattern = {"type": "rfq-corporate-field", "field": "bond_type", "trade": ["MATCH"]}
        for trade_id, category, text_display, dropdown_display in [
            ("test-0", "Corporate Bonds", "none", "block"),
            ("test-1", "Banking Instruments", "block", "none"),
        ]:
            response = self.dispatch('"type":"rfq-corporate-security-text-wrap"', {
                self.pattern_key(bond_pattern, "value"): category,
            }, [self.pattern_key(self.page.corporate_field_id("bond_type", trade_id), "value")], match_trade=trade_id)
            for mode, display in [("text", text_display), ("dropdown", dropdown_display)]:
                component_id = {"type": f"rfq-corporate-security-{mode}-wrap", "trade": trade_id}
                key = self.pattern_key(component_id, "style").rsplit(".", 1)[0]
                self.assertEqual(response[key]["style"], {"display": display})
        remove_id = {"type": "rfq-corporate-remove", "trade": "test-0"}
        remove_pattern = {"type": "rfq-corporate-remove", "trade": ["ALL"]}
        blocks = self.manage_trades(trades, asset_class="corporate", changed=self.pattern_key(remove_id, "n_clicks"), **{
            self.pattern_key(remove_pattern, "n_clicks"): [1, 0],
        })
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["props"]["id"]["trade"], "test-1")
        self.assertEqual(self.block_fields(blocks[0])["security_text"]["value"], "CDB Test Bank")
        # Deleting the final block leaves the same fresh starting point as sovereigns.
        blocks = self.manage_trades([trades[0]], asset_class="corporate", changed=self.pattern_key(remove_id, "n_clicks"), **{
            self.pattern_key(remove_pattern, "n_clicks"): [1],
        })
        self.assertEqual(len(blocks), 1)
        self.assertIsNone(self.block_fields(blocks[0])["security"]["value"])

    def test_corporate_ai_batch_fills_mixed_categories_and_saves_after_review(self):
        response = self.ai_dispatch("corporate", {}, trades=[
            {"direction": "Buy", "bond_type": "Corporate Bonds", "security": "TEST11", "volume": 500000,
             "security_text": "Hidden stale description", "requester": "Forged name"},
            {"direction": "Sell", "bond_type": "Banking Instruments", "security_text": "CDB Test Bank", "quantity": 25,
             "security": "TEST11", "yield_limit": 13},
        ], usage={"input_tokens": 1500, "output_tokens": 400, "total_tokens": 1900})
        self.service.extract.assert_called_once()
        draft = response["rfq-ai-result-corporate"]["data"]
        # Reproduce the browser's batched Apply/result notifications, in Apply-first order.
        rendered = self.dispatch("rfq-corporate-trades.children", {
            "rfq-form-corporate.opened": True, "current-user.data": {"user": "AI Test User"},
            "rfq-ai-apply-corporate.n_clicks": 1, "rfq-ai-result-corporate.data": draft,
            **self.corporate_state([{}]),
        }, ["rfq-ai-apply-corporate.n_clicks", "rfq-ai-result-corporate.data"])
        blocks = rendered["rfq-corporate-trades"]["children"]
        first, second = map(self.block_fields, blocks)
        self.assertEqual(first["security"]["value"], "TEST11")
        self.assertIsNone(first["security_text"]["value"])
        self.assertEqual(second["security_text"]["value"], "CDB Test Bank")
        self.assertIsNone(second["security"]["value"])
        self.assertEqual(first["requester"]["value"], "AI Test User")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM RFQ").fetchone()[0], 0)
        audit = self.audit_rows()[0]
        self.assertEqual(audit["STATUS"], "completed")
        self.assertEqual(audit["TOTAL_TOKENS"], 1900)
        self.assertEqual(json.loads(audit["MODEL_OUTPUT"])["trades"][0]["fields"]["requester"], "Forged name")
        state = {name: props["value"] for name, props in first.items()}
        state2 = {name: props["value"] for name, props in second.items()}
        submitted = self.dispatch("rfq-notification-container.sendNotifications", {
            **self.corporate_state([state, state2]), "current-user.data": {"user": "AI Test User"},
            "rfq-submit-corporate.n_clicks": 1, "rfq-refresh.data": 0,
        }, ["rfq-submit-corporate.n_clicks"])
        self.assertEqual(submitted["rfq-refresh"]["data"], 1)
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute('SELECT CLASS, SEC_NAME, DT_MATURITY, QTY, VOLUME, "LIMIT", REQUESTER, TYPE FROM RFQ ORDER BY RFQ_ID').fetchall()
        self.assertEqual(rows, [
            ("Corporate Bond", "TEST11", "2030-01-01", None, 500000, None, "AI Test User", "Mkt Color"),
            ("Corporate Bond", "CDB Test Bank", None, 25, None, 13, "AI Test User", "Mkt Color"),
        ])

    def test_corporate_catalog_snapshot_is_shared_by_all_blocks_and_ai(self):
        with patch.object(self.page, "load_corporate_security_options", wraps=self.page.load_corporate_security_options) as loader:
            response = self.dispatch("rfq-corporate-options.data", {"rfq-form-corporate.opened": True}, ["rfq-form-corporate.opened"])
            options = response["rfq-corporate-options"]["data"]
            self.assertEqual(options, self.corporate_options)
            self.manage_trades([{"security": "TEST11"}], asset_class="corporate", changed="rfq-corporate-add.n_clicks", **{
                "rfq-corporate-options.data": options, "rfq-corporate-add.n_clicks": 1,
            })
            self.ai_dispatch("corporate", {"direction": "Buy", "bond_type": "Corporate Bonds", "security": "TEST11", "quantity": 10})
            contract = self.service.extract.call_args.kwargs["payload"]["form_contract"]
            self.assertEqual(contract["field_definitions"]["security"]["options"], options)
            loader.assert_called_once()
            closed = self.dispatch("rfq-corporate-options.data", {"rfq-form-corporate.opened": False}, ["rfq-form-corporate.opened"])
            self.assertEqual(closed["rfq-corporate-options"]["data"], [])

    def test_corporate_ai_missing_details_and_repeated_runs_do_not_mix_trades(self):
        response = self.ai_dispatch("corporate", {}, trades=[
            {"direction": "Buy", "bond_type": "Corporate Bonds", "security": "TEST11", "quantity": 10, "yield_limit": 12},
            {"direction": "Sell", "bond_type": "Banking Instruments", "volume": 200000},
        ])
        self.assertIn("Trade 2: banking instrument description", self.page.rfq_alert_text(response["rfq-ai-status-corporate"]["children"]))
        self.assertEqual(self.audit_rows()[0]["ERROR_TYPE"], "MissingRFQDetails")
        old = response["rfq-ai-result-corporate"]["data"]["trades"]
        for changed in ["rfq-form-corporate.opened", "rfq-ai-apply-corporate.n_clicks"]:
            blocks = self.manage_trades(old, asset_class="corporate", changed=changed, **{"rfq-ai-result-corporate.data": {"trades": old}})
            self.assertEqual(len(blocks), 1)
            self.assertIsNone(self.block_fields(blocks[0])["quantity"]["value"])
        response = self.ai_dispatch("corporate", {"direction": "Sell", "bond_type": "Corporate Bonds", "security": "TEST11", "volume": 500000})
        blocks = self.manage_trades(old, asset_class="corporate", changed="rfq-ai-result-corporate.data", **{
            "rfq-ai-result-corporate.data": response["rfq-ai-result-corporate"]["data"],
        })
        self.assertEqual(len(blocks), 1)
        fields = self.block_fields(blocks[0])
        self.assertEqual(fields["volume"]["value"], 500000)
        self.assertIsNone(fields["quantity"]["value"])
        self.assertIsNone(fields["yield_limit"]["value"])
        self.assertNotIn("previous_response", self.service.extract.call_args.kwargs["payload"])

    def test_invalid_corporate_trade_blocks_the_batch_and_shows_trade_number(self):
        valid = {"direction": "Buy", "bond_type": "Corporate Bonds", "security": "TEST11", "quantity": 10}
        for invalid in [
            {"direction": "Sell", "bond_type": "Corporate Bonds", "security": "UNKNOWN", "volume": 200000},
            {"direction": "Sell", "bond_type": "Banking Instruments", "security_text": "  ", "volume": 200000},
            {"direction": "Sell", "bond_type": "Banking Instruments", "security_text": "CDB Test Bank"},
        ]:
            response = self.dispatch("rfq-notification-container.sendNotifications", {
                **self.corporate_state([valid, invalid]), "current-user.data": {"user": "Test User"},
                "rfq-submit-corporate.n_clicks": 1,
            }, ["rfq-submit-corporate.n_clicks"])
            self.assertNotIn("rfq-refresh", response)
            self.assertTrue(all(item["message"].startswith("Trade 2:") for item in response["rfq-notification-container"]["sendNotifications"]))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM RFQ").fetchone()[0], 0)

    def test_corporate_batch_failure_on_later_trade_rolls_back_every_record(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TRIGGER reject_corporate_sell BEFORE INSERT ON RFQ WHEN NEW.DIRECTION='Sell' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        try:
            response = self.dispatch("rfq-notification-container.sendNotifications", {
                **self.corporate_state([
                    {"direction": "Buy", "bond_type": "Corporate Bonds", "security": "TEST11", "quantity": 10},
                    {"direction": "Sell", "bond_type": "Banking Instruments", "security_text": "CDB Test Bank", "volume": 200000},
                ]), "current-user.data": {"user": "Test User"}, "rfq-submit-corporate.n_clicks": 1,
            }, ["rfq-submit-corporate.n_clicks"])
            self.assertNotIn("rfq-refresh", response)
            self.assertIn("No trades were saved", response["rfq-notification-container"]["sendNotifications"][0]["message"])
            with closing(sqlite3.connect(self.db)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM RFQ").fetchone()[0], 0)
        finally:
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("DROP TRIGGER reject_corporate_sell")

    def test_new_ai_batch_replaces_all_old_blocks_and_optional_values(self):
        first = self.ai_dispatch("sovereign", {}, trades=[{"direction": "Buy", "quantity": 100, "yield_limit": 12}, {"direction": "Sell"}])
        second = self.ai_dispatch("sovereign", {"direction": "Sell", "volume": 1000000})
        draft = second["rfq-ai-result-sovereign"]["data"]
        blocks = self.manage_trades(first["rfq-ai-result-sovereign"]["data"]["trades"],
                                   changed="rfq-ai-result-sovereign.data", **{"rfq-ai-result-sovereign.data": draft})
        self.assertEqual(len(blocks), 1)
        fields = self.block_fields(blocks[0])
        self.assertEqual(fields["volume"]["value"], 1000000)
        self.assertIsNone(fields["quantity"]["value"])
        self.assertIsNone(fields["yield_limit"]["value"])
        payload = self.service.extract.call_args.kwargs["payload"]
        self.assertEqual(set(payload), {"request_text", "requester_username", "form_contract"})

    def test_completed_ai_result_wins_over_batched_apply_and_mount_events(self):
        trades = [
            {"direction": "Buy", "bond_name": "LTN", "maturity": "LTN 01/01/2028", "volume": 500000},
            {"direction": "Sell", "bond_name": "LFT", "maturity": "LFT 01/03/2029", "quantity": 25},
        ]
        response = self.ai_dispatch("sovereign", {}, trades=trades)
        draft = response["rfq-ai-result-sovereign"]["data"]
        remove_id = {"type": "rfq-sovereign-remove", "trade": "test-0"}
        remove_pattern = {"type": "rfq-sovereign-remove", "trade": ["ALL"]}
        for changed in [
            ["rfq-ai-apply-sovereign.n_clicks", "rfq-ai-result-sovereign.data"],
            ["rfq-ai-result-sovereign.data", "rfq-ai-apply-sovereign.n_clicks"],
            [self.pattern_key(remove_id, "n_clicks"), "rfq-ai-result-sovereign.data"],
        ]:
            with self.subTest(changed=changed):
                result = self.dispatch("rfq-sovereign-trades.children", {
                    "rfq-form-sovereign.opened": True,
                    "current-user.data": {"user": "AI Test User"},
                    "rfq-ai-apply-sovereign.n_clicks": 1,
                    "rfq-ai-result-sovereign.data": draft,
                    self.pattern_key(remove_pattern, "n_clicks"): [0],
                    **self.sovereign_state([{}]),
                }, changed)
                blocks = result["rfq-sovereign-trades"]["children"]
                self.assertEqual(len(blocks), 2)
                self.assertEqual(self.block_fields(blocks[0])["volume"]["value"], 500000)
                self.assertEqual(self.block_fields(blocks[1])["quantity"]["value"], 25)

    def test_invalid_trade_prevents_entire_batch_and_shows_numbered_error(self):
        for second in [
            {"direction": "Sell", "bond_name": "LFT", "maturity": "LFT 01/03/2029"},
            {"direction": "Sell", "bond_name": "LFT", "maturity": "LTN 01/01/2028", "quantity": 10},
        ]:
            response = self.dispatch("rfq-notification-container.sendNotifications", {
                **self.sovereign_state([
                    {"direction": "Buy", "bond_name": "LTN", "maturity": "LTN 01/01/2028", "quantity": 10}, second,
                ]), "current-user.data": {"user": "Test User"}, "rfq-submit-sovereign.n_clicks": 1,
            }, ["rfq-submit-sovereign.n_clicks"])
            self.assertNotIn("rfq-refresh", response)
            alerts = response["rfq-notification-container"]["sendNotifications"]
            self.assertTrue(all(alert["message"].startswith("Trade 2:") for alert in alerts))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM RFQ").fetchone()[0], 0)

    def test_database_failure_on_second_trade_rolls_back_first(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TRIGGER test_reject_second BEFORE INSERT ON RFQ WHEN NEW.DIRECTION = 'Sell' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        try:
            response = self.dispatch("rfq-notification-container.sendNotifications", {
                **self.sovereign_state([
                    {"direction": "Buy", "bond_name": "LTN", "maturity": "LTN 01/01/2028", "quantity": 10},
                    {"direction": "Sell", "bond_name": "LFT", "maturity": "LFT 01/03/2029", "volume": 500000},
                ]), "current-user.data": {"user": "Test User"}, "rfq-submit-sovereign.n_clicks": 1,
            }, ["rfq-submit-sovereign.n_clicks"])
            self.assertNotIn("rfq-refresh", response)
            message = response["rfq-notification-container"]["sendNotifications"][0]["message"]
            self.assertIn("No trades were saved", message)
            with closing(sqlite3.connect(self.db)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM RFQ").fetchone()[0], 0)
        finally:
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("DROP TRIGGER test_reject_second")

    def test_inconsistent_batch_json_is_rejected_and_logged(self):
        service, _ = self.sdk_service({
            "asset_class": "sovereign", "is_rfq_request": True, "request_count": 2,
            "has_conflicts": False, "trades": [],
        })
        with self.assertRaises(self.page.AIError):
            self.page.extract_rfq("sovereign", "Buy LTN and sell LFT", "Test User", service=service)
        self.assertEqual(self.audit_rows()[0]["STATUS"], "invalid_response")

    def test_unrelated_sovereign_request_does_not_populate_any_trades(self):
        response = self.ai_dispatch("sovereign", {}, relevant=False, count=0)
        draft = response["rfq-ai-result-sovereign"]["data"]
        self.assertEqual(draft["trades"], [])
        self.assertFalse(draft["review"])
        self.assertEqual(self.audit_rows()[0]["ERROR_TYPE"], "UnrelatedRequest")

    def test_running_state_and_immediate_client_reset_are_registered(self):
        ai_callbacks = [item for item in self.app._callback_list if "rfq-ai-status-" in item["output"] and item.get("running")]
        resets = [item for item in self.app._callback_list if "rfq-ai-status-" in item["output"] and item.get("clientside_function")]
        self.assertEqual(len(ai_callbacks), 3)
        self.assertEqual(len(resets), 3)
        for item in resets:
            self.assertTrue(any(inp["id"].startswith("rfq-form-") and inp["property"] == "opened" for inp in item["inputs"]))
            self.assertIn("rfq-ai-text-", item["output"])
        for item in ai_callbacks:
            self.assertEqual(len(item["inputs"]), 1)
            self.assertTrue(item["inputs"][0]["id"].startswith("rfq-ai-apply-"))
        for item in ai_callbacks:
            running = item["running"]["running"]
            submit = next(key for key in running if key.startswith("rfq-submit-"))
            busy = next(key for key in running if key.endswith("aria-busy"))
            self.assertTrue(running[submit])
            self.assertEqual(running[busy], "true")
            self.assertFalse(any(key.endswith("className") for key in running))
            self.assertFalse(item["running"]["runningOff"][submit])
        presentations = [item for item in self.app._callback_list if "rfq-ai-stage-" in item["output"]]
        self.assertEqual(len(presentations), 3)
        self.assertTrue(all(item.get("clientside_function") for item in presentations))

    def test_rfq_ui_callbacks_keep_stable_names_when_form_settings_change(self):
        components = self.page.rfq_components()
        expected_names = {"countText", "presentAIState", "resetAIForm"}
        for kind in self.page.RFQ_FIELDS:
            config_id = f"rfq-ai-ui-config-{kind}"
            config = components[config_id].data
            callbacks = [item for item in self.app._callback_list
                         if (item.get("clientside_function") or {}).get("namespace") == "formControls"
                         and any(state["id"] == config_id for state in item["state"])]
            self.assertEqual({item["clientside_function"]["function_name"] for item in callbacks}, expected_names)
            self.assertEqual(config["counter"]["limit"], 200)
            reset = next(item for item in callbacks if item["clientside_function"]["function_name"] == "resetAIForm")
            outputs = self.app.callback_map[reset["output"]]["output"]
            self.assertEqual(len(config["reset"]["values"]) + 1, len(outputs))
            self.assertEqual(config["reset"]["values"][-2:], [None, ""])
            self.assertEqual(config["reset"]["openedProp"], f"rfq-form-{kind}.opened")
            self.assertEqual(config["reset"]["applyProp"], f"rfq-ai-apply-{kind}.n_clicks")
            if kind in self.page.MULTI_TRADE_ASSETS:
                self.assertEqual(config["reset"]["values"], [None, ""])
            self.assertIn("rfq-fields-review", config["stages"]["review"][1])

    def test_named_rfq_client_functions_load_before_dash_starts(self):
        response = self.client.get("/assets/aiFormControls.js")
        self.assertEqual(response.status_code, 200)
        source = response.get_data(as_text=True)
        response.close()
        for name in ("countText", "presentAIState", "resetAIForm"):
            self.assertIn(f"controls.{name} = function", source)
        index = self.client.get("/").get_data(as_text=True)
        self.assertLess(index.index("/assets/aiFormControls.js"), index.index("/dash-renderer/"))

    def test_submissions_use_session_requester_for_all_asset_classes(self):
        base = {"current-user.data": {"user": "Signed In Username"}, "rfq-refresh.data": 0}
        forms = {
            "sovereign": {"rfq-direction-sovereign.value": "Buy", "rfq-quote-type-sovereign.value": "Trade",
                          "rfq-bond-name-sovereign.value": "LTN", "rfq-maturity-sovereign.value": "LTN 01/01/2028", "rfq-qty-sovereign.value": 10},
            "corporate": {"rfq-direction-corporate.value": "Sell", "rfq-quote-type-corporate.value": "Trade",
                          "rfq-bond-type-corporate.value": "Corporate Bonds", "rfq-corp-security-dropdown.value": "TEST11", "rfq-volume-corporate.value": 5000},
            "derivatives": {"rfq-direction-derivatives.value": "Buy", "rfq-quote-type-derivatives.value": "Trade", "rfq-bond-name-derivatives.value": "DOL",
                            "rfq-maturity-derivatives.value": 3, "rfq-maturity-unit-derivatives.value": "M", "rfq-volume-derivatives.value": 1000000,
                            "rfq-limit-derivatives.value": 0.05, "rfq-strategy-derivatives.value": "Call", "rfq-strike-solve-derivatives.value": ["solve"],
                            "rfq-barrier-type-derivatives.value": "No Barrier"},
        }
        for kind, values in forms.items():
            if kind == "sovereign":
                values = self.sovereign_state([{
                    "direction": "Buy", "bond_name": "LTN", "maturity": "LTN 01/01/2028",
                    "quantity": 10, "requester": "Forged Requester",
                }])
            elif kind == "corporate":
                values = self.corporate_state([{
                    "direction": "Sell", "bond_type": "Corporate Bonds", "security": "TEST11",
                    "volume": 5000, "requester": "Forged Requester",
                }])
            result = self.dispatch("rfq-notification-container.sendNotifications", {
                **base, **values, f"rfq-submit-{kind}.n_clicks": 1,
                f"rfq-requester-{kind}.value": "Forged Requester",
            }, [f"rfq-submit-{kind}.n_clicks"])
            self.assertEqual(result["rfq-refresh"]["data"], 1, str(result))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            rows = conn.execute("SELECT CLASS, REQUESTER, USR_INPUT, TYPE FROM RFQ").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(requester == "Signed In Username" and trader == requester for _, requester, trader, _ in rows))
        for asset_class, _, _, quote_type in rows:
            self.assertEqual(quote_type, "Trade" if asset_class == "Derivatives" else "Mkt Color")


if __name__ == "__main__":
    unittest.main()
