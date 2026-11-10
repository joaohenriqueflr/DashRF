"""Exercise the shared routine page through real Dash HTTP callbacks."""

import base64
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from services.prompts import PromptStore
from routine_test_support import single_ntnb_activity

HAS_DASH = all(importlib.util.find_spec(name) for name in (
    "dash", "dash_ag_grid", "dash_bootstrap_components",
))


@unittest.skipUnless(HAS_DASH, "Run UI tests in the dashboard's Dash environment.")
class RoutinesUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dash import Dash, dcc, html

        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / "data").mkdir()
        cls.db = cls.root / "data" / "fixed_income.db"
        with closing(sqlite3.connect(cls.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE FIXED_INCOME_BROKER_DATA (
                    DT_REF DATE,ID_SECURITY INTEGER,NM_SECURITY TEXT,NM_SOURCE TEXT,
                    NM_BROKER TEXT,SIDE TEXT,TX_QUOTE REAL,VL_QUOTE REAL,DT_INPUT DATE,USR_INPUT TEXT
                );
                CREATE TABLE FIXED_INCOME_SECURITY_CLASSIFICATION (
                    ID_SECURITY INTEGER,NM_SECURITY TEXT,NM_INSTRUMENT_TYPE TEXT,DT_MATURITY TEXT
                );
                INSERT INTO FIXED_INCOME_SECURITY_CLASSIFICATION VALUES
                    (1,'NTN-B 15/08/2030','NTN-B','2030-08-15'),
                    (2,'TEST11','Debenture','2031-01-01'),
                    (3,'NTN-B 15/08/2028','NTN-B','2028-08-15'),
                    (4,'NTN-B 15/08/2035','NTN-B','2035-08-15');
            """)
        cls.app = Dash("routines_ui_tests", suppress_callback_exceptions=True)
        cls.page = types.ModuleType("routines_ui_test_page")
        cls.page.__file__ = str(cls.root / "pages" / "routines.py")
        sys.modules[cls.page.__name__] = cls.page
        source = (Path(__file__).resolve().parents[1] / "pages" / "routines.py").read_text()
        with patch("dash.register_page"):
            exec(compile(source, cls.page.__file__, "exec"), cls.page.__dict__)
        single_ntnb_activity(cls.page.service())
        cls.app.layout = html.Div([dcc.Store(id="current-user", data={"user": "Alice"}), cls.page.layout()])
        PromptStore(cls.db).seed("routines", "NTNB_RUN", "Test prompt", "test-model")
        cls.app._setup_server()
        cls.client = cls.app.server.test_client()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(cls.page.__name__, None)
        cls.temp.cleanup()

    def setUp(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for table in ("UGM_ROUTINE_CHAT", "UGM_ROUTINE_EXECUTIONS", "UGM_ROUTINES_DEVEDORES", "FIXED_INCOME_BROKER_DATA"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("UPDATE UGM_ROUTINE_ACTIVITY_TOOLS SET REVIEW_POLICY='AUTO' WHERE NM_TOOL LIKE 'query_%'")

    @staticmethod
    def pattern(component_id):
        return json.dumps(component_id, sort_keys=True, separators=(",", ":"))

    def dispatch(self, output_fragment, values=None, changed=None, *, execution=None, expected_status=200):
        values = {"routine-selected.data": "NTNB_RUN", "current-user.data": {"user": "Alice"},
                  "routine-refresh.data": 0, **(values or {})}
        values.setdefault("routine-activity-selected.data", {
            "routine": values["routine-selected.data"],
            "activity": values["routine-selected.data"] + ":DEFAULT",
        })
        matches = [(key, spec) for key, spec in self.app.callback_map.items()
                   if output_fragment in key and spec.get("callback")]
        self.assertEqual(len(matches), 1, output_fragment)
        key, spec = matches[0]

        def resolve(component_id):
            if isinstance(component_id, str) and component_id.startswith("{"):
                component_id = json.loads(component_id)
            if isinstance(component_id, dict) and execution is not None:
                return {name: execution if name == "execution" else value for name, value in component_id.items()}
            return component_id

        def payload(item):
            component_id = resolve(item["id"])
            lookup = self.pattern(component_id) if isinstance(component_id, dict) else component_id
            return {"id": component_id, "property": item["property"],
                    "value": values.get(lookup + "." + item["property"])}

        outputs = spec["output"] if isinstance(spec["output"], list) else [spec["output"]]
        output_specs = [{"id": resolve(item.component_id), "property": item.component_property} for item in outputs]
        response = self.client.post("/_dash-update-component", json={
            "output": key, "outputs": output_specs if isinstance(spec["output"], list) else output_specs[0],
            "inputs": [payload(item) for item in spec["inputs"]],
            "state": [payload(item) for item in spec["state"]],
            "changedPropIds": changed or [spec["inputs"][0]["id"] + "." + spec["inputs"][0]["property"]],
        })
        self.assertEqual(response.status_code, expected_status, response.get_data(as_text=True))
        if expected_status == 204:
            return {}
        return response.json["response"]

    def prepare(self, *, upload=False, activity_id="NTNB_RUN:DEFAULT"):
        if not upload:
            return self.page.service().prepare_action("NTNB_RUN", "Alice", "save_quotes", {
                "source": "XP", "rows": [
                    {"NM_SECURITY": "NTN-B 15/08/2030", "SIDE": "Bid", "TX_QUOTE": 0.075},
                    {"NM_SECURITY": "NTN-B 15/08/2030", "SIDE": "Ask", "TX_QUOTE": 0.076},
                ],
            }, activity_id=activity_id)
        text = "NM_SECURITY\tSIDE\tTX_QUOTE\nTEST11\tBid\t0.075\nTEST11\tAsk\t0.076"
        values = {"routine-selected.data": "CREDIT_RUN", "routine-prepare.n_clicks": 1,
                  "routine-import-text.value": "", "routine-source.value": "Santander",
                  "routine-upload.contents": "data:text/plain;base64," + base64.b64encode(text.encode()).decode(),
                  "routine-upload.filename": "run.tsv"}
        response = self.dispatch("routine-chat-feedback.children", values, ["routine-prepare.n_clicks"])
        self.assertIn("Revisão preparada", response["routine-import-feedback"]["children"])
        return self.page.service().get_routine_executions("CREDIT_RUN", username="Alice")[-1]

    def card_action(self, execution, *, cancel=False, rows=None, username="Alice"):
        execution_id = execution["ID_EXECUTION"]
        component = lambda kind: {"type": kind, "execution": execution_id}
        values = {
            "routine-selected.data": execution["ID_ROUTINE"],
            "routine-activity-selected.data": {"routine": execution["ID_ROUTINE"], "activity": execution["ID_ACTIVITY"]},
            "current-user.data": {"user": username},
            self.pattern(component("routine-card-confirm")) + ".n_clicks": 0 if cancel else 1,
            self.pattern(component("routine-card-cancel")) + ".n_clicks": 1 if cancel else 0,
            self.pattern(component("routine-card-grid")) + ".rowData": rows if rows is not None else execution["INPUT_JSON"].get("rows", []),
            self.pattern(component("routine-card-grid")) + ".id": component("routine-card-grid"),
        }
        trigger = component("routine-card-cancel" if cancel else "routine-card-confirm")
        return self.dispatch("routine-card-feedback", values, [self.pattern(trigger) + ".n_clicks"], execution=execution_id)

    def quote_rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT SIDE,TX_QUOTE FROM FIXED_INCOME_BROKER_DATA").fetchall()

    def test_layout_and_access_use_existing_user_store(self):
        response = self.dispatch("routine-shell.style", {"current-user.data": None})
        self.assertEqual(response["routine-shell"]["style"], {"display": "none"})
        blocked = self.dispatch("routine-chat-feedback.children", {
            "current-user.data": None, "routine-send.n_clicks": 1, "routine-chat-text.value": "hello",
        }, ["routine-send.n_clicks"])
        self.assertIn("UGM Access", blocked["routine-chat-feedback"]["children"])
        self.assertEqual(self.quote_rows(), [])
        self.assertEqual(self.client.get("/_dash-layout").status_code, 200)

    def test_header_loads_configured_prompt_and_sources(self):
        response = self.dispatch("routine-title.children")
        self.assertEqual(response["routine-title"]["children"], "Run NTN-B")
        self.assertEqual(response["routine-configuration"]["children"], "")
        self.assertEqual(response["routine-source"]["value"], "XP")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            original = conn.execute("SELECT SOURCES_JSON FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ACTIVITY='CREDIT_RUN:DEFAULT'").fetchone()[0]
            conn.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET SOURCES_JSON='[]' WHERE ID_ACTIVITY='CREDIT_RUN:DEFAULT'")
        try:
            response = self.dispatch("routine-title.children", {"routine-selected.data": "CREDIT_RUN"})
            self.assertEqual(response["routine-source"]["options"], [])
            self.assertIsNone(response["routine-source"]["value"])
        finally:
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET SOURCES_JSON=? WHERE ID_ACTIVITY='CREDIT_RUN:DEFAULT'", (original,))

    def test_import_upload_persists_review_without_operational_write(self):
        execution = self.prepare(upload=True)
        self.assertEqual(execution["STATUS"], "PENDING_REVIEW")
        self.assertEqual(self.quote_rows(), [])
        transcript = self.dispatch("routine-chat.children", {"routine-selected.data": "CREDIT_RUN"})
        rendered = json.dumps(transcript)
        self.assertIn("routine-card-confirm", rendered)
        self.assertIn("TEST11", rendered)

    def test_delete_edit_confirm_and_repeat_are_atomic_without_llm(self):
        execution = self.prepare()
        execution_id = execution["ID_EXECUTION"]
        rows = execution["INPUT_JSON"]["rows"]
        grid = {"type": "routine-card-grid", "execution": execution_id}
        delete = {"type": "routine-card-delete", "execution": execution_id}
        response = self.dispatch("routine-card-grid", {
            self.pattern(delete) + ".n_clicks": 1, self.pattern(grid) + ".selectedRows": [rows[1]],
            self.pattern(grid) + ".rowData": rows,
        }, [self.pattern(delete) + ".n_clicks"], execution=execution_id)
        edited = response[self.pattern(grid)]["rowData"]
        edited[0]["TX_QUOTE"] = 0.081
        with patch("services.openai_client.OpenAIService.converse") as llm:
            self.card_action(execution, rows=edited)
            self.card_action(execution, rows=edited)
            llm.assert_not_called()
        self.assertEqual(self.quote_rows(), [("Bid", 0.081)])
        self.assertEqual(self.page.service().get_execution(execution_id, "Alice")["STATUS"], "SUCCESS")

    def test_invalid_card_edit_stays_pending_and_other_users_cannot_confirm(self):
        execution = self.prepare()
        rows = execution["INPUT_JSON"]["rows"]
        rows[0]["TX_QUOTE"] = 7.5
        self.card_action(execution, rows=rows)
        self.assertEqual(self.page.service().get_execution(execution["ID_EXECUTION"], "Alice")["STATUS"], "PENDING_REVIEW")
        self.card_action(execution, username="Bob")
        self.assertEqual(self.quote_rows(), [])

    def test_cancel_button_records_cancellation_without_llm(self):
        execution = self.prepare()
        with patch("services.openai_client.OpenAIService.converse") as llm:
            self.card_action(execution, cancel=True)
            llm.assert_not_called()
        self.assertEqual(self.page.service().get_execution(execution["ID_EXECUTION"], "Alice")["STATUS"], "CANCELLED")
        self.assertEqual(self.quote_rows(), [])

    def test_button_cannot_confirm_another_cards_state(self):
        first, second = self.prepare(), self.prepare()
        execution_id = first["ID_EXECUTION"]
        component = lambda kind: {"type": kind, "execution": execution_id}
        trigger = component("routine-card-confirm")
        self.dispatch("routine-card-feedback", {
            self.pattern(trigger) + ".n_clicks": 1,
            self.pattern(component("routine-card-cancel")) + ".n_clicks": 0,
            self.pattern(component("routine-card-grid")) + ".rowData": second["INPUT_JSON"]["rows"],
            self.pattern(component("routine-card-grid")) + ".id": {
                "type": "routine-card-grid", "execution": second["ID_EXECUTION"]},
        }, [self.pattern(trigger) + ".n_clicks"], execution=execution_id)
        self.assertEqual(self.quote_rows(), [])
        self.assertEqual(self.page.service().get_execution(execution_id, "Alice")["STATUS"], "PENDING_REVIEW")
        self.assertEqual(self.page.service().get_execution(second["ID_EXECUTION"], "Alice")["STATUS"], "PENDING_REVIEW")

    def test_poll_updates_shared_data_without_remounting_chat_cards(self):
        first = self.dispatch("routine-timeline.children", {"routine-poll.n_intervals": 0})
        key = first["routine-view-key"]["data"]
        second = self.dispatch("routine-timeline.children", {
            "routine-poll.n_intervals": 1, "routine-view-key.data": key,
        }, ["routine-poll.n_intervals"])
        self.assertNotIn("routine-view-key", second)
        self.assertNotIn("routine-chat", second)
        chat_spec = next(spec for name, spec in self.app.callback_map.items() if name == "routine-chat.children")
        self.assertFalse(any(item["id"] == "routine-poll" for item in chat_spec["inputs"]))

    def test_selection_clears_uploaded_file_and_unsent_text(self):
        response = self.dispatch("routine-import-text.value", {
            "routine-selected.data": "CREDIT_RUN",
            "routine-view-key.data": ["CREDIT_RUN", "Alice", self.page.service().today()],
        }, ["routine-selected.data"])
        self.assertIsNone(response["routine-upload"]["contents"])
        self.assertIsNone(response["routine-upload"]["filename"])
        self.assertEqual(response["routine-import-text"]["value"], "")
        self.assertEqual(response["routine-chat-text"]["value"], "")
        blocked = self.dispatch("routine-chat-feedback.children", {
            "routine-selected.data": "CREDIT_RUN", "routine-prepare.n_clicks": 1,
            "routine-source.value": None, "routine-import-text.value": "old file",
        }, ["routine-prepare.n_clicks"])
        self.assertIn("Selecione uma fonte", blocked["routine-import-feedback"]["children"])
        self.assertEqual(self.quote_rows(), [])

    def test_match_outputs_follow_dash_frontend_wildcard_rules(self):
        from dash import MATCH

        # The renderer rejects mixed fixed/MATCH outputs even when Flask accepts
        # a manually dispatched callback. Mirror its findMismatchedWildcards rule.
        for name, spec in self.app.callback_map.items():
            outputs = spec["output"] if isinstance(spec["output"], list) else [spec["output"]]
            match_keys = []
            for output in outputs:
                component_id = output.component_id
                match_keys.append({key for key, value in component_id.items() if value is MATCH}
                                  if isinstance(component_id, dict) else set())
            for keys in match_keys[1:]:
                self.assertEqual(keys, match_keys[0], f"Renderer would reject MATCH outputs: {name}")
            for dependency in spec["inputs"] + spec["state"]:
                component_id = dependency["id"]
                if not component_id.startswith("{"):
                    continue
                component_id = json.loads(component_id)
                required = {key for key, value in component_id.items()
                            if value == ["MATCH"] or value == ["ALLSMALLER"]}
                self.assertTrue(required.issubset(match_keys[0]), f"Renderer would reject input/state: {name}")

    def test_card_result_refreshes_shared_page_once_and_ignores_mounts(self):
        pattern = self.pattern({"type": "routine-card-result", "execution": ["ALL"]})
        self.dispatch("routine-card-notice.data", {pattern + ".data": [None]}, expected_status=204)
        execution = self.prepare()
        result = self.card_action(execution)
        result_id = self.pattern({"type": "routine-card-result", "execution": execution["ID_EXECUTION"]})
        notice = result[result_id]["data"]
        self.assertEqual(notice["status"], "SUCCESS")
        refreshed = self.dispatch("routine-card-notice.data", {pattern + ".data": [notice]})
        self.assertEqual(refreshed["routine-refresh"]["data"], 1)
        self.dispatch("routine-card-notice.data", {
            pattern + ".data": [notice], "routine-refresh.data": 1,
            "routine-card-notice.data": notice,
        }, expected_status=204)
        self.dispatch("routine-card-notice.data", {
            pattern + ".data": [], "routine-refresh.data": 1,
            "routine-card-notice.data": notice,
        }, expected_status=204)

    def test_query_review_keeps_preview_readonly_and_uses_original_filters(self):
        execution = self.prepare(upload=True)
        self.card_action(execution)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE UGM_ROUTINE_ACTIVITY_TOOLS SET REVIEW_POLICY='REVIEW' WHERE ID_ACTIVITY='CREDIT_RUN:DEFAULT' AND NM_TOOL='query_quotes'")
        query = self.page.service().prepare_action("CREDIT_RUN", "Alice", "query_quotes", {"source": "Santander"})
        card = self.page.review_card(query)
        grid = next(child for child in card.children if getattr(child, "id", None) == {"type": "routine-card-grid", "execution": query["ID_EXECUTION"]})
        self.assertEqual(len(grid.rowData), 2)
        self.assertTrue(all(not column["editable"] for column in grid.columnDefs))
        self.card_action(query, rows=[{"source": "forged", "limit": 1}])
        stored = self.page.service().get_execution(query["ID_EXECUTION"], "Alice")
        self.assertEqual(stored["INPUT_JSON"]["source"], "Santander")
        self.assertEqual(stored["STATUS"], "SUCCESS")
        self.assertEqual(len(self.quote_rows()), 2)

    def test_quote_import_shows_chat_with_vertical_timeline_and_daily_summary(self):
        for routine_id in ("NTNB_RUN", "NTNF_RUN"):
            with self.subTest(routine_id=routine_id):
                view = self.dispatch("routine-title.children", {"routine-selected.data": routine_id})
                self.assertEqual(view["routine-import-panel"]["style"], {"display": "none"})
                self.assertEqual(view["routine-data-panel"]["style"], {"display": "none"})
                self.assertIn("routine-workspace-import", view["routine-workspace"]["className"])
                self.assertEqual(view["routine-quote-summary-panel"]["style"], {})
                self.assertIn("routine-timeline-vertical", view["routine-timeline"]["className"])
                self.assertIn("Cole", view["routine-chat-text"]["placeholder"])
                with patch.object(self.page.RoutineService, "get_operational_data") as query:
                    self.dispatch("routine-timeline.children", {"routine-selected.data": routine_id})
                    query.assert_not_called()
        generic = self.dispatch("routine-title.children", {"routine-selected.data": "CREDIT_RUN"})
        self.assertEqual(generic["routine-import-panel"]["style"], {})
        self.assertEqual(generic["routine-data-panel"]["style"], {})
        self.assertEqual(generic["routine-workspace"]["className"], "routine-workspace")
        self.assertEqual(generic["routine-quote-summary-panel"]["style"], {"display": "none"})
        self.assertEqual(generic["routine-timeline"]["className"], "routine-timeline")

        def find_component(component, component_id):
            if getattr(component, "id", None) == component_id:
                return component
            children = getattr(component, "children", [])
            for child in children if isinstance(children, (list, tuple)) else [children]:
                found = find_component(child, component_id)
                if found is not None:
                    return found
            return None

        workspace = find_component(self.app.layout, "routine-workspace")
        self.assertIn("routine-chat-panel", workspace.children[0].className)
        self.assertEqual(workspace.children[1].id, "routine-timeline-panel")
        self.assertFalse(any(getattr(child, "id", None) == "routine-quote-summary-panel" for child in workspace.children))
        self.assertIsNotNone(find_component(self.app.layout, "routine-quote-chart"))
        self.assertIsNotNone(find_component(self.app.layout, "routine-quote-data"))

    def test_quote_chat_forwards_pasted_table_without_manual_step_selection(self):
        text = "NTN-B 2030\t7,5\t7,6"
        with patch.object(self.page.RoutineService, "process_routine_message", return_value={"text": "Revise a importação."}) as process:
            response = self.dispatch("routine-chat-feedback.children", {
                "routine-send.n_clicks": 1, "routine-chat-text.value": text,
            }, ["routine-send.n_clicks"])
        process.assert_called_once_with("NTNB_RUN", "Alice", text, activity_id="NTNB_RUN:DEFAULT")
        self.assertEqual(response["routine-chat-text"]["value"], "")
        with patch.object(self.page.RoutineService, "process_routine_message", side_effect=ValueError("Cole uma tabela de cotações.")):
            refused = self.dispatch("routine-chat-feedback.children", {
                "routine-send.n_clicks": 1, "routine-chat-text.value": "Qual a cotação de ontem?",
            }, ["routine-send.n_clicks"])
        self.assertIn("Cole uma tabela", refused["routine-chat-feedback"]["children"])
        self.assertNotIn("routine-chat-text", refused)

    def test_compact_interaction_history_renders_user_and_consolidated_assistant_outcome(self):
        history = [{"ID_MESSAGE": 1, "ID_ROUTINE": "NTNB_RUN", "USERNAME": "Alice",
                    "DT_REF": self.page.service().today(), "TS_MESSAGE": "2026-11-04T09:00:00-03:00",
                    "TS_RESPONSE": "2026-11-04T09:00:05-03:00", "USER_MESSAGE": "Tabela colada pelo usuário",
                    "ASSISTANT_MESSAGE": "Importação concluída: duas cotações.", "STATUS": "SUCCESS",
                    "INPUT_TOKENS": 100, "OUTPUT_TOKENS": 20, "TOTAL_TOKENS": 120}]
        with patch.object(self.page.RoutineService, "load_chat_history", return_value=history):
            rendered = json.dumps(self.dispatch("routine-chat.children"), ensure_ascii=False)
        self.assertIn("Tabela colada pelo usuário", rendered)
        self.assertIn("Importação concluída: duas cotações.", rendered)
        self.assertIn("routine-message-user", rendered)
        self.assertIn("routine-message-assistant", rendered)
        self.assertNotIn("CONTENT_JSON", rendered)

    def test_quote_transcript_keeps_import_cards_and_omits_historical_lookup_cards(self):
        imported = self.prepare()
        lookup = {**imported, "ID_EXECUTION": imported["ID_EXECUTION"] + 100,
                  "NM_ACTION": "query_quotes", "STATUS": "SUCCESS", "INPUT_JSON": {},
                  "RESULT_JSON": {"kind": "query", "rows": [{"NM_SECURITY": "LOOKUP_ONLY_SECURITY"}]}}
        with patch.object(self.page.RoutineService, "get_routine_executions", return_value=[lookup, imported]):
            rendered = json.dumps(self.dispatch("routine-chat.children"), ensure_ascii=False)
        self.assertIn("Importação de cotações", rendered)
        self.assertIn("routine-card-confirm", rendered)
        self.assertNotIn("Consulta de cotações", rendered)
        self.assertNotIn("LOOKUP_ONLY_SECURITY", rendered)

    def test_quote_legacy_upload_and_manual_status_requests_are_rejected(self):
        with patch.object(self.page, "parse_import") as parser:
            response = self.dispatch("routine-chat-feedback.children", {
                "routine-prepare.n_clicks": 1, "routine-source.value": "XP",
                "routine-upload.contents": "data:text/plain;base64,b2xkIGZpbGU=",
            }, ["routine-prepare.n_clicks"])
            parser.assert_not_called()
        self.assertIn("diretamente no chat", response["routine-import-feedback"]["children"])
        pattern = self.pattern({"type": "routine-activity-complete", "activity": ["ALL"]})
        changed = self.pattern({"type": "routine-activity-complete", "activity": "hidden-step"}) + ".n_clicks"
        with patch.object(self.page.RoutineService, "complete_activity") as complete:
            response = self.dispatch("routine-timeline-feedback.children", {pattern + ".n_clicks": [1]}, [changed])
            complete.assert_not_called()
        self.assertIn("automaticamente", response["routine-timeline-feedback"]["children"])

    def test_quote_timeline_shows_automatic_progress_without_manual_buttons(self):
        steps = [{"ID_ACTIVITY": "morning", "NM_ACTIVITY": "Run da manhã", "DEADLINE_TIME": "10:00",
                  "state": "PENDING", "progress": "PENDING_REVIEW"}]
        with patch.object(self.page.RoutineService, "get_routine_timeline", return_value=steps):
            quote = json.dumps(self.dispatch("routine-timeline.children"), ensure_ascii=False)
            generic = json.dumps(self.dispatch("routine-timeline.children", {"routine-selected.data": "CREDIT_RUN"}), ensure_ascii=False)
        self.assertIn("Aguardando confirmação", quote)
        self.assertNotIn("routine-activity-complete", quote)
        self.assertIn("routine-step-number", quote)
        self.assertIn("routine-step-vertical", quote)
        self.assertIn("routine-activity-complete", generic)

    @staticmethod
    def quote_snapshot():
        early, middle, late = "NTN-B 15/08/2028", "NTN-B 15/08/2030", "NTN-B 15/08/2035"
        vertices = [{"NM_SECURITY": late, "DT_MATURITY": "2035-08-15"},
                    {"NM_SECURITY": early, "DT_MATURITY": "2028-08-15"},
                    {"NM_SECURITY": middle, "DT_MATURITY": "2030-08-15"}]
        maturities = {item["NM_SECURITY"]: item["DT_MATURITY"] for item in vertices}
        runs = []
        for execution_id, quotes in (
            (101, [(late, "Ask", .074), (early, "Bid", .07), (middle, "Bid", .071),
                   (late, "Bid", .072), (early, "Ask", .073)]),
            (102, [(early, "Bid", 0), (late, "Bid", .079), (middle, "Ask", .08)]),
        ):
            rows = [{"ID_EXECUTION": execution_id, "TS_EXECUTION": "2026-11-04T09:15:00-03:00",
                     "NM_BROKER": "XP", "NM_SECURITY": name, "DT_MATURITY": maturities[name],
                     "SIDE": side, "TX_QUOTE": rate, "TS_REF": "2026-11-04T09:14:00-03:00"}
                    for name, side, rate in quotes]
            runs.append({"id_execution": execution_id, "label": "09:15", "source": "XP", "username": "Alice", "rows": rows})
        return {"rows": [row for run in runs for row in run["rows"]], "runs": runs, "vertices": vertices,
                "summary": {"run_count": 2, "quote_count": 8, "security_count": 3}}

    def test_quote_chart_orders_maturities_and_draws_one_bid_ask_pair_per_run(self):
        snapshot = self.quote_snapshot()
        figure = self.page.quote_run_figure(snapshot, revision="NTNB_RUN:2026-11-04").to_plotly_json()
        traces = figure["data"]
        self.assertEqual(len(traces), 4)
        self.assertEqual(figure["layout"]["xaxis"]["categoryarray"],
                         ["NTN-B 15/08/2028", "NTN-B 15/08/2030", "NTN-B 15/08/2035"])
        self.assertEqual(figure["layout"]["yaxis"]["ticksuffix"], "%")
        self.assertEqual(figure["layout"]["yaxis"]["tickformat"], ".2f")
        self.assertEqual(figure["layout"]["uirevision"], "NTNB_RUN:2026-11-04")
        self.assertEqual(figure["layout"]["legend"]["groupclick"], "togglegroup")
        for offset, execution_id in ((0, 101), (2, 102)):
            bid, ask = traces[offset:offset + 2]
            self.assertIn(f"#{execution_id}", bid["name"])
            self.assertIn("09:15", bid["name"])
            self.assertTrue(bid["name"].endswith("Bid"))
            self.assertTrue(ask["name"].endswith("Ask"))
            self.assertEqual(bid["line"]["color"], ask["line"]["color"])
            self.assertEqual(bid["legendgroup"], ask["legendgroup"])
            self.assertEqual(bid["line"]["dash"], "solid")
            self.assertEqual(ask["line"]["dash"], "dash")
            self.assertFalse(bid["connectgaps"])
            self.assertFalse(ask["connectgaps"])
        self.assertNotEqual(traces[0]["line"]["color"], traces[2]["line"]["color"])
        self.assertNotEqual(traces[0]["legendgroup"], traces[2]["legendgroup"])
        for actual, expected in zip(traces[0]["y"], [7, 7.1, 7.2]):
            self.assertAlmostEqual(actual, expected)
        self.assertIsNone(traces[1]["y"][1])
        self.assertEqual(traces[2]["y"][0], 0)
        self.assertEqual(traces[3]["y"], [None, 8, None])

    def test_missing_quote_side_keeps_an_empty_trace_without_estimated_rates(self):
        snapshot = self.quote_snapshot()
        snapshot["runs"][1]["rows"] = [row for row in snapshot["runs"][1]["rows"] if row["SIDE"] == "Bid"]
        traces = self.page.quote_run_figure(snapshot).to_plotly_json()["data"]
        self.assertEqual(len(traces), 4)
        self.assertEqual(traces[3]["y"], [None, None, None])
        self.assertFalse(traces[3]["connectgaps"])

    def test_duplicate_vertex_quotes_remain_individual_observations_without_averaging(self):
        snapshot = self.quote_snapshot()
        duplicate = {**snapshot["runs"][0]["rows"][1], "TX_QUOTE": .075}
        snapshot["runs"][0]["rows"].append(duplicate)
        trace = self.page.quote_run_figure(snapshot).to_plotly_json()["data"][0]
        early_values = [value for name, value in zip(trace["x"], trace["y"]) if name == duplicate["NM_SECURITY"]]
        self.assertEqual(len(early_values), 2)
        for actual, expected in zip(early_values, [7, 7.5]):
            self.assertAlmostEqual(actual, expected)
        self.assertIn(None, trace["x"])
        self.assertFalse(trace["connectgaps"])

    def test_quote_poll_uses_confirmed_snapshot_for_table_chart_and_counts(self):
        snapshot = self.quote_snapshot()
        key = ["NTNB_RUN", "Alice", self.page.service().today(), "NTNB_RUN:DEFAULT"]
        with patch.object(self.page.RoutineService, "get_daily_quote_runs", return_value=snapshot) as daily:
            with patch.object(self.page.RoutineService, "get_operational_data") as query:
                response = self.dispatch("routine-timeline.children", {
                    "routine-poll.n_intervals": 2, "routine-view-key.data": key,
                }, ["routine-poll.n_intervals"])
                query.assert_not_called()
            daily.assert_called_once_with("NTNB_RUN")
        self.assertEqual(response["routine-quote-data"]["rowData"], snapshot["rows"])
        self.assertEqual(len(response["routine-quote-chart"]["figure"]["data"]), 4)
        self.assertIn("runs confirmadas", json.dumps(response["routine-quote-counts"], ensure_ascii=False))
        self.assertNotIn("routine-view-key", response)
        self.assertNotIn("routine-chat", response)

        rate_column = next(item for item in response["routine-quote-data"]["columnDefs"] if item["field"] == "TX_QUOTE")
        self.assertIn("* 100", rate_column["valueFormatter"]["function"])

    def add_activity(self, activity_id="review-only", *, routine="NTNB_RUN", tools=None, config=None):
        engine = self.page.service()
        engine.configure_activity(routine, activity_id, "Review activity", prompt_key="NTNB_RUN",
            tools=[] if tools is None else tools, config={**engine.load_activity(routine,routine+":DEFAULT")["CONFIG_JSON"],**(config or {"workflow":"assistant"})})

        def cleanup():
            with closing(sqlite3.connect(self.db)) as conn, conn:
                for table in ("UGM_ROUTINE_CHAT", "UGM_ROUTINE_EXECUTIONS", "UGM_ROUTINE_ACTIVITY_TOOLS", "UGM_ROUTINE_ACTIVITIES"):
                    conn.execute(f"DELETE FROM {table} WHERE ID_ACTIVITY=?", (activity_id,))
        self.addCleanup(cleanup)
        return {"routine": routine, "activity": activity_id}

    def test_workspace_requires_explicit_activity_selection_and_rejects_stale_routine(self):
        for selection in (None, {"routine": "NTNF_RUN", "activity": "NTNF_RUN:DEFAULT"}):
            view = self.dispatch("routine-title.children", {"routine-activity-selected.data": selection})
            self.assertEqual(view["routine-activity-content"]["style"], {"display": "none"})
            self.assertEqual(view["routine-activity-empty"]["style"], {})
            with patch.object(self.page.RoutineService, "process_routine_message") as process:
                reply = self.dispatch("routine-chat-feedback.children", {
                    "routine-activity-selected.data": selection, "routine-send.n_clicks": 1,
                    "routine-chat-text.value": "Import this",
                }, ["routine-send.n_clicks"])
                process.assert_not_called()
            self.assertIn("Selecione uma atividade", reply["routine-chat-feedback"]["children"])

    def test_activity_menu_displays_schedule_and_selection_changes_on_click(self):
        selection = self.add_activity(tools=["save_quotes"], config={"workflow": "quote_import"})
        engine = self.page.service()
        engine.configure_activity_schedule("NTNB_RUN", "review-only", deadline="12:30", recurrence="DAILY", completion_action="save_quotes")
        menu = self.dispatch("routine-activity-menu.children", {"routine-activity-selected.data": selection})
        encoded = json.dumps(menu, ensure_ascii=False)
        self.assertIn("Review activity", encoded)
        self.assertIn("12:30", encoded)
        self.assertIn("routine-activity-selected", encoded)
        button = {"type": "routine-activity-select", "routine": "NTNB_RUN", "activity": "review-only"}
        all_buttons = self.pattern({"type": "routine-activity-select", "routine": ["ALL"], "activity": ["ALL"]})
        mounted = self.dispatch("routine-activity-selected.data", {
            "routine-activity-selected.data": None, all_buttons + ".n_clicks": [0, 0],
        }, [all_buttons + ".n_clicks"])
        self.assertEqual(mounted, {})
        changed = self.dispatch("routine-activity-selected.data", {
            "routine-activity-selected.data": None, all_buttons + ".n_clicks": [0, 1],
        }, [self.pattern(button) + ".n_clicks"])
        self.assertEqual(changed["routine-activity-selected"]["data"], selection)
        reset = self.dispatch("routine-activity-selected.data", {
            "routine-selected.data": "NTNF_RUN", "routine-activity-selected.data": selection,
        }, ["routine-selected.data"])
        self.assertIsNone(reset["routine-activity-selected"]["data"])

    def test_activity_tool_selection_controls_panels_and_history(self):
        selection = self.add_activity()
        original = self.prepare()
        view = self.dispatch("routine-title.children", {"routine-activity-selected.data": selection})
        self.assertEqual(view["routine-activity-title"]["children"], "Review activity")
        self.assertEqual(view["routine-import-panel"]["style"], {"display": "none"})
        self.assertEqual(view["routine-data-panel"]["style"], {"display": "none"})
        chat = self.dispatch("routine-chat.children", {"routine-activity-selected.data": selection})
        self.assertNotIn("routine-card-confirm", json.dumps(chat))
        component = lambda kind: {"type": kind, "execution": original["ID_EXECUTION"]}
        denied = self.dispatch("routine-card-feedback", {
            "routine-activity-selected.data": selection,
            self.pattern(component("routine-card-confirm")) + ".n_clicks": 1,
            self.pattern(component("routine-card-grid")) + ".rowData": original["INPUT_JSON"]["rows"],
            self.pattern(component("routine-card-grid")) + ".id": component("routine-card-grid"),
        }, [self.pattern(component("routine-card-confirm")) + ".n_clicks"], execution=original["ID_EXECUTION"])
        self.assertIn("não está disponível", denied[self.pattern(component("routine-card-feedback"))]["children"])
        self.assertEqual(self.quote_rows(), [])

    def test_activity_switch_clears_unsent_inputs_and_deep_links_select_only_valid_activity(self):
        from plotly.utils import PlotlyJSONEncoder
        selection = self.add_activity()
        response = self.dispatch("routine-import-text.value", {"routine-activity-selected.data": selection},
                                 ["routine-activity-selected.data"])
        self.assertEqual(response["routine-chat-text"]["value"], "")
        self.assertIsNone(response["routine-upload"]["contents"])

        def stores(routine, activity):
            tree = json.loads(json.dumps(self.page.layout(routine=routine, activity=activity), cls=PlotlyJSONEncoder))
            return next(node["props"]["data"] for node in tree["props"]["children"]
                        if node.get("props", {}).get("id") == "routine-activity-selected")
        self.assertIsNone(stores("NTNB_RUN", None))
        self.assertEqual(stores("NTNB_RUN", "review-only"), selection)
        self.assertIsNone(stores("NTNF_RUN", "review-only"))

    def test_daily_summary_adds_exactly_one_pair_only_after_each_confirmation(self):
        self.add_activity("second-run", tools=["save_quotes"], config={"workflow": "quote_import"})
        self.add_activity("third-run", tools=["save_quotes"], config={"workflow": "quote_import"})
        first = self.prepare()
        before = self.dispatch("routine-timeline.children")
        self.assertEqual(before["routine-quote-data"]["rowData"], [])
        self.assertEqual(before["routine-quote-chart"]["figure"]["data"], [])
        self.card_action(first)
        second = self.prepare(activity_id="second-run")
        self.card_action(second)
        self.prepare(activity_id="third-run")  # Pending activities stay outside the comparison.
        after = self.dispatch("routine-timeline.children")
        rows = after["routine-quote-data"]["rowData"]
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["ID_EXECUTION"] for row in rows}, {first["ID_EXECUTION"], second["ID_EXECUTION"]})
        self.assertEqual(len(after["routine-quote-chart"]["figure"]["data"]), 4)


if __name__ == "__main__":
    unittest.main()
