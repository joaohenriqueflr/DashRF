"""Maintenance reports and callbacks, using isolated audit records and no SDK calls."""

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import csv
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from services.ai_audit import AIRequestLog
from services.ai_maintenance import AIMaintenanceService, ALL, UNKNOWN, scope_value
from services.prompts import PromptStore, PromptConflictError


MASTER = {"key_id": 1, "user": "Master", "role": "master"}
TRADER = {"key_id": 2, "user": "Trader", "role": "trader"}
NOW = datetime(2026, 11, 9, 14, 30, tzinfo=ZoneInfo("America/Sao_Paulo"))
FILTERS = {"start": "2026-11-01", "end": "2026-11-09", "user": ALL, "workload": ALL}


def populate(path):
    store = PromptStore(path)
    store.seed("routines", "NTNB_RUN", "Original instructions", "model-a", comment="Original comment")
    store.seed("rfq", "fill_test", "RFQ extraction", "model-b")
    prompt = store.get_active("routines", "NTNB_RUN")
    AIRequestLog(path).ensure_schema()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE USER_KEYS (KEY_ID INTEGER PRIMARY KEY, USR_NAME TEXT, ROLE TEXT, FL_ACTIVE INTEGER, SECRET_KEY TEXT);
            INSERT INTO USER_KEYS VALUES (1,'Master','master',1,'do-not-expose-master-secret');
            INSERT INTO USER_KEYS VALUES (2,'Trader','trader',1,'do-not-expose-trader-secret');
            INSERT INTO USER_KEYS VALUES (3,'Inactive','master',0,'do-not-expose-inactive-secret');
            CREATE TABLE UGM_ROUTINES (ID_ROUTINE TEXT PRIMARY KEY,NM_ROUTINE TEXT);
            INSERT INTO UGM_ROUTINES VALUES ('NTNB_RUN','NTN-B run'),('DEBTORS','Devedores');
            CREATE TABLE UGM_ROUTINE_CHAT (ID_MESSAGE INTEGER PRIMARY KEY,ID_ROUTINE TEXT,USERNAME TEXT,DT_REF TEXT,
                TS_MESSAGE TEXT,TS_RESPONSE TEXT,USER_MESSAGE TEXT,ASSISTANT_MESSAGE TEXT,STATUS TEXT,ID_EXECUTION INTEGER,ID_ACTIVITY TEXT);
            INSERT INTO UGM_ROUTINE_CHAT VALUES (1,'NTNB_RUN','Alice','2026-11-08','2026-11-08T23:58:00-03:00',
                '2026-11-09T00:03:00-03:00','Import the quotes','Ready for review','PENDING_REVIEW',NULL,'NTNB_RUN:DEFAULT');
            INSERT INTO UGM_ROUTINE_CHAT VALUES (2,'DEBTORS','Bob','2026-11-09','2026-11-09T12:00:00-03:00',
                '2026-11-09T12:03:00-03:00','=1+1','Recorded','COMPLETED',88,'DEBTORS:DEFAULT');
            INSERT INTO UGM_ROUTINE_CHAT VALUES (3,'NTNB_RUN','Alice','2026-11-08','2026-11-08T23:55:00-03:00',
                NULL,'Different activity','Other conversation','COMPLETED',NULL,'NTNB_RUN:OTHER');
        """)
        def request(identity, stamp, *, username="Alice", form="NTNB_RUN", feature="routines", usage=(10, 2, 12),
                    chat_id=1, record_type="REQUEST", model="model-a", status="completed", prompt_id=prompt.id, version=1, error=None):
            conn.execute("""INSERT INTO AI_REQUEST_LOG (LOG_ID,FEATURE,FORM_TYPE,USERNAME,PROMPT_TEXT,FULL_PROMPT,
                DT_STARTED,DT_FINISHED,PROMPT_ID,PROMPT_VERSION,MODEL,MODEL_OUTPUT,INPUT_TOKENS,OUTPUT_TOKENS,TOTAL_TOKENS,
                STATUS,ERROR_TYPE,ERROR_MESSAGE,RECORD_TYPE,CHAT_MESSAGE_ID) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, feature, form, username, "Recorded request", '{"instructions":"Original instructions"}', stamp,
                 stamp, prompt_id, version, model, '{"value":1}', *usage, status, error, "Sample error" if error else None, record_type, chat_id))
        request(1, "2026-11-09T02:59:00Z")
        request(2, "2026-11-09T03:01:00+00:00", usage=(20, 5, 25))
        request(3, "2026-11-09T15:00:00Z", username="Bob", form="DEBTORS", chat_id=2,
                usage=(None, None, None), status="failed", error="transport_error")
        request(4, "2026-11-09T15:01:00Z", username="Bob", form="DEBTORS", chat_id=2, usage=(0, 0, 0))
        request(5, "2026-11-08T15:00:00Z", usage=(100, 20, 120), record_type="LEGACY_AGGREGATE", model=None, prompt_id=None, version=None)
        request(6, "2026-11-09T16:00:00Z", form="fill_test", feature="rfq", usage=(5, 3, 8), chat_id=None)
    return prompt


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "test.db"
        self.prompt = populate(self.db)
        self.engine = AIMaintenanceService(self.db, clock=lambda: NOW)

    def test_master_access_rechecks_active_role_and_identity(self):
        self.assertTrue(self.engine.authorized(MASTER))
        for user in (None, {}, TRADER, {**TRADER, "role": "master"}, {**MASTER, "key_id": 3}, {**MASTER, "user": "Other"}):
            self.assertFalse(self.engine.authorized(user))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE USER_KEYS SET FL_ACTIVE=0 WHERE KEY_ID=1")
        self.assertFalse(self.engine.authorized(MASTER))

    def test_usage_local_boundaries_and_historical_call_count(self):
        result = self.engine.usage("2026-11-09")
        self.assertEqual((result["daily"]["REQUESTS"], result["daily"]["TOTAL_TOKENS"]), (4, 33))
        self.assertEqual((result["monthly"]["REQUESTS"], result["monthly"]["TOTAL_TOKENS"]), (5, 165))
        self.assertEqual(result["monthly"]["HISTORICAL_RECORDS"], 1)
        self.assertEqual(result["daily"]["MISSING_USAGE"], 1)
        day = self.engine.usage("2026-11-08", period="DLY")
        self.assertEqual(day["daily"]["TOTAL_TOKENS"], 132)
        self.assertEqual(day["daily"]["REQUESTS"], 1)

    def test_usage_by_user_and_routine_reconciles_with_filtered_records(self):
        for user in (ALL, "Alice", "Bob"):
            for workload in (ALL, scope_value("routines", "NTNB_RUN"), scope_value("rfq", "fill_test")):
                report = self.engine.usage("2026-11-09", user=user, workload=workload)
                filters = {**FILTERS, "user": user, "workload": workload}
                rows = self.engine.browse("requests", filters)["rows"]
                total = sum(r["TOTAL_TOKENS"] for r in rows if r["TOTAL_TOKENS"] is not None) if rows else None
                self.assertEqual(report["monthly"]["TOTAL_TOKENS"], total)
                self.assertEqual(sum(r["REQUESTS"] for r in report["users"]), report["monthly"]["REQUESTS"])

    def test_unknown_usage_remains_null_and_reported_zero_is_preserved(self):
        stats = self.engine.summary("requests", {**FILTERS, "request_status": "failed"})
        self.assertIsNone(stats["total"])
        self.assertEqual(stats["missing"], 1)
        rows = self.engine.browse("requests", {**FILTERS, "usage": "missing"})["rows"]
        self.assertEqual([r["LOG_ID"] for r in rows], [3])
        report = self.engine.usage("2026-11-09", user="Bob", workload=scope_value("routines", "DEBTORS"))
        self.assertEqual(report["daily"]["TOTAL_TOKENS"], 0)

    def test_hourly_trend_comes_from_actual_timestamps(self):
        result = self.engine.usage("2026-11-09", period="DLY")
        self.assertEqual(result["trend"][0]["BUCKET"], "00:00")
        self.assertEqual(result["trend"][0]["TOTAL_TOKENS"], 25)

    def test_model_breakdown_uses_recorded_models_and_preserves_unknown_history(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE AI_REQUEST_LOG SET MODEL='model-b' WHERE LOG_ID=6")
        report = self.engine.usage("2026-11-09")
        models = {row["MODEL"]: row for row in report["models"]}
        self.assertEqual(models["model-a"]["TOTAL_TOKENS"], 37)
        self.assertEqual(models["model-b"]["TOTAL_TOKENS"], 8)
        self.assertEqual(models[None]["TOTAL_TOKENS"], 120)
        self.assertEqual(models[None]["REQUESTS"], 0)
        self.assertEqual(models[None]["HISTORICAL_RECORDS"], 1)
        self.assertEqual(sum(row["TOTAL_TOKENS"] for row in models.values()), report["monthly"]["TOTAL_TOKENS"])
        self.assertEqual(sum(row["REQUESTS"] for row in models.values()), report["monthly"]["REQUESTS"])
        daily = self.engine.usage("2026-11-09", period="DLY")
        self.assertNotIn(None, {row["MODEL"] for row in daily["models"]})

    def test_model_breakdown_distinguishes_reported_zero_from_missing_usage(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE AI_REQUEST_LOG SET MODEL='missing-model' WHERE LOG_ID=3")
            conn.execute("UPDATE AI_REQUEST_LOG SET MODEL='zero-model' WHERE LOG_ID=4")
        models = {row["MODEL"]: row for row in self.engine.usage("2026-11-09", user="Bob")["models"]}
        self.assertIsNone(models["missing-model"]["TOTAL_TOKENS"])
        self.assertEqual(models["zero-model"]["TOTAL_TOKENS"], 0)

    def test_user_routine_interactions_include_chats_without_calls_without_fanout(self):
        report = self.engine.usage("2026-11-09")
        rows = {(row["USERNAME"], row["FEATURE"], row["FORM_TYPE"]): row for row in report["user_routines"]}
        alice = rows[("Alice", "routines", "NTNB_RUN")]
        self.assertEqual(alice["TOTAL_TOKENS"], 157)
        self.assertEqual(alice["REQUESTS"], 2)
        # Two calls and a legacy total belong to chat #1; chat #3 has no AI call.
        self.assertEqual(alice["INTERACTIONS"], 2)
        self.assertEqual(rows[("Bob", "routines", "DEBTORS")]["INTERACTIONS"], 1)
        self.assertIsNone(rows[("Alice", "rfq", "fill_test")]["INTERACTIONS"])
        self.assertEqual(sum(row["TOTAL_TOKENS"] for row in rows.values()), report["monthly"]["TOTAL_TOKENS"])

    def test_user_routine_interactions_respect_operational_day_and_shared_filters(self):
        scope = scope_value("routines", "NTNB_RUN")
        daily = self.engine.usage("2026-11-09", user="Alice", workload=scope, period="DLY")["user_routines"]
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["TOTAL_TOKENS"], 25)
        self.assertEqual(daily[0]["INTERACTIONS"], 0)
        previous = self.engine.usage("2026-11-08", user="Alice", workload=scope, period="DLY")["user_routines"]
        self.assertEqual(previous[0]["TOTAL_TOKENS"], 132)
        self.assertEqual(previous[0]["INTERACTIONS"], 2)
        rfq = self.engine.usage("2026-11-09", workload=scope_value("rfq", "fill_test"))["user_routines"]
        self.assertEqual(len(rfq), 1)
        self.assertIsNone(rfq[0]["INTERACTIONS"])

    def test_chat_only_user_routine_is_included_even_without_a_request_table(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO UGM_ROUTINE_CHAT (ID_MESSAGE,ID_ROUTINE,USERNAME,DT_REF,TS_MESSAGE,USER_MESSAGE,ASSISTANT_MESSAGE,STATUS) "
                         "VALUES (4,'DEBTORS','Python-only','2026-11-09','2026-11-09T12:00:00-03:00','Manual import','Saved','COMPLETED')")
        report = self.engine.usage("2026-11-09", user="Python-only")
        self.assertEqual(len(report["user_routines"]), 1)
        row = report["user_routines"][0]
        self.assertEqual((row["INTERACTIONS"], row["REQUESTS"], row["TOTAL_TOKENS"]), (1, 0, 0))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE AI_REQUEST_LOG")
        report = self.engine.usage("2026-11-09", user="Python-only")
        self.assertEqual(report["models"], [])
        self.assertEqual(report["user_routines"][0]["INTERACTIONS"], 1)

    def test_chats_use_operational_dates_and_conversations_keep_activity_scope(self):
        rows = self.engine.browse("chats", {**FILTERS, "start": "2026-11-08", "end": "2026-11-08"})["rows"]
        self.assertEqual({r["ID_MESSAGE"] for r in rows}, {1, 3})
        self.assertEqual([r["ID_MESSAGE"] for r in self.engine.conversation(1)], [1])
        linked = self.engine.linked_context(1)
        self.assertEqual((linked["start"], linked["end"]), ("2026-11-08", "2026-11-09"))
        self.assertEqual(len(linked["records"]), 3)
        rows = self.engine.browse("requests", {**FILTERS, "chat_id": 1})["rows"]
        self.assertEqual({r["LOG_ID"] for r in rows}, {1, 2, 5})

    def test_search_scans_payloads_errors_and_activity_ids_as_literal_text(self):
        for query, identity in (("Original instructions", 1), ("transport_error", 3)):
            self.assertIn(identity, [r["LOG_ID"] for r in self.engine.browse("requests", {**FILTERS, "request_query": query})["rows"]])
        self.assertEqual([r["ID_MESSAGE"] for r in self.engine.browse("chats", {**FILTERS, "chat_query": "NTNB_RUN:OTHER"})["rows"]], [3])
        self.assertEqual(self.engine.browse("requests", {**FILTERS, "request_query": "' OR 1=1 --"})["count"], 0)

    def test_server_paging_sorting_and_payloads_not_in_grid(self):
        rows = self.engine.browse("requests", FILTERS, limit=1, sort_model=[{"colId": "TOTAL_TOKENS", "sort": "desc"}])
        self.assertEqual(rows["count"], 6)
        self.assertEqual(rows["rows"][0]["LOG_ID"], 5)
        self.assertNotIn("FULL_PROMPT", rows["rows"][0])
        self.assertNotIn("MODEL_OUTPUT", rows["rows"][0])
        self.assertIsNone(rows["rows"][0]["DURATION_MS"])
        self.assertIsNone(rows["rows"][0]["MODEL"])
        self.assertEqual(self.engine.browse("requests", FILTERS, start=5, limit=1)["rows"][0]["LOG_ID"], 5)

    def test_invalid_scopes_dates_and_sort_fields_cannot_become_sql(self):
        for workload in ('{"a":"b","c":"d"}', '"ab"', '["routines"]'):
            with self.assertRaises(ValueError):
                self.engine.browse("requests", {**FILTERS, "workload": workload})
        with self.assertRaises(ValueError):
            self.engine.browse("requests", {**FILTERS, "start": "2026-12-01"})
        self.assertEqual(self.engine.browse("requests", FILTERS, sort_model=[{"colId": "LOG_ID; DROP TABLE AI_REQUEST_LOG", "sort": "desc"}])["count"], 6)

    def test_read_queries_and_metadata_never_mutate_or_expose_access_keys(self):
        before = self.db.read_bytes()
        metadata = self.engine.metadata()
        self.engine.browse("requests", FILTERS)
        self.engine.usage("2026-11-09")
        self.engine.record("chats", 1)
        self.assertEqual(before, self.db.read_bytes())
        self.assertNotIn("do-not-expose", json.dumps(metadata))
        with self.engine.connection() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM AI_REQUEST_LOG")

    def test_export_is_complete_filtered_and_spreadsheet_safe(self):
        self.engine.browse("requests", FILTERS, limit=1)
        exported = list(csv.DictReader(io.StringIO(self.engine.export_csv("requests", FILTERS))))
        self.assertEqual(len(exported), 6)
        self.assertIn("FULL_PROMPT", exported[0])
        subset = list(csv.DictReader(io.StringIO(self.engine.export_csv("requests", {**FILTERS, "chat_id": 1}))))
        self.assertEqual({r["LOG_ID"] for r in subset}, {"1", "2", "5"})
        chat = list(csv.DictReader(io.StringIO(self.engine.export_csv("chats", FILTERS))))
        self.assertEqual(next(r for r in chat if r["ID_MESSAGE"] == "2")["USER_MESSAGE"], "'=1+1")

    def test_activation_preserves_history_records_actor_and_rejects_stale_editor(self):
        changed = self.engine.activate_prompt(self.prompt.id, "Updated", "model-b", "Why changed", user=MASTER)
        self.assertEqual(changed.version, 2)
        self.assertEqual(self.engine.prompt(changed.id)["CREATED_BY"], "Master")
        self.assertEqual(self.engine.prompt(self.prompt.id)["INSTRUCTIONS"], "Original instructions")
        with self.assertRaises(PromptConflictError):
            self.engine.activate_prompt(self.prompt.id, "Stale overwrite", "model-a", "", user=MASTER)
        self.assertEqual(len(self.engine.prompt_history(changed.id)), 2)
        self.assertEqual(self.engine.record("requests", 1)["PROMPT_ID"], self.prompt.id)
        with self.assertRaises(PermissionError):
            self.engine.activate_prompt(changed.id, "Not allowed", "model-a", "", user=TRADER)

    def test_two_editors_cannot_activate_over_the_same_base_version(self):
        def activate(text):
            try:
                return self.engine.activate_prompt(self.prompt.id, text, "model-a", "", user=MASTER).version
            except PromptConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(activate, ["Editor A", "Editor B"]))
        self.assertCountEqual(results, [2, "conflict"])
        self.assertEqual(len(self.engine.prompt_history(self.prompt.id)), 2)

    def test_missing_log_tables_are_empty_without_schema_creation(self):
        path = Path(self.temp.name) / "empty.db"
        sqlite3.connect(path).close()
        engine = AIMaintenanceService(path)
        self.assertEqual(engine.browse("requests", FILTERS)["count"], 0)
        self.assertEqual(engine.usage("2026-11-09")["monthly"]["REQUESTS"], 0)
        with engine.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)


HAS_DASH = all(importlib.util.find_spec(name) for name in ("dash", "dash_ag_grid", "plotly"))


@unittest.skipUnless(HAS_DASH, "Run in the dashboard's Dash environment.")
class MaintenanceUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dash import Dash, dcc, html
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / "data").mkdir()
        cls.db = cls.root / "data" / "fixed_income.db"
        populate(cls.db)
        cls.engine = AIMaintenanceService(cls.db, clock=lambda: NOW)
        cls.page = types.ModuleType("maintenance_ui_page")
        cls.page.__file__ = str(cls.root / "pages" / "ai_maintenance.py")
        sys.modules[cls.page.__name__] = cls.page
        source = (Path(__file__).resolve().parents[1] / "pages" / "ai_maintenance.py").read_text()
        cls.app = Dash("maintenance_ui_tests", suppress_callback_exceptions=True)
        with patch("dash.register_page") as register:
            exec(compile(source, cls.page.__file__, "exec"), cls.page.__dict__)
        cls.registered_path = register.call_args.kwargs["path"]
        with patch.object(cls.page, "service", return_value=cls.engine):
            cls.app.layout = html.Div([dcc.Store(id="current-user", data=MASTER), cls.page.layout()])
        cls.app._setup_server()
        cls.client = cls.app.server.test_client()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(cls.page.__name__, None)
        cls.temp.cleanup()

    def setUp(self):
        self.service_patch = patch.object(self.page, "service", return_value=self.engine)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)

    def dispatch(self, output, values=None, trigger=None):
        values = {"current-user.data": MASTER, "aim-tab.value": "requests", "aim-query.data": FILTERS, **(values or {})}
        matches = [(key, spec) for key, spec in self.app.callback_map.items() if output in key and spec.get("callback")]
        self.assertEqual(len(matches), 1, output)
        key, spec = matches[0]
        outputs = spec["output"] if isinstance(spec["output"], list) else [spec["output"]]
        output_specs = [{"id": item.component_id, "property": item.component_property} for item in outputs]
        response = self.client.post("/_dash-update-component", json={
            "output": key, "outputs": output_specs if isinstance(spec["output"], list) else output_specs[0],
            "inputs": [{"id": item["id"], "property": item["property"], "value": values.get(item["id"] + "." + item["property"])} for item in spec["inputs"]],
            "state": [{"id": item["id"], "property": item["property"], "value": values.get(item["id"] + "." + item["property"])} for item in spec["state"]],
            "changedPropIds": [trigger or spec["inputs"][0]["id"] + "." + spec["inputs"][0]["property"]],
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["response"]

    def test_layout_and_dependencies_register_and_include_four_tabs(self):
        self.assertEqual(self.registered_path, "/ai-maintenance")
        response = self.client.get("/_dash-layout")
        self.assertEqual(response.status_code, 200)
        for label in ("Usage", "Prompts", "Routine chats", "AI request log"):
            self.assertIn(label, response.get_data(as_text=True))
        self.assertEqual(self.client.get("/_dash-dependencies").status_code, 200)

    def test_metadata_and_data_callbacks_reject_non_master(self):
        response = self.dispatch("aim-shell.style", {"current-user.data": TRADER})
        self.assertEqual(response["aim-shell"]["style"], {"display": "none"})
        with patch.object(self.engine, "browse") as browse:
            response = self.dispatch("aim-requests-grid.getRowsResponse", {"current-user.data": None,
                "aim-requests-grid.getRowsRequest": {"startRow": 0, "endRow": 25}})
        browse.assert_not_called()
        self.assertEqual(response["aim-requests-grid"]["getRowsResponse"]["rowData"], [])
        response = self.dispatch("aim-request-detail.children", {"current-user.data": TRADER, "aim-selected-request.data": 1})
        self.assertNotIn("Original instructions", json.dumps(response))

    def test_grid_callback_fetches_entire_count_without_payload_columns(self):
        response = self.dispatch("aim-requests-grid.getRowsResponse", {"aim-requests-grid.getRowsRequest":
            {"startRow": 0, "endRow": 1, "sortModel": [{"colId": "TOTAL_TOKENS", "sort": "desc"}]}})
        result = response["aim-requests-grid"]["getRowsResponse"]
        self.assertEqual(result["rowCount"], 6)
        self.assertEqual(len(result["rowData"]), 1)
        self.assertNotIn("FULL_PROMPT", result["rowData"][0])

    def test_filter_changes_replace_the_infinite_grid_cache(self):
        filters = {key: FILTERS.get(key, ALL) for key, _, _ in self.page.FILTER_PROPS}
        filters.update(chat_query="", request_query="", chat_id=None)
        values = {identity + "." + prop: filters[key] for key, identity, prop in self.page.FILTER_PROPS}
        values.update({"aim-chat-columns.value": self.page.DEFAULT_CHAT_FIELDS,
                       "aim-request-columns.value": self.page.DEFAULT_REQUEST_FIELDS, "aim-refresh.n_clicks": 0})
        first = self.dispatch("aim-query.data", values)
        first_key = first["aim-requests-results"]["children"]["props"]["key"]
        values["aim-browse-user.value"] = "Bob"
        changed = self.dispatch("aim-query.data", values, "aim-browse-user.value")
        self.assertNotEqual(first_key, changed["aim-requests-results"]["children"]["props"]["key"])
        self.assertEqual(changed["aim-query"]["data"]["user"], "Bob")

    def test_master_metadata_usage_and_prompt_history_render(self):
        response = self.dispatch("aim-shell.style")
        self.assertEqual(response["aim-shell"]["style"], {})
        self.assertNotIn("do-not-expose", json.dumps(response))
        usage = self.dispatch("aim-usage-content.children", {"aim-usage-date.date": "2026-11-09",
            "aim-usage-user.value": ALL, "aim-usage-workload.value": ALL, "aim-period.value": "DLY"})
        self.assertIn("Token consumption", json.dumps(usage))
        prompt = self.dispatch("aim-active-prompt.data", {"aim-prompt.value": 1})
        self.assertEqual(prompt["aim-active-prompt"]["data"]["INSTRUCTIONS"], "Original instructions")
        history = self.dispatch("aim-history-content.children", {"aim-history.value": 1,
            "aim-active-prompt.data": prompt["aim-active-prompt"]["data"]})
        self.assertFalse(history["aim-restore"]["disabled"])

    def test_usage_renders_model_and_user_routine_analysis(self):
        response = self.dispatch("aim-usage-content.children", {"aim-usage-date.date": "2026-11-09",
            "aim-usage-user.value": ALL, "aim-usage-workload.value": ALL, "aim-period.value": "MTD"})
        def grids(value):
            if isinstance(value, list):
                return [grid for child in value for grid in grids(child)]
            if isinstance(value, dict):
                if value.get("type") == "AgGrid":
                    return [value["props"]]
                return grids(value.get("props", {}).get("children"))
            return []
        tables = {grid["id"]: grid for grid in grids(response["aim-usage-content"]["children"])}
        self.assertEqual(len(tables), 4)
        self.assertEqual(sum(row["TOTAL_TOKENS"] for row in tables["aim-usage-models-grid"]["rowData"]), 165)
        combined = tables["aim-usage-user-routines-grid"]["rowData"]
        self.assertEqual(sum(row["INTERACTIONS"] or 0 for row in combined), 3)

    def test_combined_usage_drilldown_applies_user_and_routine_together(self):
        scope = scope_value("routines", "NTNB_RUN")
        response = self.dispatch("aim-usage-user.value", {"aim-usage-user-routines-grid.cellClicked":
            {"data": {"USERNAME": "Alice", "WORKLOAD": scope}}}, "aim-usage-user-routines-grid.cellClicked")
        self.assertEqual(response["aim-usage-user"]["value"], "Alice")
        self.assertEqual(response["aim-usage-workload"]["value"], scope)

    def test_activation_callback_saves_once_and_rejects_replay(self):
        store = PromptStore(self.db)
        store.seed("ui_test", "activate", "UI initial", "model-a")
        initial = store.get_active("ui_test", "activate")
        active = self.engine.prompt(initial.id)
        reviewed = {"prompt_id": initial.id, "model": "model-a", "instructions": "Reviewed UI changes", "comment": "UI test"}
        values = {"aim-activate.n_clicks": 1, "aim-reviewed.data": reviewed, "aim-active-prompt.data": active,
                  "aim-model.value": reviewed["model"], "aim-instructions.value": reviewed["instructions"],
                  "aim-comment.value": reviewed["comment"], "aim-prompt-refresh.data": 0}
        response = self.dispatch("aim-prompt-refresh.data", values, "aim-activate.n_clicks")
        self.assertEqual(response["aim-prompt-refresh"]["data"], 1)
        self.assertEqual(store.get_active("ui_test", "activate").version, 2)
        replay = self.dispatch("aim-prompt-refresh.data", values, "aim-activate.n_clicks")
        self.assertNotIn("aim-prompt-refresh", replay)
        self.assertIn("changed since", json.dumps(replay))
        self.assertEqual(len(self.engine.prompt_history(initial.id)), 2)

    def test_request_inspector_preserves_unknown_provenance(self):
        response = self.dispatch("aim-request-detail.children", {"aim-selected-request.data": 5})
        text = json.dumps(response)
        self.assertIn("Historical aggregate", text)
        self.assertIn("Unknown", text)
        self.assertTrue(response["aim-log-prompt"]["disabled"])
        self.assertFalse(response["aim-request-chat"]["disabled"])

    def test_review_is_required_and_editing_after_review_cannot_activate(self):
        active = self.engine.prompt_history(1)[0]
        values = {"aim-review.n_clicks": 1, "aim-model.value": "model-a", "aim-instructions.value": "Reviewed text",
                  "aim-comment.value": "Test review", "aim-active-prompt.data": active}
        response = self.dispatch("aim-reviewed.data", values, "aim-review.n_clicks")
        reviewed = response["aim-reviewed"]["data"]
        self.assertFalse(response["aim-activate"]["disabled"])
        before = len(self.engine.prompt_history(1))
        result = self.page.activate_prompt(1, reviewed, active, "model-a", "Unreviewed text", "Test review", MASTER, 0)
        from dash import no_update
        self.assertIs(result[0], no_update)
        self.assertEqual(len(self.engine.prompt_history(1)), before)

    def test_linked_navigation_preserves_context_and_dates_across_midnight(self):
        state = {key: None for key, _, _ in self.page.NAV_PROPS}
        state.update(FILTERS, tab="chats", chat_query="original search", request_query="stale filter", request_status="failed",
                     selected_chat=1, selected_request=3, backstack=[])
        linked = self.page.navigation_changes(self.engine, "aim-chat-requests", state, ())
        self.assertEqual((linked["start"], linked["end"]), ("2026-11-08", "2026-11-09"))
        self.assertEqual(linked["request_query"], "")
        self.assertEqual(linked["chat_id"], 1)
        self.assertEqual(self.engine.browse("requests", linked)["count"], 3)
        restored = self.page.navigation_changes(self.engine, "aim-back", linked, ())
        self.assertEqual(restored["request_query"], "stale filter")
        self.assertEqual(restored["chat_query"], "original search")

    def test_navigation_callback_selects_a_row_without_reloading_prompt_editor(self):
        state = {key: None for key, _, _ in self.page.NAV_PROPS}
        state.update(FILTERS, tab="chats", backstack=[], prompt=1)
        values = {identity + "." + prop: state[key] for key, identity, prop in self.page.NAV_PROPS}
        values.update({"aim-chats-grid.cellClicked": {"data": {"ID_MESSAGE": 1}},
                       "aim-usage-date.date": "2026-11-09", "aim-usage-user.value": ALL,
                       "aim-usage-workload.value": ALL, "aim-period.value": "MTD"})
        response = self.dispatch("aim-nav-status.children", values, "aim-chats-grid.cellClicked")
        self.assertEqual(response["aim-selected-chat"]["data"], 1)
        self.assertNotIn("aim-prompt", response)

    def test_export_callback_returns_all_filtered_rows(self):
        response = self.dispatch("aim-download.data", {"aim-export.n_clicks": 1}, "aim-export.n_clicks")
        content = response["aim-download"]["data"]["content"]
        self.assertEqual(len(list(csv.DictReader(io.StringIO(content)))), 6)


if __name__ == "__main__":
    unittest.main()
