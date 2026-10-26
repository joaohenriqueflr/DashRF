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

from services.openai_client import StructuredResult
from services.prompts import PromptStore

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
        cls.app = Dash("rfq_ai_tests", suppress_callback_exceptions=True)
        cls.page = types.ModuleType("rfq_ai_test_page")
        cls.page.__file__ = str(cls.root / "pages" / "request_for_quote.py")
        sys.modules[cls.page.__name__] = cls.page
        source = (Path(__file__).resolve().parents[1] / "pages" / "request_for_quote.py").read_text()
        with patch("dash.register_page"):
            exec(compile(source, cls.page.__file__, "exec"), cls.page.__dict__)
        cls.sovereign_assets = cls.page.load_sovereign_assets()
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

    def dispatch(self, output_fragment, values, changed):
        values = {"rfq-sovereign-assets.data": self.sovereign_assets, **values}
        matches = [(key, spec) for key, spec in self.app.callback_map.items() if output_fragment in key and spec.get("callback")]
        self.assertEqual(len(matches), 1)
        key, spec = matches[0]
        outputs = spec["output"] if isinstance(spec["output"], list) else [spec["output"]]
        response = self.client.post("/_dash-update-component", json={
            "output": key,
            "outputs": [{"id": out.component_id, "property": out.component_property} for out in outputs],
            "inputs": [{**item, "value": values.get(item["id"] + "." + item["property"])} for item in spec["inputs"]],
            "state": [{**item, "value": values.get(item["id"] + "." + item["property"])} for item in spec["state"]],
            "changedPropIds": changed,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["response"]

    def setUp(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM RFQ")
            conn.execute("DELETE FROM AI_REQUEST_LOG")
            conn.execute("DELETE FROM FORM_EVENT_LOG")

    def ai_dispatch(self, asset_class, patch_values, *, relevant=True, count=1, conflicts=False, unresolved=None,
                    text="Fill one complete test RFQ", usage=None):
        fields = self.page.rfq_empty_form(asset_class, "AI Test User")
        fields.update(patch_values)
        result = StructuredResult({
            "asset_class": asset_class, "is_rfq_request": relevant,
            "request_count": count, "has_conflicts": conflicts,
            "fields": fields, "unresolved_fields": unresolved or [],
        }, "test-model", "test-response", 1, 1, **(usage or {}))
        values = {
            f"rfq-ai-apply-{asset_class}.n_clicks": 1,
            f"rfq-ai-text-{asset_class}.value": text,
            f"rfq-form-{asset_class}.opened": True,
            "current-user.data": {"user": "AI Test User"},
        }
        self.service = Mock()
        self.service.extract.return_value = result
        with patch.object(self.page, "OpenAIService", return_value=self.service):
            return self.dispatch(f"rfq-ai-status-{asset_class}.children", values, [f"rfq-ai-apply-{asset_class}.n_clicks"])

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
        self.assertEqual(result["rfq-bond-name-sovereign"]["value"], "LTN")
        draft = result["rfq-ai-result-sovereign"]["data"]
        self.assertEqual(draft["fields"]["requester"], "AI Test User")
        maturity = self.dispatch("rfq-maturity-sovereign.options", {
            "rfq-bond-name-sovereign.value": "LTN", "rfq-ai-result-sovereign.data": draft,
            "rfq-maturity-sovereign.value": None,
        }, ["rfq-bond-name-sovereign.value", "rfq-ai-result-sovereign.data"])
        self.assertEqual(maturity["rfq-maturity-sovereign"]["value"], "LTN 01/01/2028")

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
                filtered = self.dispatch("rfq-maturity-sovereign.options", {
                    "rfq-bond-name-sovereign.value": bond,
                    "rfq-sovereign-assets.data": assets,
                }, ["rfq-bond-name-sovereign.value"])
                self.assertEqual([item["value"] for item in filtered["rfq-maturity-sovereign"]["options"]], [security])
            contract = self.page.rfq_json_contract("sovereign", "Test User", sovereign_assets=assets)
            self.assertEqual(contract["maturity_options_by_bond"]["LTN"][0]["value"], "LTN 01/01/2028")
            response = self.ai_dispatch("sovereign", {
                "direction": "Buy", "bond_name": "LTN", "maturity": "LTN 01/01/2028", "quantity": 10,
            })
            fields = response["rfq-ai-result-sovereign"]["data"]["fields"]
            state = {component_id + ".value": fields[name] for name, component_id in self.page.RFQ_FIELDS["sovereign"].items()}
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
        result = self.dispatch("rfq-requester-sovereign.value", {
            "current-user.data": {"user": "Signed In Username"},
        }, ["current-user.data"])
        for kind in self.page.RFQ_FIELDS:
            self.assertEqual(result[f"rfq-requester-{kind}"]["value"], "Signed In Username")

    def test_corporate_and_derivatives_apply_without_creating_records(self):
        corporate = self.ai_dispatch("corporate", {
            "direction": "Buy", "bond_type": "Corporate Bonds",
            "security": "TEST11", "volume": 1000000,
        })
        self.assertEqual(corporate["rfq-corp-security-dropdown"]["value"], "TEST11")
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
        self.assertEqual(first["rfq-qty-corporate"]["value"], 100)
        second = self.ai_dispatch("corporate", {"direction": "Sell", "security": "TEST11", "volume": 1000000})
        self.assertIsNone(second["rfq-qty-corporate"]["value"])
        self.assertIsNone(second["rfq-limit-corporate"]["value"])
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
        self.assertIsNone(result["rfq-qty-sovereign"]["value"])
        self.assertIsNone(result["rfq-direction-sovereign"]["value"])
        self.assertIn("unavailable", str(result["rfq-ai-status-sovereign"]))

    def test_multiple_conflicting_and_unrelated_requests_leave_form_empty(self):
        for kwargs in [{"count": 2}, {"conflicts": True}, {"relevant": False, "count": 0}]:
            with self.subTest(kwargs=kwargs):
                result = self.ai_dispatch("corporate", {"direction": "Buy", "quantity": 50, "security": "TEST11"}, **kwargs)
                self.assertIsNone(result["rfq-qty-corporate"]["value"])
                self.assertIsNone(result["rfq-corp-security-dropdown"]["value"])
                self.assertIsNone(result["rfq-direction-corporate"]["value"])

    def test_contracts_have_distinct_json_fields_and_use_exact_form_options(self):
        schemas = {}
        for kind in self.page.RFQ_FIELDS:
            contract = self.page.rfq_json_contract(kind, "Test User")
            schemas[kind] = contract["output_schema"]["properties"]["fields"]["properties"]
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
            self.assertNotIn("quote_type", contract["output_schema"]["properties"]["fields"]["properties"])
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
            self.assertNotIn("quote_type", self.service.extract.return_value.data["fields"])
            populated = response[f"rfq-ai-result-{kind}"]["data"]["fields"]
            state = {component_id + ".value": populated[name] for name, component_id in self.page.RFQ_FIELDS[kind].items()}
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
        fields = result["rfq-ai-result-sovereign"]["data"]["fields"]
        for name in ["direction", "maturity", "quantity", "volume"]:
            self.assertIsNone(fields[name])
        result = self.ai_dispatch("corporate", {"security": "TEST11", "volume": 100}, unresolved=["security"])
        self.assertIsNone(result["rfq-corp-security-dropdown"]["value"])
        self.assertEqual(result["rfq-volume-corporate"]["value"], 100)

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
        self.assertEqual(response["rfq-qty-corporate"]["value"], 1234)
        self.assertEqual(response["rfq-volume-corporate"]["value"], 1234567.89)
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

    def event_rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM FORM_EVENT_LOG ORDER BY EVENT_ID")]

    def test_full_prompt_log_matches_the_exact_sdk_request(self):
        fields = self.page.rfq_empty_form("corporate", "Test User")
        fields.update(direction="Buy", bond_type="Corporate Bonds", security="TEST11", volume=500000)
        output = json.dumps({"asset_class": "corporate", "is_rfq_request": True, "request_count": 1,
                             "has_conflicts": False, "fields": fields, "unresolved_fields": []})
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            status="completed", output=[], output_text=output, model="returned-model", id="test-id",
            usage=SimpleNamespace(input_tokens=2400, output_tokens=180, total_tokens=2580),
        )
        service = self.page.OpenAIService(client=client)
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

    def test_ai_errors_before_model_call_and_model_warnings_are_logged(self):
        self.ai_dispatch("corporate", {}, text="")
        self.service.extract.assert_not_called()
        error = self.event_rows()[0]
        self.assertEqual(error["LEVEL"], "error")
        self.assertEqual(error["SOURCE"], "ai_fill")
        self.assertEqual(error["USERNAME"], "AI Test User")
        self.assertIn("Describe one complete RFQ", error["MESSAGE"])
        self.assertIsNone(error["AI_LOG_ID"])
        self.ai_dispatch("corporate", {"direction": "Buy"})
        warning = self.event_rows()[-1]
        self.assertEqual(warning["LEVEL"], "warning")
        self.assertIn("Please specify", warning["MESSAGE"])
        self.assertEqual(warning["AI_LOG_ID"], self.audit_rows()[-1]["LOG_ID"])
        self.assertTrue(json.loads(warning["CONTEXT_JSON"])["missing_fields"])
        request = self.audit_rows()[-1]
        self.assertEqual(request["STATUS"], "invalid_response")
        self.assertEqual(request["ERROR_TYPE"], "MissingRFQDetails")
        self.assertEqual(request["ERROR_MESSAGE"], warning["MESSAGE"])

    def test_each_submission_validation_alert_is_logged(self):
        response = self.dispatch("rfq-notification-container.sendNotifications", {
            "rfq-submit-corporate.n_clicks": 1, "current-user.data": {"user": "Test User"},
        }, ["rfq-submit-corporate.n_clicks"])
        notifications = response["rfq-notification-container"]["sendNotifications"]
        rows = self.event_rows()
        self.assertTrue(rows)
        self.assertEqual([row["MESSAGE"] for row in rows], [item["message"] for item in notifications])
        self.assertTrue(all(row["USERNAME"] == "Test User" and row["SOURCE"] == "submit_rfq"
                            and row["FORM_TYPE"] == "corporate" and row["LEVEL"] == "error" for row in rows))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM RFQ").fetchone()[0], 0)

    def test_submission_database_failure_is_logged_after_rollback(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TRIGGER test_reject_rfq BEFORE INSERT ON RFQ BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        try:
            response = self.dispatch("rfq-notification-container.sendNotifications", {
                "rfq-submit-corporate.n_clicks": 1, "current-user.data": {"user": "Test User"},
                "rfq-direction-corporate.value": "Buy", "rfq-bond-type-corporate.value": "Corporate Bonds",
                "rfq-corp-security-dropdown.value": "TEST11", "rfq-volume-corporate.value": 500000,
            }, ["rfq-submit-corporate.n_clicks"])
            message = response["rfq-notification-container"]["sendNotifications"][0]["message"]
            self.assertEqual(message, "Unable to create RFQ. Try again.")
            self.assertEqual(self.event_rows()[0]["MESSAGE"], message)
        finally:
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("DROP TRIGGER test_reject_rfq")

    def test_broker_validation_alert_is_logged_without_database_lock(self):
        original = json.dumps([{"broker name": "Test Broker", "broker_response": "100", "timestamp": "2026-01-01T10:00"}])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO RFQ (RFQ_ID, CLASS, STR_RESPONSE) VALUES (42, 'Sovereign Bond', ?)", (original,))
        response = self.dispatch("rfq-error-notification.message", {
            "rfq-grid.cellValueChanged": {"colId": self.page._counterparty_slug("Test Broker"),
                                          "data": {"rfq_id": 42}, "newValue": "100|99:99"},
            "rfq-active-switch.checked": True, "current-user.data": {"user": "Test User"},
        }, ["rfq-grid.cellValueChanged"])
        self.assertEqual(response["rfq-error-notification"]["action"], "show")
        row = self.event_rows()[0]
        self.assertEqual(row["MESSAGE"], response["rfq-error-notification"]["message"])
        self.assertEqual(row["SOURCE"], "broker_response")
        self.assertEqual(row["RFQ_ID"], 42)
        self.assertEqual(row["FORM_TYPE"], "sovereign")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT STR_RESPONSE FROM RFQ WHERE RFQ_ID=42").fetchone()[0], original)

    def test_logging_failure_does_not_hide_original_alert(self):
        with patch.object(self.page.FormEventLog, "record", side_effect=sqlite3.OperationalError("test failure")):
            response = self.ai_dispatch("corporate", {}, text="")
        self.assertIn("Describe one complete RFQ", str(response["rfq-ai-status-corporate"]))

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
        self.assertIsNone(response["rfq-corp-security-dropdown"]["value"])
        row = self.audit_rows()[0]
        original = json.loads(row["MODEL_OUTPUT"])
        self.assertEqual(original["request_count"], 2)
        self.assertEqual(original["fields"]["security"], "TEST11")
        self.assertEqual(original["fields"]["requester"], "Model supplied name")
        self.assertEqual(row["USERNAME"], "AI Test User")

    def test_multiple_requests_warning_is_visible_in_the_ai_request_log(self):
        response = self.ai_dispatch("sovereign", {}, count=2, conflicts=True,
                                    text="compro b50 500k compro b40",
                                    usage={"input_tokens": 1890, "output_tokens": 420, "total_tokens": 2310})
        shown = response["rfq-ai-status-sovereign"]["children"]["props"]["children"]
        request = self.audit_rows()[0]
        event = self.event_rows()[0]
        self.assertIn("Several RFQs or conflicting details", shown)
        self.assertEqual(request["STATUS"], "invalid_response")
        self.assertEqual(request["ERROR_TYPE"], "ConflictingRFQs")
        self.assertEqual(request["ERROR_MESSAGE"], shown)
        self.assertEqual(event["MESSAGE"], shown)
        self.assertEqual(event["AI_LOG_ID"], request["LOG_ID"])
        self.assertEqual(json.loads(request["MODEL_OUTPUT"]), self.service.extract.return_value.data)
        self.assertIsNotNone(request["FULL_PROMPT"])
        self.assertEqual((request["INPUT_TOKENS"], request["OUTPUT_TOKENS"], request["TOTAL_TOKENS"]), (1890, 420, 2310))

    def test_attempt_is_committed_before_model_call_and_transport_failure_logged(self):
        service = Mock()

        def fail_request(**kwargs):
            row = self.audit_rows()[0]
            self.assertEqual(row["STATUS"], "running")
            self.assertIsNone(row["DT_FINISHED"])
            raise self.page.AIError("The AI service could not complete the request.", error_type="APITimeoutError")

        service.extract.side_effect = fail_request
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
        service = self.page.OpenAIService(client=client)
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
            self.page.extract_rfq("corporate", "Buy 500k TEST11", "Test User", service=self.page.OpenAIService(client=client))
        row = self.audit_rows()[0]
        self.assertEqual(row["STATUS"], "failed")
        self.assertEqual(row["ERROR_TYPE"], "IncompleteResponse")
        self.assertEqual((row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]), (321, 4000, 4321))

    def test_json_rejected_by_form_is_logged_before_error_is_returned(self):
        service = Mock()
        raw = '{\n  "unexpected": true\n}'
        service.extract.return_value = StructuredResult(
            {"unexpected": True}, "model", "bad-schema-response", 1, 1, raw,
            input_tokens=1100, output_tokens=95, total_tokens=1195,
        )
        with self.assertRaises(self.page.AIError):
            self.page.extract_rfq("corporate", "Buy TEST11", "Test User", service=service)
        row = self.audit_rows()[0]
        self.assertEqual(row["STATUS"], "invalid_response")
        self.assertEqual(row["MODEL_OUTPUT"], raw)
        self.assertEqual((row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]), (1100, 95, 1195))

    def test_no_model_call_if_request_cannot_be_logged(self):
        service = Mock()
        with patch.object(self.page.AIRequestLog, "begin", side_effect=sqlite3.OperationalError("test database unavailable")):
            with self.assertRaises(sqlite3.Error):
                self.page.extract_rfq("sovereign", "Buy LTN", "Test User", service=service)
        service.extract.assert_not_called()

    def test_manual_maturity_change_does_not_reapply_old_ai_result(self):
        result = self.dispatch("rfq-maturity-sovereign.options", {
            "rfq-bond-name-sovereign.value": "LFT", "rfq-maturity-sovereign.value": "LTN 01/01/2028",
            "rfq-ai-result-sovereign.data": {"fields": {"maturity": "LTN 01/01/2028"}},
        }, ["rfq-bond-name-sovereign.value"])
        self.assertIsNone(result["rfq-maturity-sovereign"]["value"])

    def test_new_modals_start_with_blank_trade_controls(self):
        for kind in self.page.RFQ_FIELDS:
            values = self.page.rfq_empty_form(kind, "Signed In Username")
            self.assertEqual(values.pop("requester"), "Signed In Username")
            for field, value in values.items():
                if field in self.page.SOLVE_FIELDS:
                    self.assertFalse(value)
                else:
                    self.assertIsNone(value, f"{kind}.{field} must open blank")

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
