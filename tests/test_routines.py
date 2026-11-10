"""Exercise the shared routine engine against an isolated, real SQLite schema.

No production records, provider credentials, or live network services are used.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from routine_test_support import single_ntnb_activity
from services.prompts import PromptStore
from services.openai_client import AIError, ConversationResult, OpenAIService, StructuredResult
from services.routines import RoutineService
from services.routine_tools import parse_import


TZ = ZoneInfo("America/Sao_Paulo")


class RoutineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "routines.db"
        # Match the actual broker/classification column names and constraints.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE FIXED_INCOME_SECURITY_CLASSIFICATION (
                    ID_SECURITY INTEGER PRIMARY KEY,
                    NM_SECURITY TEXT NOT NULL UNIQUE,
                    NM_ISSUER TEXT, NM_INDEX TEXT, DT_MATURITY DATE,
                    NM_INSTRUMENT_TYPE TEXT, NM_RATING TEXT, NM_SECTOR TEXT,
                    DT_INPUT DATE, USR_INPUT TEXT
                );
                INSERT INTO FIXED_INCOME_SECURITY_CLASSIFICATION
                    (ID_SECURITY, NM_SECURITY, NM_ISSUER, NM_INDEX,
                     DT_MATURITY, NM_INSTRUMENT_TYPE, NM_SECTOR)
                VALUES
                    (101, 'NTN-B May-27', 'Treasury', 'IPCA', '2027-05-15', 'NTN-B', 'Sovereign'),
                    (102, 'NTN-F Jan-29', 'Treasury', 'PRE', '2029-01-01', 'NTN-F', 'Sovereign'),
                    (103, 'TEST11', 'Test issuer', 'CDI', '2030-01-01', 'Debenture', 'Utilities');
                CREATE TABLE FIXED_INCOME_BROKER_DATA (
                    DT_REF DATE, ID_SECURITY INTEGER, NM_SECURITY TEXT,
                    NM_SOURCE TEXT, NM_BROKER TEXT, SIDE TEXT,
                    TX_QUOTE REAL, VL_QUOTE REAL, DT_INPUT DATE, USR_INPUT TEXT
                );
                CREATE INDEX idx_fix_broker_security_ref
                    ON FIXED_INCOME_BROKER_DATA (NM_SECURITY, DT_REF DESC);
                INSERT INTO FIXED_INCOME_BROKER_DATA VALUES
                    ('2026-01-01', 101, 'NTN-B May-27', 'legacy', 'XP',
                     'Bid', 0.07, NULL, '2026-01-01', 'Existing user');
            """)
        self.now = datetime(2026, 11, 3, 10, 0, tzinfo=TZ)
        self.service = RoutineService(self.db, clock=lambda: self.now)
        single_ntnb_activity(self.service)

    def execute(self, sql, args=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def scalar(self, sql, args=()):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, args).fetchone()[0]

    def audit_rows(self):
        return self.execute("SELECT * FROM AI_REQUEST_LOG ORDER BY LOG_ID")

    def assert_chat_has_no_usage_columns(self, rows):
        columns = {row["name"] for row in self.execute("PRAGMA table_info(UGM_ROUTINE_CHAT)")}
        for column in ("INPUT_TOKENS", "OUTPUT_TOKENS", "TOTAL_TOKENS"):
            self.assertNotIn(column, columns)
            self.assertTrue(all(column not in row for row in rows))

    def quotes(self, **overrides):
        row = {"NM_SECURITY": "NTN-B May-27", "SIDE": "Bid", "TX_QUOTE": 0.075}
        row.update(overrides)
        return {"source": "XP", "rows": [row]}

    def proposal(self, **kwargs):
        kwargs.setdefault("activity_id","NTNB_RUN:DEFAULT")
        return self.service.prepare_action("NTNB_RUN","Alice","save_quotes",self.quotes(),**kwargs)

    def schedule(self, step="test-morning", routine="NTNB_RUN", deadline="11:00", recurrence="WEEKDAYS", action="save_quotes"):
        activity_id=f"{routine}:DEFAULT"
        base=self.service.load_activity(routine,activity_id)
        if base['RECURRENCE']!='MANUAL' or base['DEADLINE_TIME'] is not None:
            activity_id=f"{routine}:{step}"
        tools=[row['NM_TOOL'] for row in self.service.load_routine_tools(routine,base['ID_ACTIVITY'])]
        action='manual_complete' if action=='MANUAL' else action
        self.service.configure_activity(routine,activity_id,step,prompt_key=routine,tools=tools,
            config=base['CONFIG_JSON'],deadline=deadline,recurrence=recurrence,completion_action=action)
        return activity_id

    def timeline_step(self, step):
        return next(row for row in self.service.get_routine_timeline("NTNB_RUN") if row['ID_ACTIVITY']==step or row['NM_ACTIVITY']==step)

    def test_migration_preserves_existing_rows_and_admin_edits(self):
        self.execute("UPDATE UGM_ROUTINES SET NM_ROUTINE='Custom name', FL_ACTIVE=0 WHERE ID_ROUTINE='NTNB_RUN'")
        self.service.ensure_schema()
        self.service.ensure_schema()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINES"), 4)
        self.assertEqual(self.scalar("SELECT NM_ROUTINE FROM UGM_ROUTINES WHERE ID_ROUTINE='NTNB_RUN'"), "Custom name")
        self.assertEqual(self.scalar("SELECT FL_ACTIVE FROM UGM_ROUTINES WHERE ID_ROUTINE='NTNB_RUN'"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)
        self.assertEqual(self.scalar("SELECT TX_QUOTE FROM FIXED_INCOME_BROKER_DATA"), 0.07)

    def test_only_configured_active_tools_are_loaded(self):
        tools = self.service.load_routine_tools("NTNB_RUN")
        self.assertIn("save_quotes", [row["NM_TOOL"] for row in tools])
        self.assertNotIn("update_debtor", [row["NM_TOOL"] for row in tools])
        self.execute("UPDATE UGM_ROUTINE_ACTIVITY_TOOLS SET FL_ACTIVE=0 WHERE ID_ACTIVITY='NTNB_RUN:DEFAULT' AND NM_TOOL='save_quotes'")
        self.assertNotIn("save_quotes", [row["NM_TOOL"] for row in self.service.load_routine_tools("NTNB_RUN")])
        with self.assertRaises((ValueError, LookupError)):
            self.proposal()

    def test_legacy_debtors_table_is_renamed_without_losing_history(self):
        self.execute("ALTER TABLE UGM_ROUTINES_DEVEDORES RENAME TO UGM_DEVEDORES")
        cases = [
            ("2026-11-02", "00123", 2, "GWM", "Histórico anterior\nInformação preservada", 0),
            ("2026-11-03", "00123", 3, "GWM", "Ainda devedor", 1),
        ]
        for case in cases:
            self.execute("INSERT INTO UGM_DEVEDORES VALUES (?,?,?,?,?,?)", case)
        self.service.ensure_schema()
        self.service.ensure_schema()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='UGM_DEVEDORES'"), 0)
        rows = self.execute("SELECT * FROM UGM_ROUTINES_DEVEDORES ORDER BY DT_REF")
        columns = ("DT_REF", "ID_CLIENT", "NR_DAYS", "NM_RESP_GWM", "DESCR", "FL_RESOLVED")
        self.assertEqual([tuple(row[column] for column in columns) for row in rows], cases)
        shared = self.service.get_operational_data("DEBTORS")
        self.assertEqual(shared["source"], "UGM_ROUTINES_DEVEDORES")
        self.assertEqual(shared["rows"][0]["ID_CLIENT"], "00123")
        self.assertEqual(shared["rows"][0]["FL_RESOLVED"], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute("INSERT INTO UGM_ROUTINES_DEVEDORES VALUES (?,?,?,?,?,?)", cases[1])

    def test_debtors_rename_conflict_preserves_both_tables(self):
        self.execute("INSERT INTO UGM_ROUTINES_DEVEDORES VALUES ('2026-11-03','00123',1,'GWM','Current case',1)")
        self.execute("CREATE TABLE UGM_DEVEDORES AS SELECT * FROM UGM_ROUTINES_DEVEDORES")
        self.execute("UPDATE UGM_DEVEDORES SET DESCR='Legacy case'")
        with self.assertRaisesRegex(ValueError, "Both UGM_DEVEDORES and UGM_ROUTINES_DEVEDORES exist"):
            self.service.ensure_schema()
        self.assertEqual(self.scalar("SELECT DESCR FROM UGM_DEVEDORES"), "Legacy case")
        self.assertEqual(self.scalar("SELECT DESCR FROM UGM_ROUTINES_DEVEDORES"), "Current case")

    def test_model_write_requires_confirmation_even_with_auto_policy(self):
        self.execute("UPDATE UGM_ROUTINE_ACTIVITY_TOOLS SET REVIEW_POLICY='AUTO' WHERE ID_ACTIVITY='NTNB_RUN:DEFAULT' AND NM_TOOL='save_quotes'")
        proposal = self.proposal()
        self.assertEqual(proposal["STATUS"], "PENDING_REVIEW")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)
        self.assertEqual(self.scalar("SELECT STATUS FROM UGM_ROUTINE_EXECUTIONS WHERE ID_EXECUTION=?", (proposal["ID_EXECUTION"],)), "PENDING_REVIEW")

    def test_confirm_uses_edited_rows_and_records_success(self):
        proposal = self.proposal()
        edited = self.quotes(TX_QUOTE=0.081)
        result = self.service.confirm_action(proposal["ID_EXECUTION"], "Alice", edited_args=edited, routine_id="NTNB_RUN")
        self.assertEqual(result["STATUS"], "SUCCESS")
        rows = self.execute("SELECT * FROM FIXED_INCOME_BROKER_DATA WHERE USR_INPUT='Alice'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["TX_QUOTE"], 0.081)
        self.assertEqual(rows[0]["DT_REF"], "2026-11-03")
        self.assertIsNotNone(result["TS_EXECUTION"])

    def test_invalid_edited_row_does_not_write_any_records(self):
        proposal = self.proposal()
        invalid = {"source": "XP", "rows": [self.quotes()["rows"][0], {"NM_SECURITY": "Unknown", "SIDE": "Bid", "TX_QUOTE": 0.08}]}
        with self.assertRaises(ValueError):
            self.service.confirm_action(proposal["ID_EXECUTION"], "Alice", edited_args=invalid)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)

    def test_other_user_or_routine_cannot_confirm_or_cancel_proposal(self):
        proposal = self.proposal()
        for operation in (self.service.confirm_action, self.service.cancel_action):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises((ValueError, PermissionError, LookupError)):
                    operation(proposal["ID_EXECUTION"], "Bob")
                with self.assertRaises((ValueError, PermissionError, LookupError)):
                    operation(proposal["ID_EXECUTION"], "Alice", routine_id="DEBTORS")
        self.assertEqual(self.scalar("SELECT STATUS FROM UGM_ROUTINE_EXECUTIONS WHERE ID_EXECUTION=?", (proposal["ID_EXECUTION"],)), "PENDING_REVIEW")

    def test_repeated_confirmation_does_not_duplicate_an_import(self):
        proposal = self.proposal()
        self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        try:
            self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        except ValueError:
            pass
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA WHERE USR_INPUT='Alice'"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS WHERE STATUS='SUCCESS'"), 1)

    def test_concurrent_confirmation_claims_same_proposal_once(self):
        proposal = self.proposal()
        def confirm(_):
            try:
                service = RoutineService(self.db, clock=lambda: self.now)
                return service.confirm_action(proposal["ID_EXECUTION"], "Alice")["STATUS"]
            except ValueError:
                return "already_processed"
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(confirm, range(2)))
        self.assertIn("SUCCESS", statuses)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA WHERE USR_INPUT='Alice'"), 1)

    def test_cancelled_proposal_cannot_execute(self):
        proposal = self.proposal()
        result = self.service.cancel_action(proposal["ID_EXECUTION"], "Alice")
        self.assertEqual(result["STATUS"], "CANCELLED")
        confirmed = self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        self.assertEqual(confirmed["STATUS"], "CANCELLED")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)

    def test_separate_intraday_proposals_preserve_multiple_quotes(self):
        first = self.proposal()
        self.service.confirm_action(first["ID_EXECUTION"], "Alice")
        self.now += timedelta(hours=1)
        self.activity("second-import")
        second = self.proposal(activity_id="second-import")
        self.service.confirm_action(second["ID_EXECUTION"], "Alice")
        rows = self.execute("SELECT * FROM FIXED_INCOME_BROKER_DATA WHERE USR_INPUT='Alice'")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["TS_REF"], rows[1]["TS_REF"])
        self.assertEqual(rows[0]["DT_INPUT"], "2026-11-03")
        self.assertEqual(rows[1]["DT_INPUT"], "2026-11-03")

    def test_sql_failure_rolls_back_whole_batch_and_persists_failed_execution(self):
        proposal = self.proposal()
        self.execute("""CREATE TRIGGER reject_new_quote BEFORE INSERT ON FIXED_INCOME_BROKER_DATA
            WHEN NEW.USR_INPUT = 'Alice' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        try:
            result = self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        except (ValueError, sqlite3.Error):
            result = self.execute("SELECT * FROM UGM_ROUTINE_EXECUTIONS WHERE ID_EXECUTION=?", (proposal["ID_EXECUTION"],))[0]
        self.assertEqual(result["STATUS"], "FAILED")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)
        self.assertTrue(result["ERROR_MESSAGE"])

    def test_timeline_pending_overdue_and_exact_step_completion(self):
        morning = self.schedule("test-morning", deadline="11:00")
        afternoon = self.schedule("test-afternoon", deadline="15:00")
        self.assertEqual(self.timeline_step(morning)["state"], "PENDING")
        proposal = self.proposal(activity_id=morning)
        self.assertEqual(proposal["STATUS"], "PENDING_REVIEW")
        self.assertIsNone(self.timeline_step(morning)["completed_at"])
        self.assertEqual(self.timeline_step(morning)["progress"], "PENDING_REVIEW")
        self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        self.assertEqual(self.timeline_step(morning)["state"], "ON_TIME")
        self.assertEqual(self.timeline_step(morning)["completed_by"], "Alice")
        self.assertIsNone(self.timeline_step(morning)["progress"])
        self.assertEqual(self.timeline_step(afternoon)["state"], "PENDING")
        self.now = self.now.replace(hour=16)
        self.assertEqual(self.timeline_step(afternoon)["state"], "OVERDUE")
        late = self.proposal(activity_id=afternoon)
        self.service.confirm_action(late["ID_EXECUTION"], "Alice")
        self.assertEqual(self.timeline_step(afternoon)["state"], "LATE")

    def test_automatic_import_binding_completes_only_next_scheduled_step(self):
        morning = self.schedule("test-morning", deadline="11:00")
        afternoon = self.schedule("test-afternoon", deadline="15:00")
        proposal = self.proposal()
        self.assertEqual(proposal["ID_ACTIVITY"], morning)
        self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        self.assertEqual(self.timeline_step(morning)["state"], "ON_TIME")
        self.assertEqual(self.timeline_step(afternoon)["state"], "PENDING")

    def test_unknown_or_other_routine_step_is_rejected(self):
        wrong_step = self.schedule("debtors-only", routine="DEBTORS", action="import_debtors")
        for step in ("nonexistent-step", wrong_step):
            with self.subTest(step=step):
                with self.assertRaises((ValueError, LookupError)):
                    self.proposal(activity_id=step)

    def test_manual_completion_is_shared_and_registered(self):
        step = self.schedule("test-manual", routine="DEBTORS", recurrence="MANUAL", action="MANUAL")
        result = self.service.complete_activity("DEBTORS", step, "Bob")
        self.assertEqual(result["STATUS"], "SUCCESS")
        timeline = next(row for row in self.service.get_routine_timeline("DEBTORS") if row["ID_ACTIVITY"] == step)
        self.assertEqual(timeline["completed_by"], "Bob")
        self.assertEqual(timeline["state"], "ON_TIME")
        persisted = self.execute("SELECT * FROM UGM_ROUTINE_EXECUTIONS WHERE ID_ACTIVITY=?", (step,))
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["USERNAME"], "Bob")

    def test_weekdays_skip_weekend_but_daily_and_manual_remain(self):
        weekday = self.schedule("test-weekday", recurrence="WEEKDAYS")
        daily = self.schedule("test-daily", recurrence="DAILY")
        manual = self.schedule("test-manual", recurrence="MANUAL", action="MANUAL")
        self.now = datetime(2026, 11, 7, 10, 0, tzinfo=TZ)  # Saturday.
        steps = {row["ID_ACTIVITY"] for row in self.service.get_routine_timeline("NTNB_RUN")}
        self.assertNotIn(weekday, steps)
        self.assertIn(daily, steps)
        self.assertIn(manual, steps)

    def test_operational_day_uses_sao_paulo_instead_of_utc_date(self):
        utc = ZoneInfo("UTC")
        self.now = datetime(2026, 11, 4, 1, 0, tzinfo=utc)  # Still November 3 in São Paulo.
        proposal = self.proposal()
        self.assertEqual(proposal["DT_REF"], "2026-11-03")
        self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        self.assertEqual(self.scalar("SELECT DT_REF FROM FIXED_INCOME_BROKER_DATA WHERE USR_INPUT='Alice'"), "2026-11-03")

    def test_yesterdays_pending_proposal_does_not_write_todays_data(self):
        proposal = self.proposal()
        self.now += timedelta(days=1)
        with self.assertRaises((ValueError, PermissionError)):
            self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)

    def seed_chat(self, routine="DEBTORS"):
        PromptStore(self.db).seed("routines", routine, "Configured routine instructions", "configured-routine-model")
        ai = Mock()
        self.service.ai_service = ai
        return ai

    @staticmethod
    def assistant_response(text="Response", **usage):
        return ConversationResult([
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
        ], text, "response-test", "configured-routine-model", **usage)

    @staticmethod
    def canonical_run(security="NTN-B May-27", rate="0.075"):
        return f"NM_SECURITY\tSIDE\tTX_QUOTE\n{security}\tBid\t{rate}\n"

    @staticmethod
    def api_response(text="", *, items=None, usage=None, status="completed", response_id="test-response"):
        response = SimpleNamespace(status=status, output=items or [], output_text=text,
                                   id=response_id, model="configured-routine-model")
        if usage is not None:
            response.usage = usage
        return response

    def mock_api(self, responses):
        client = Mock()
        client.responses.create.side_effect = responses
        self.service.ai_service = OpenAIService(client=client, database_path=self.db)
        return client

    def test_interaction_history_is_persistent_and_scoped_by_user_routine_day(self):
        messages = [
            ("NTNB_RUN", "Alice", "2026-11-03", "Alice today", "Alice's reply"),
            ("NTNB_RUN", "Alice", "2026-11-02", "Alice yesterday", "Archived reply"),
            ("NTNB_RUN", "Bob", "2026-11-03", "Bob today", "Bob's reply"),
            ("DEBTORS", "Alice", "2026-11-03", "Other routine", "Debtors reply"),
        ]
        for routine, username, day, request, reply in messages:
            self.execute("""INSERT INTO UGM_ROUTINE_CHAT
                (ID_ROUTINE,ID_ACTIVITY,USERNAME,DT_REF,TS_MESSAGE,TS_RESPONSE,USER_MESSAGE,ASSISTANT_MESSAGE,STATUS)
                VALUES(?,?,?,?,?,?,?,?,'COMPLETED')""",
                (routine, routine+":DEFAULT", username, day, day + "T10:00:00-03:00", day + "T10:00:01-03:00", request, reply))
        reloaded = RoutineService(self.db, clock=lambda: self.now)
        rows = reloaded.load_chat_history("NTNB_RUN", "Alice")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["USER_MESSAGE"], "Alice today")
        self.assertEqual(rows[0]["ASSISTANT_MESSAGE"], "Alice's reply")
        self.assertEqual(reloaded.load_chat_history("NTNB_RUN", "Bob")[0]["USER_MESSAGE"], "Bob today")
        self.assertEqual(reloaded.load_chat_history("DEBTORS", "Alice")[0]["USER_MESSAGE"], "Other routine")
        self.now += timedelta(days=1)
        self.assertEqual(reloaded.load_chat_history("NTNB_RUN", "Alice"), [])
        self.assertEqual(len(reloaded.load_chat_history("NTNB_RUN", "Alice", dt_ref="2026-11-03")), 1)

    def test_direct_actions_across_routines_use_one_consolidated_human_log(self):
        quote = self.proposal()
        report = self.service.prepare_action("DEBTORS", "Bob", "debtor_report", {})
        rows = self.execute("SELECT * FROM UGM_ROUTINE_CHAT ORDER BY ID_MESSAGE")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["ID_ROUTINE"] for row in rows}, {"NTNB_RUN", "DEBTORS"})
        self.assertEqual({row["ID_EXECUTION"] for row in rows}, {quote["ID_EXECUTION"], report["ID_EXECUTION"]})
        self.assertTrue(all(isinstance(row["USER_MESSAGE"], str) and isinstance(row["ASSISTANT_MESSAGE"], str) for row in rows))
        self.assertTrue(all("ROLE" not in row and "CONTENT_JSON" not in row for row in rows))
        self.assertEqual(len(self.service.load_chat_history("NTNB_RUN", "Alice")), 1)
        self.assertEqual(len(self.service.load_chat_history("DEBTORS", "Bob")), 1)
        self.assertEqual(self.service.load_chat_history("NTNB_RUN", "Bob"), [])
        self.assertEqual(self.service.load_chat_history("DEBTORS", "Alice"), [])

    def test_general_tool_loop_audits_each_request_once_and_keeps_chat_human_readable(self):
        self.seed_chat()
        call = {"type": "function_call", "call_id": "local-query-call", "name": "query_debtors", "arguments": "{}"}
        client = self.mock_api([
            self.api_response(items=[
                {"type": "reasoning", "id": "opaque-reasoning-id", "summary": [], "encrypted_content": "private-opaque-content"}, call,
            ], usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}, response_id="query-response"),
            self.api_response("Não há clientes cadastrados hoje.", items=self.assistant_response("Não há clientes cadastrados hoje.").items,
                              usage={"input_tokens": 200, "output_tokens": 20, "total_tokens": 220}),
            self.api_response("Próxima interação", items=self.assistant_response("Próxima interação").items),
        ])
        self.service.process_routine_message("DEBTORS", "Alice", "Liste os clientes devedores")
        history = self.service.load_chat_history("DEBTORS", "Alice")
        self.assertEqual(len(history), 1)
        interaction = history[0]
        self.assertEqual(interaction["USER_MESSAGE"], "Liste os clientes devedores")
        self.assertIn("Não há clientes", interaction["ASSISTANT_MESSAGE"])
        self.assertEqual(interaction["STATUS"], "COMPLETED")
        self.assert_chat_has_no_usage_columns(history)
        audit = self.audit_rows()
        self.assertEqual(len(audit), 2)
        self.assertEqual([(row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]) for row in audit], [(100, 10, 110), (200, 20, 220)])
        self.assertTrue(all(row["RECORD_TYPE"] == "REQUEST" and row["CHAT_MESSAGE_ID"] == interaction["ID_MESSAGE"] for row in audit))
        self.assertTrue(all(row["FEATURE"] == "routines" and row["FORM_TYPE"] == "DEBTORS" and row["USERNAME"] == "Alice" for row in audit))
        self.assertTrue(all(row["STATUS"] == "completed" and row["FULL_PROMPT"] is not None for row in audit))
        self.assertIsNotNone(interaction["TS_RESPONSE"])
        self.assertNotIn("ROLE", interaction)
        self.assertNotIn("CONTENT_JSON", interaction)
        for private in ("local-query-call", "opaque-reasoning-id", "private-opaque-content", "function_call_output"):
            self.assertNotIn(private, json.dumps(history))
        current_turn = client.responses.create.call_args_list[1].kwargs["input"]
        call_index = current_turn.index(call)
        self.assertEqual(current_turn[call_index + 1]["type"], "function_call_output")
        self.assertEqual(current_turn[call_index + 1]["call_id"], call["call_id"])
        self.service.process_routine_message("DEBTORS", "Alice", "Próximo pedido")
        self.assertEqual(len(self.service.load_chat_history("DEBTORS", "Alice")), 2)
        self.assertNotIn("local-query-call", json.dumps(client.responses.create.call_args.kwargs["input"]))
        audit = self.audit_rows()
        self.assertEqual(len(audit), 3)
        self.assertNotEqual(audit[2]["CHAT_MESSAGE_ID"], audit[0]["CHAT_MESSAGE_ID"])

    def test_central_usage_preserves_unknown_and_reported_zero(self):
        self.seed_chat()
        self.mock_api([
            self.api_response("Uso indisponível", items=self.assistant_response("Uso indisponível").items),
            self.api_response("Uso igual a zero", items=self.assistant_response("Uso igual a zero").items,
                              usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
        ])
        self.service.process_routine_message("DEBTORS", "Alice", "Primeira interação")
        self.service.process_routine_message("DEBTORS", "Alice", "Segunda interação")
        rows = self.service.load_chat_history("DEBTORS", "Alice")
        self.assert_chat_has_no_usage_columns(rows)
        self.assertEqual([(row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]) for row in self.audit_rows()],
                         [(None, None, None), (0, 0, 0)])

    def test_partial_api_usage_remains_per_request_without_inventing_total(self):
        self.seed_chat()
        self.mock_api([
            self.api_response(items=[{"type": "function_call", "call_id": "partial-usage-call", "name": "query_debtors", "arguments": "{}"}],
                              usage={"input_tokens": 10}, response_id="query-response"),
            self.api_response("Consulta concluída", items=self.assistant_response("Consulta concluída").items, usage={"output_tokens": 3}),
        ])
        self.service.process_routine_message("DEBTORS", "Alice", "Consultar posição")
        history = self.service.load_chat_history("DEBTORS", "Alice")
        self.assertEqual(len(history), 1)
        self.assert_chat_has_no_usage_columns(history)
        self.assertEqual([(row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]) for row in self.audit_rows()],
                         [(10, None, None), (None, 3, None)])

    def test_empty_general_response_keeps_usage_only_in_central_audit(self):
        self.seed_chat()
        self.mock_api([self.api_response(usage={"input_tokens": 14, "output_tokens": 2, "total_tokens": 16}, response_id="empty-response")])
        self.service.process_routine_message("DEBTORS", "Alice", "Exemplo de resposta vazia")
        rows = self.service.load_chat_history("DEBTORS", "Alice")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["STATUS"], "COMPLETED")
        self.assert_chat_has_no_usage_columns(rows)
        audit = self.audit_rows()
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]["INPUT_TOKENS"], audit[0]["OUTPUT_TOKENS"], audit[0]["TOTAL_TOKENS"]), (14, 2, 16))
        self.assertEqual(audit[0]["CHAT_MESSAGE_ID"], rows[0]["ID_MESSAGE"])
        self.assertNotIn("ai_response", json.dumps(rows))

    def test_failed_general_response_audits_usage_and_keeps_chat_sanitized(self):
        self.seed_chat()
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            status="incomplete", output=[{"type": "function_call", "call_id": "partial-private-call", "name": "query_debtors", "arguments": "{}"}],
            output_text="private-partial-output", id="incomplete-response", model="configured-routine-model",
            usage={"input_tokens": 120, "output_tokens": 6, "total_tokens": 126})
        self.service.ai_service = OpenAIService(client=client, database_path=self.db)
        with self.assertRaises(AIError):
            self.service.process_routine_message("DEBTORS", "Alice", "Pedido interrompido")
        rows = self.service.load_chat_history("DEBTORS", "Alice")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["STATUS"], "FAILED")
        self.assert_chat_has_no_usage_columns(rows)
        audit = self.audit_rows()
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]["INPUT_TOKENS"], audit[0]["OUTPUT_TOKENS"], audit[0]["TOTAL_TOKENS"]), (120, 6, 126))
        self.assertEqual(audit[0]["STATUS"], "failed")
        self.assertEqual(audit[0]["CHAT_MESSAGE_ID"], rows[0]["ID_MESSAGE"])
        self.assertNotIn("private-partial-output", json.dumps(rows))
        self.assertNotIn("partial-private-call", json.dumps(rows))

    def test_provider_failure_logs_one_sanitized_interaction_with_unknown_usage(self):
        self.seed_chat()
        client = Mock()
        client.responses.create.side_effect = RuntimeError("private-provider-detail")
        self.service.ai_service = OpenAIService(client=client, database_path=self.db)
        with self.assertRaises(AIError):
            self.service.process_routine_message("DEBTORS", "Alice", "Consulta com provedor indisponível")
        history = self.service.load_chat_history("DEBTORS", "Alice")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["STATUS"], "FAILED")
        self.assert_chat_has_no_usage_columns(history)
        audit = self.audit_rows()
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]["INPUT_TOKENS"], audit[0]["OUTPUT_TOKENS"], audit[0]["TOTAL_TOKENS"]), (None, None, None))
        self.assertEqual(audit[0]["STATUS"], "failed")
        self.assertNotIn("private-provider-detail", json.dumps(audit))
        self.assertNotIn("private-provider-detail", json.dumps(history))

    def test_canonical_quote_import_updates_same_interaction_on_confirmation(self):
        ai = self.seed_chat("NTNB_RUN")
        text = self.canonical_run()
        self.service.process_routine_message("NTNB_RUN", "Alice", text)
        ai.extract.assert_not_called()
        ai.converse.assert_not_called()
        self.assertEqual(self.audit_rows(), [])
        history = self.service.load_chat_history("NTNB_RUN", "Alice")
        self.assertEqual(len(history), 1)
        pending = history[0]
        self.assertEqual(pending["USER_MESSAGE"], text.strip())
        self.assertEqual(pending["STATUS"], "PENDING_REVIEW")
        self.assertIsNotNone(pending["ID_EXECUTION"])
        self.assertTrue(pending["ASSISTANT_MESSAGE"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)
        result = self.service.confirm_action(pending["ID_EXECUTION"], "Alice")
        self.assertEqual(result["STATUS"], "SUCCESS")
        confirmed = self.service.load_chat_history("NTNB_RUN", "Alice")
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["ID_MESSAGE"], pending["ID_MESSAGE"])
        self.assertEqual(confirmed[0]["STATUS"], "SUCCESS")
        self.assertEqual(confirmed[0]["ID_EXECUTION"], pending["ID_EXECUTION"])
        self.service.confirm_action(pending["ID_EXECUTION"], "Alice")
        self.assertEqual(len(self.service.load_chat_history("NTNB_RUN", "Alice")), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA WHERE USR_INPUT='Alice'"), 1)

    def test_canonical_import_cancellation_updates_interaction_without_writing(self):
        self.service.process_routine_message("NTNB_RUN", "Alice", self.canonical_run())
        pending = self.service.load_chat_history("NTNB_RUN", "Alice")[0]
        self.service.cancel_action(pending["ID_EXECUTION"], "Alice")
        history = self.service.load_chat_history("NTNB_RUN", "Alice")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["STATUS"], "CANCELLED")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)

    def test_approval_message_cannot_execute_pending_market_import(self):
        ai = self.seed_chat("NTNB_RUN")
        self.service.process_routine_message("NTNB_RUN", "Alice", self.canonical_run())
        pending = self.service.load_chat_history("NTNB_RUN", "Alice")[0]
        ai.extract.return_value = StructuredResult(
            {"intent": "OUT_OF_SCOPE", "source": None, "clarification_fields": [], "rows": []},
            "configured-routine-model", "approval-message-response", 1, 1)
        self.service.process_routine_message("NTNB_RUN", "Alice", "pode aprovar")
        execution = self.service.get_execution(pending["ID_EXECUTION"], "Alice")
        self.assertEqual(execution["STATUS"], "PENDING_REVIEW")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)
        self.assertEqual(self.service.load_chat_history("NTNB_RUN", "Alice")[0]["STATUS"], "PENDING_REVIEW")

    def test_failed_confirmation_updates_same_interaction_and_preserves_schedule(self):
        step = self.schedule("confirmation-failure", deadline="09:00")
        self.service.process_routine_message("NTNB_RUN", "Alice", self.canonical_run())
        pending = self.service.load_chat_history("NTNB_RUN", "Alice")[0]
        self.execute("""CREATE TRIGGER reject_chat_quote BEFORE INSERT ON FIXED_INCOME_BROKER_DATA
            WHEN NEW.USR_INPUT='Alice' BEGIN SELECT RAISE(ABORT,'private-database-detail'); END""")
        result = self.service.confirm_action(pending["ID_EXECUTION"], "Alice")
        self.assertEqual(result["STATUS"], "FAILED")
        history = self.service.load_chat_history("NTNB_RUN", "Alice")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["ID_MESSAGE"], pending["ID_MESSAGE"])
        self.assertEqual(history[0]["STATUS"], "FAILED")
        self.assertNotIn("private-database-detail", json.dumps(history))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)
        self.assertIsNone(self.timeline_step(step)["completed_at"])

    def test_import_capabilities_are_local_and_cannot_complete_steps(self):
        ai = self.seed_chat("NTNB_RUN")
        self.schedule("capabilities-step", deadline="09:00")
        self.service.process_routine_message("NTNB_RUN", "Alice", "O que você pode fazer?")
        ai.extract.assert_not_called()
        ai.converse.assert_not_called()
        self.assertEqual(self.audit_rows(), [])
        rows = self.service.load_chat_history("NTNB_RUN", "Alice")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ASSISTANT_MESSAGE"])
        self.assertEqual(rows[0]["STATUS"], "COMPLETED")
        self.assertIsNone(rows[0]["ID_EXECUTION"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS"), 0)
        self.assertEqual(self.timeline_step("capabilities-step")["state"], "OVERDUE")

    def test_unstructured_market_import_uses_extraction_and_backend_security_resolution(self):
        self.seed_chat("NTNB_RUN")
        candidate = {"NM_SECURITY": None, "MATURITY_HINT": "2027-05", "SIDE": "Bid", "TX_QUOTE": 0.075,
                     "VL_QUOTE": None, "TS_REF": None, "SPREAD_QUOTE": None}
        extracted = {"intent": "IMPORT", "source": "XP", "clarification_fields": [], "rows": [candidate]}
        client = self.mock_api([self.api_response(json.dumps(extracted),
            usage={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}, response_id="extraction-response")])
        self.service.process_routine_message("NTNB_RUN", "Alice", "XP: NTN-B maio 2027, compra a 7,5%")
        client.responses.create.assert_called_once()
        request = client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "configured-routine-model")
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertNotIn("tools", request)
        history = self.service.load_chat_history("NTNB_RUN", "Alice")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["STATUS"], "PENDING_REVIEW")
        self.assert_chat_has_no_usage_columns(history)
        audit = self.audit_rows()
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]["INPUT_TOKENS"], audit[0]["OUTPUT_TOKENS"], audit[0]["TOTAL_TOKENS"]), (12, 3, 15))
        self.assertEqual((audit[0]["RECORD_TYPE"], audit[0]["CHAT_MESSAGE_ID"], audit[0]["FORM_TYPE"], audit[0]["USERNAME"]),
                         ("REQUEST", history[0]["ID_MESSAGE"], "NTNB_RUN", "Alice"))
        execution = self.service.get_execution(history[0]["ID_EXECUTION"], "Alice")
        self.assertEqual(execution["INPUT_JSON"]["rows"][0]["NM_SECURITY"], "NTN-B May-27")
        self.assertEqual(execution["INPUT_JSON"]["rows"][0]["ID_SECURITY"], 101)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)

    def test_query_and_ambiguity_do_not_execute_or_complete_import_steps(self):
        ai = self.seed_chat("NTNB_RUN")
        step = self.schedule("uncompleted-step", deadline="09:00")
        ai.extract.side_effect = [
            StructuredResult({"intent": "OUT_OF_SCOPE", "source": None, "clarification_fields": [], "rows": []},
                             "configured-routine-model", "outside-response", 1, 1),
            StructuredResult({"intent": "CLARIFY", "source": "XP", "clarification_fields": ["SIDE"], "rows": []},
                             "configured-routine-model", "clarify-response", 1, 1),
        ]
        self.service.process_routine_message("NTNB_RUN", "Alice", "Explique a estratégia e calcule meu retorno")
        self.service.process_routine_message("NTNB_RUN", "Alice", "XP NTN-B 2027 7,5% sem lado informado")
        ai.converse.assert_not_called()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)
        history = self.service.load_chat_history("NTNB_RUN", "Alice")
        self.assertEqual(len(history), 2)
        self.assertTrue(all(row["ASSISTANT_MESSAGE"] and row["ID_EXECUTION"] is None for row in history))
        self.assertIsNone(self.timeline_step(step)["completed_at"])

    def test_extracted_unknown_asset_or_conflicting_dates_cannot_be_relabelled_by_maturity(self):
        ai = self.seed_chat("NTNB_RUN")
        for name, maturity in (("PETR4", "2027-05"), ("2028", "2027-05")):
            with self.subTest(name=name):
                candidate = {"NM_SECURITY": name, "MATURITY_HINT": maturity, "SIDE": "Bid", "TX_QUOTE": 0.075,
                             "VL_QUOTE": None, "TS_REF": None, "SPREAD_QUOTE": None}
                ai.extract.return_value = StructuredResult(
                    {"intent": "IMPORT", "source": "XP", "clarification_fields": [], "rows": [candidate]},
                    "configured-routine-model", "conflicting-asset-response", 1, 1)
                with self.assertRaises(ValueError):
                    self.service.process_routine_message("NTNB_RUN", "Alice", "Run com ativo a esclarecer")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)

    def test_import_failure_leaves_schedule_uncompleted_and_logs_one_failed_interaction(self):
        self.seed_chat("NTNB_RUN")
        step = self.schedule("failed-step", deadline="09:00")
        self.mock_api([self.api_response("private-partial-import", status="incomplete",
            usage={"input_tokens": 9, "output_tokens": 1, "total_tokens": 10})])
        with self.assertRaises(AIError):
            self.service.process_routine_message("NTNB_RUN", "Alice", "Texto da run XP para interpretação")
        history = self.service.load_chat_history("NTNB_RUN", "Alice")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["STATUS"], "FAILED")
        self.assert_chat_has_no_usage_columns(history)
        audit = self.audit_rows()
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]["INPUT_TOKENS"], audit[0]["OUTPUT_TOKENS"], audit[0]["TOTAL_TOKENS"]), (9, 1, 10))
        self.assertEqual((audit[0]["STATUS"], audit[0]["CHAT_MESSAGE_ID"]), ("failed", history[0]["ID_MESSAGE"]))
        self.assertNotIn("private-partial-import", json.dumps(history))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS"), 0)
        self.assertIsNone(self.timeline_step(step)["completed_at"])

    def test_ntnf_pasted_spread_without_trusted_curve_does_not_complete_schedule(self):
        step = self.schedule("ntnf-slot", routine="NTNF_RUN", deadline="09:00")
        text = "NM_SECURITY\tSIDE\tSPREAD_QUOTE\nNTN-F Jan-29\tBid\t10\n"
        with self.assertRaisesRegex(ValueError, "Bloomberg|DI"):
            self.service.process_routine_message("NTNF_RUN", "Alice", text)
        rows = self.service.load_chat_history("NTNF_RUN", "Alice")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["STATUS"], "FAILED")
        timeline = next(row for row in self.service.get_routine_timeline("NTNF_RUN") if row["ID_ACTIVITY"] == step)
        self.assertIsNone(timeline["completed_at"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)

    def test_restricted_market_routine_refuses_manual_step_completion(self):
        step = self.schedule("restricted-manual", deadline="09:00")
        with self.assertRaises(ValueError):
            self.service.complete_activity("NTNB_RUN", step, "Alice")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS"), 0)

    def test_each_selected_activity_completes_only_its_own_schedule_once(self):
        first = self.schedule("slot-09", deadline="09:00")
        second = self.schedule("slot-11", deadline="11:00")
        third = self.schedule("slot-15", deadline="15:00")
        for expected_step in (third, first, second):
            with self.subTest(expected_step=expected_step):
                activity_id = self.scalar("SELECT ID_ACTIVITY FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ACTIVITY=?", (expected_step,))
                self.service.process_routine_message("NTNB_RUN", "Alice", self.canonical_run(), activity_id=activity_id)
                interaction = self.service.load_chat_history("NTNB_RUN", "Alice")[-1]
                execution = self.service.get_execution(interaction["ID_EXECUTION"], "Alice")
                self.assertEqual(execution["ID_ACTIVITY"], expected_step)
                self.assertEqual(execution["STATUS"], "PENDING_REVIEW")
                if expected_step:
                    self.assertIsNone(self.timeline_step(expected_step)["completed_at"])
                    self.assertEqual(self.timeline_step(expected_step)["progress"], "PENDING_REVIEW")
                self.service.confirm_action(execution["ID_EXECUTION"], "Alice")
                with self.assertRaisesRegex(ValueError, "já foi concluída"):
                    self.proposal(activity_id=activity_id)
        executions = self.service.get_routine_executions("NTNB_RUN")
        self.assertEqual([row["ID_ACTIVITY"] for row in executions], [third, first, second])
        self.assertTrue(all(row["STATUS"] == "SUCCESS" for row in executions))
        for step in (first, second, third):
            self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS WHERE ID_ACTIVITY=? AND STATUS='SUCCESS'", (step,)), 1)
        self.assertEqual(self.timeline_step(first)["state"], "LATE")
        self.assertEqual(self.timeline_step(second)["state"], "ON_TIME")
        self.assertEqual(self.timeline_step(third)["state"], "ON_TIME")

    def test_parallel_proposals_retain_original_slot_and_do_not_complete_future_step(self):
        morning = self.schedule("parallel-09", deadline="09:00")
        afternoon = self.schedule("parallel-15", deadline="15:00")
        alice = self.proposal()
        bob = self.service.prepare_action("NTNB_RUN", "Bob", "save_quotes", self.quotes(), activity_id="NTNB_RUN:DEFAULT")
        self.assertEqual(alice["ID_ACTIVITY"], morning)
        self.assertEqual(bob["ID_ACTIVITY"], morning)
        self.service.confirm_action(alice["ID_EXECUTION"], "Alice")
        with self.assertRaisesRegex(ValueError, "já foi concluída"):
            self.service.confirm_action(bob["ID_EXECUTION"], "Bob")
        self.assertIsNotNone(self.timeline_step(morning)["completed_at"])
        self.assertIsNone(self.timeline_step(morning)["progress"])
        self.assertIsNone(self.timeline_step(afternoon)["completed_at"])
        self.assertEqual(self.service.get_execution(bob["ID_EXECUTION"], "Bob")["ID_ACTIVITY"], morning)
        self.assertEqual(self.proposal(activity_id=afternoon)["ID_ACTIVITY"], afternoon)






    def import_debtors(self):
        args = {"rows": [
            {"ID_CLIENT": "00123", "NR_DAYS": 2, "NM_RESP_GWM": "GWM A", "DESCR": "Original case"},
            {"ID_CLIENT": "00456", "NR_DAYS": 1, "NM_RESP_GWM": "GWM B", "DESCR": "Awaiting transfer"},
        ]}
        proposal = self.service.prepare_action("DEBTORS", "Alice", "import_debtors", args)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINES_DEVEDORES"), 0)
        result = self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        self.assertEqual(result["STATUS"], "SUCCESS")
        return args

    def test_debtor_updates_are_shared_append_history_and_preserve_inverse_flag(self):
        self.import_debtors()
        self.activity("contact-client", routine="DEBTORS", tools=["update_debtor"])
        self.activity("resolve-client", routine="DEBTORS", tools=["update_debtor"])
        update = self.service.prepare_action("DEBTORS", "Bob", "update_debtor", {
            "client_id": "00123", "description": "Client contacted", "resolved": False,
        }, activity_id="contact-client")
        self.service.confirm_action(update["ID_EXECUTION"], "Bob")
        self.now += timedelta(hours=1)
        resolve = self.service.prepare_action("DEBTORS", "Alice", "update_debtor", {
            "client_id": "00123", "description": "Funds received", "resolved": True,
        }, activity_id="resolve-client")
        # Changing the target account in the review cannot redirect an update.
        with self.assertRaises(ValueError):
            self.service.confirm_action(resolve["ID_EXECUTION"], "Alice", edited_args={
                "client_id": "00456", "description": "Wrong account", "resolved": True,
            })
        self.service.confirm_action(resolve["ID_EXECUTION"], "Alice")
        row = self.execute("SELECT * FROM UGM_ROUTINES_DEVEDORES WHERE ID_CLIENT='00123'")[0]
        self.assertEqual(row["FL_RESOLVED"], 0)
        self.assertIn("Original case", row["DESCR"])
        self.assertIn("10:00:00 Bob", row["DESCR"])
        self.assertIn("Client contacted", row["DESCR"])
        self.assertIn("11:00:00 Alice", row["DESCR"])
        self.assertIn("Funds received", row["DESCR"])
        shared = self.service.get_operational_data("DEBTORS", activity_id="DEBTORS:DEFAULT")
        self.assertEqual(len(shared["rows"]), 2)
        self.assertEqual(self.scalar("SELECT FL_RESOLVED FROM UGM_ROUTINES_DEVEDORES WHERE ID_CLIENT='00456'"), 1)

    def test_debtor_closing_report_uses_shared_database_and_filters_unresolved(self):
        self.import_debtors()
        self.execute("UPDATE UGM_ROUTINES_DEVEDORES SET FL_RESOLVED=0 WHERE ID_CLIENT='00123'")
        # Bob has never conversed or imported, but sees the shared closing data.
        service = RoutineService(self.db, clock=lambda: self.now)
        report = service.prepare_action("DEBTORS", "Bob", "debtor_report", {})
        self.assertEqual(report["STATUS"], "SUCCESS")
        draft = report["RESULT_JSON"]
        self.assertEqual(draft["kind"], "draft")
        self.assertIn("00456", draft["body"])
        self.assertIn("Awaiting transfer", draft["body"])
        self.assertNotIn("00123", draft["body"])
        history = service.load_chat_history("DEBTORS", "Bob")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["ID_EXECUTION"], report["ID_EXECUTION"])
        self.assertNotIn("ROLE", history[0])
        self.now += timedelta(days=1)
        self.assertEqual(service.get_operational_data("DEBTORS")["rows"], [])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINES_DEVEDORES"), 2)

    def test_reimport_does_not_overwrite_existing_debtor_description(self):
        args = self.import_debtors()
        args["rows"][0]["DESCR"] = "Must not replace existing history"
        with self.assertRaises(ValueError):
            self.service.prepare_action("DEBTORS", "Bob", "import_debtors", args)
        self.assertEqual(self.scalar("SELECT DESCR FROM UGM_ROUTINES_DEVEDORES WHERE ID_CLIENT='00123'"), "Original case")

    def test_credit_sources_share_quote_executor_without_accepting_sovereigns(self):
        args = {"source": "xBridge", "rows": [{"NM_SECURITY": "TEST11", "SIDE": "Ask", "TX_QUOTE": 0.12}]}
        proposal = self.service.prepare_action("CREDIT_RUN", "Bob", "save_quotes", args)
        self.service.confirm_action(proposal["ID_EXECUTION"], "Bob")
        self.assertEqual(self.scalar("SELECT NM_BROKER FROM FIXED_INCOME_BROKER_DATA WHERE USR_INPUT='Bob'"), "xBridge")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS WHERE STATUS='SUCCESS'"), 1)
        with self.assertRaises(ValueError):
            self.service.prepare_action("CREDIT_RUN", "Bob", "save_quotes", self.quotes())

    def test_ntnf_missing_curve_never_invents_or_writes_a_rate(self):
        with self.assertRaisesRegex(ValueError, "Bloomberg|DI"):
            self.service.prepare_action("NTNF_RUN", "Alice", "save_quotes", {
                "source": "XP", "rows": [{"NM_SECURITY": "NTN-F Jan-29", "SIDE": "Bid", "SPREAD_QUOTE": 10}],
            })
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)

    def test_changed_ntnf_derived_rates_require_review_of_updated_snapshot(self):
        calculated = {"reference_rate": 0.13, "rate": 0.131}
        with patch("services.routine_tools._DI_CURVE_PROVIDER", return_value={"test_curve": True}), \
                patch("services.routine_tools._NTNF_RATE_CALCULATOR", side_effect=lambda **_: dict(calculated)):
            proposal = self.service.prepare_action("NTNF_RUN", "Alice", "save_quotes", {
                "source": "XP", "rows": [{"NM_SECURITY": "NTN-F Jan-29", "SIDE": "Bid", "SPREAD_QUOTE": 10}],
            })
            displayed = proposal["RESULT_JSON"]["rows"][0]
            self.assertEqual(displayed["TX_DI_REF"], 0.13)
            self.assertEqual(displayed["TX_SPREAD"], 0.131)
            calculated.update(reference_rate=0.14, rate=0.141)
            changed = self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
            self.assertEqual(changed["ID_EXECUTION"], proposal["ID_EXECUTION"])
            self.assertEqual(changed["STATUS"], "PENDING_REVIEW")
            self.assertIsNone(changed["TS_EXECUTION"])
            self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)
            self.assertTrue(any("changed" in warning.lower() for warning in changed["RESULT_JSON"]["warnings"]))
            updated_display = changed["RESULT_JSON"]["rows"][0]
            self.assertEqual(updated_display["TX_DI_REF"], 0.14)
            self.assertEqual(updated_display["TX_SPREAD"], 0.141)
            self.assertEqual(changed["INPUT_JSON"]["rows"][0]["TX_SPREAD"], updated_display["TX_SPREAD"])
            # A stable second click saves the snapshot the user has now seen.
            confirmed = self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
            self.assertEqual(confirmed["STATUS"], "SUCCESS")
        saved = self.execute("SELECT TX_QUOTE,TX_DI_REF,TX_SPREAD,SPREAD_QUOTE FROM FIXED_INCOME_BROKER_DATA WHERE USR_INPUT='Alice'")
        self.assertEqual(saved, [{"TX_QUOTE": 0.141, "TX_DI_REF": 0.14, "TX_SPREAD": 0.141, "SPREAD_QUOTE": 10.0}])

    def test_security_lookup_is_read_only_and_scoped_to_routine(self):
        execution = self.service.prepare_action("CREDIT_RUN", "Alice", "lookup_securities", {})
        self.assertEqual(execution["STATUS"], "SUCCESS")
        self.assertEqual(execution["ID_ACTIVITY"], "CREDIT_RUN:DEFAULT")
        self.assertEqual([row["NM_SECURITY"] for row in execution["RESULT_JSON"]["rows"]], ["TEST11"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)
        for text in ("%", "' OR 1=1 --"):
            with self.subTest(search=text):
                execution = self.service.prepare_action("CREDIT_RUN", "Alice", "lookup_securities", {"search": text})
                self.assertEqual(execution["RESULT_JSON"]["rows"], [])
        execution = self.service.prepare_action("CREDIT_RUN", "Alice", "lookup_securities", {"search": "2030-01"})
        self.assertEqual(execution["RESULT_JSON"]["rows"][0]["NM_SECURITY"], "TEST11")
        with self.assertRaises(ValueError):
            self.service.prepare_action("DEBTORS", "Alice", "lookup_securities", {})

    def test_restricted_market_routines_reject_standalone_read_tools_and_shared_queries(self):
        for routine in ("NTNB_RUN", "NTNF_RUN"):
            self.assertEqual([row["NM_TOOL"] for row in self.service.load_routine_tools(routine)], ["save_quotes"])
            for tool in ("lookup_securities", "query_quotes"):
                with self.subTest(routine=routine, tool=tool):
                    with self.assertRaises(ValueError):
                        self.service.prepare_action(routine, "Alice", tool, {})
            with self.subTest(routine=routine, action="shared query"):
                with self.assertRaises(ValueError):
                    self.service.get_operational_data(routine)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS"), 0)

    def test_similar_market_routine_reuses_handlers_with_sqlite_configuration(self):
        routine='CUSTOM_BOND_RUN'
        self.execute("INSERT INTO UGM_ROUTINES(ID_ROUTINE,NM_ROUTINE,LAYOUT_KEY) VALUES(?,'Custom bond run','market_run')",(routine,))
        self.service.configure_activity(routine,'CUSTOM_IMPORT','Import',prompt_key='CUSTOM',tools=['lookup_securities','save_quotes','query_quotes'],
            config=dict(sources=['XP'],instrument_types=['NTN-B'],quote_mode='RATE'))
        lookup=self.service.prepare_action(routine,'Alice','lookup_securities',{})
        self.assertEqual([row['NM_SECURITY'] for row in lookup['RESULT_JSON']['rows']],['NTN-B May-27'])
        proposal=self.service.prepare_action(routine,'Alice','save_quotes',self.quotes())
        result=self.service.confirm_action(proposal['ID_EXECUTION'],'Alice',routine_id=routine)
        self.assertEqual(result['STATUS'],'SUCCESS')
        self.assertEqual(self.service.get_operational_data(routine)['rows'][0]['NM_SECURITY'],'NTN-B May-27')

    def test_unknown_parameters_are_rejected_before_normalization_can_hide_them(self):
        for args in (
            {**self.quotes(), "query": "DELETE FROM FIXED_INCOME_BROKER_DATA"},
            {"source": "XP", "rows": [{**self.quotes()["rows"][0], "USR_INPUT": "Forged user"}]},
            {"source": "XP", "rows": [{**self.quotes()["rows"][0], "DT_REF": "2020-01-01"}]},
        ):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.service.prepare_action("NTNB_RUN", "Alice", "save_quotes", args)
        with self.assertRaises(ValueError):
            self.service.prepare_action("NTNB_RUN", "Alice", "lookup_securities", {"search": "NTN", "table": "USER_KEYS"})
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)

    def test_csv_numbers_normalize_and_confirm_without_changing_decimal_units(self):
        parsed = parse_import("NM_SECURITY,SIDE,TX_QUOTE,VL_QUOTE\nNTN-B May-27,Bid,0.075,1000\n", "xp.csv")
        proposal = self.service.prepare_action("NTNB_RUN", "Alice", "save_quotes", {"source": "XP", "rows": parsed["rows"]})
        normalized = proposal["INPUT_JSON"]["rows"][0]
        self.assertIsInstance(normalized["TX_QUOTE"], float)
        self.assertEqual(normalized["TX_QUOTE"], 0.075)
        self.assertEqual(normalized["VL_QUOTE"], 1000)
        self.assertEqual(proposal["STATUS"], "PENDING_REVIEW")
        confirmed = self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        self.assertEqual(confirmed["STATUS"], "SUCCESS")
        row = self.execute("SELECT TX_QUOTE,VL_QUOTE FROM FIXED_INCOME_BROKER_DATA WHERE USR_INPUT='Alice'")[0]
        self.assertEqual(row, {"TX_QUOTE": 0.075, "VL_QUOTE": 1000.0})

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "Install requirements-ai.txt for XLSX imports.")
    def test_xlsx_numbers_and_text_accounts_roundtrip_through_confirmed_import(self):
        from openpyxl import Workbook
        for routine, tool, headers, values in [
            ("CREDIT_RUN", "save_quotes", ["NM_SECURITY", "SIDE", "TX_QUOTE"], ["TEST11", "Bid", 0.075]),
            ("DEBTORS", "import_debtors", ["ID_CLIENT", "NR_DAYS", "NM_RESP_GWM", "DESCR"], ["00123", 2, "GWM", "Waiting"]),
        ]:
            with self.subTest(routine=routine):
                workbook = Workbook()
                workbook.active.append(headers)
                workbook.active.append(values)
                buffer = io.BytesIO()
                workbook.save(buffer)
                workbook.close()
                args = {"rows": parse_import(buffer.getvalue(), "manual.xlsx")["rows"]}
                if tool == "save_quotes":
                    args["source"] = "xBridge"
                proposal = self.service.prepare_action(routine, "Alice", tool, args)
                self.assertEqual(proposal["STATUS"], "PENDING_REVIEW")
                result = self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
                self.assertEqual(result["STATUS"], "SUCCESS")
        self.assertEqual(self.scalar("SELECT TX_QUOTE FROM FIXED_INCOME_BROKER_DATA WHERE NM_BROKER='xBridge'"), 0.075)
        self.assertEqual(self.scalar("SELECT ID_CLIENT FROM UGM_ROUTINES_DEVEDORES"), "00123")

    def test_editing_quote_security_rederives_its_hidden_classification_id(self):
        self.execute("""INSERT INTO FIXED_INCOME_SECURITY_CLASSIFICATION
            (ID_SECURITY,NM_SECURITY,NM_INSTRUMENT_TYPE,DT_MATURITY,NM_SECTOR)
            VALUES(104,'NTN-B Aug-28','NTN-B','2028-08-15','Sovereign')""")
        proposal = self.proposal()
        edited = proposal["INPUT_JSON"]
        self.assertEqual(edited["rows"][0]["ID_SECURITY"], 101)
        # AG Grid edits the visible name while the backend ID remains hidden.
        edited["rows"][0]["NM_SECURITY"] = "NTN-B Aug-28"
        result = self.service.confirm_action(proposal["ID_EXECUTION"], "Alice", edited_args=edited)
        self.assertEqual(result["STATUS"], "SUCCESS")
        self.assertEqual(self.execute("SELECT ID_SECURITY,NM_SECURITY FROM FIXED_INCOME_BROKER_DATA WHERE USR_INPUT='Alice'"), [
            {"ID_SECURITY": 104, "NM_SECURITY": "NTN-B Aug-28"},
        ])

    def test_daily_quote_runs_keep_separate_execution_ids_at_same_time_and_exclude_uncommitted_data(self):
        first_args = self.quotes()
        first_args["rows"].append({"NM_SECURITY": "NTN-B May-27", "SIDE": "Ask", "TX_QUOTE": 0.077})
        first = self.service.prepare_action("NTNB_RUN", "Alice", "save_quotes", first_args)
        first = self.service.confirm_action(first["ID_EXECUTION"], "Alice")
        for activity_id in ("second-run", "pending-run", "failed-run"):
            self.activity(activity_id)
        second = self.service.prepare_action("NTNB_RUN", "Bob", "save_quotes", self.quotes(TX_QUOTE=0.08), activity_id="second-run")
        second = self.service.confirm_action(second["ID_EXECUTION"], "Bob")
        self.proposal(activity_id="pending-run")  # Pending activities are not market data.
        failed = self.service.prepare_action("NTNB_RUN", "Carol", "save_quotes", self.quotes(TX_QUOTE=0.09), activity_id="failed-run")
        self.execute("""CREATE TRIGGER reject_curve_fixture BEFORE INSERT ON FIXED_INCOME_BROKER_DATA
            WHEN NEW.USR_INPUT='Carol' BEGIN SELECT RAISE(ABORT,'fixture failure'); END""")
        self.assertEqual(self.service.confirm_action(failed["ID_EXECUTION"], "Carol")["STATUS"], "FAILED")
        self.assertEqual(first["TS_EXECUTION"], second["TS_EXECUTION"])
        data = self.service.get_daily_quote_runs("NTNB_RUN")
        self.assertEqual(data["summary"], {"run_count": 2, "quote_count": 3, "security_count": 1})
        self.assertEqual([run["id_execution"] for run in data["runs"]], [first["ID_EXECUTION"], second["ID_EXECUTION"]])
        self.assertEqual({run["username"] for run in data["runs"]}, {"Alice", "Bob"})
        self.assertNotEqual(data["runs"][0]["label"], data["runs"][1]["label"])
        self.assertEqual([len(run["rows"]) for run in data["runs"]], [2, 1])
        self.assertEqual({row["ID_EXECUTION"] for row in data["rows"]}, {first["ID_EXECUTION"], second["ID_EXECUTION"]})
        self.assertEqual(sorted(row["TX_QUOTE"] for row in data["rows"]), [0.075, 0.077, 0.08])
        self.service.confirm_action(first["ID_EXECUTION"], "Alice")
        self.assertEqual(self.service.get_daily_quote_runs("NTNB_RUN")["summary"], data["summary"])

    def test_daily_quote_runs_are_shared_but_scoped_by_routine_and_operational_day(self):
        first = self.proposal()
        self.service.confirm_action(first["ID_EXECUTION"], "Alice")
        credit = self.service.prepare_action("CREDIT_RUN", "Bob", "save_quotes", {
            "source": "xBridge", "rows": [{"NM_SECURITY": "TEST11", "SIDE": "Ask", "TX_QUOTE": 0.12}]})
        self.service.confirm_action(credit["ID_EXECUTION"], "Bob")
        self.now += timedelta(days=1)
        today = self.service.prepare_action("NTNB_RUN", "Bob", "save_quotes", self.quotes(TX_QUOTE=0.081))
        self.service.confirm_action(today["ID_EXECUTION"], "Bob")
        shared = RoutineService(self.db, clock=lambda: self.now)
        data = shared.get_daily_quote_runs("NTNB_RUN")
        self.assertEqual([run["id_execution"] for run in data["runs"]], [today["ID_EXECUTION"]])
        self.assertEqual(data["rows"][0]["USERNAME"], "Bob")
        yesterday = shared.get_daily_quote_runs("NTNB_RUN", dt_ref="2026-11-03")
        self.assertEqual([run["id_execution"] for run in yesterday["runs"]], [first["ID_EXECUTION"]])
        self.assertEqual(yesterday["rows"][0]["USERNAME"], "Alice")
        credit_data = shared.get_daily_quote_runs("CREDIT_RUN", dt_ref="2026-11-03")
        self.assertEqual([row["NM_SECURITY"] for row in credit_data["rows"]], ["TEST11"])

    def test_curve_vertices_follow_classification_maturities_instead_of_security_labels(self):
        self.execute("""INSERT INTO FIXED_INCOME_SECURITY_CLASSIFICATION
            (ID_SECURITY,NM_SECURITY,NM_INSTRUMENT_TYPE,DT_MATURITY,NM_SECTOR)
            VALUES(104,'NTN-B Zebra','NTN-B','2027-03-15','Sovereign'),
                  (105,'NTN-B Alpha','NTN-B','2030-08-15','Sovereign')""")
        rows = [{"NM_SECURITY": name, "SIDE": "Bid", "TX_QUOTE": 0.075}
                for name in ("NTN-B Alpha", "NTN-B May-27", "NTN-B Zebra")]
        proposal = self.service.prepare_action("NTNB_RUN", "Alice", "save_quotes", {"source": "XP", "rows": rows})
        self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        data = self.service.get_daily_quote_runs("NTNB_RUN")
        self.assertEqual(data["vertices"], [
            {"NM_SECURITY": "NTN-B Zebra", "DT_MATURITY": "2027-03-15"},
            {"NM_SECURITY": "NTN-B May-27", "DT_MATURITY": "2027-05-15"},
            {"NM_SECURITY": "NTN-B Alpha", "DT_MATURITY": "2030-08-15"},
        ])
        self.assertEqual([row["NM_SECURITY"] for row in data["runs"][0]["rows"]],
                         ["NTN-B Zebra", "NTN-B May-27", "NTN-B Alpha"])
        self.assertTrue(all(row["SIDE"] == "Bid" and row["TX_QUOTE"] == 0.075 for row in data["rows"]))
        self.assertEqual(len(data["rows"]), 3)  # No synthetic Ask quotes.

    def test_daily_quote_runs_recover_legacy_committed_input_when_result_has_no_rows(self):
        proposal = self.proposal()
        self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        self.execute("UPDATE UGM_ROUTINE_EXECUTIONS SET RESULT_JSON=? WHERE ID_EXECUTION=?",
                     (json.dumps({"message": "Legacy import committed"}), proposal["ID_EXECUTION"]))
        data = self.service.get_daily_quote_runs("NTNB_RUN")
        self.assertEqual(data["summary"], {"run_count": 1, "quote_count": 1, "security_count": 1})
        self.assertEqual(data["rows"][0]["NM_SECURITY"], "NTN-B May-27")
        self.assertEqual(data["rows"][0]["TX_QUOTE"], 0.075)
        self.assertEqual(data["rows"][0]["DT_MATURITY"], "2027-05-15")

    def test_incomplete_historical_curve_rows_do_not_invent_side_rate_or_maturity(self):
        proposal = self.proposal()
        self.service.confirm_action(proposal["ID_EXECUTION"], "Alice")
        missing = {"rows": [{"NM_SECURITY": "NTN-B missing classification", "SIDE": None, "TX_QUOTE": None}]}
        self.execute("UPDATE UGM_ROUTINE_EXECUTIONS SET RESULT_JSON=? WHERE ID_EXECUTION=?",
                     (json.dumps(missing), proposal["ID_EXECUTION"]))
        data = self.service.get_daily_quote_runs("NTNB_RUN")
        self.assertEqual(len(data["rows"]), 1)
        self.assertIsNone(data["rows"][0]["SIDE"])
        self.assertIsNone(data["rows"][0]["TX_QUOTE"])
        self.assertIsNone(data["rows"][0]["DT_MATURITY"])
        self.assertEqual(data["vertices"], [])
        self.assertTrue(data["warnings"])


    def activity(self, activity_id="ntnb-second", *, routine="NTNB_RUN", tools=None, config=None):
        base=self.service.load_activity(routine,f"{routine}:DEFAULT")
        settings={**base['CONFIG_JSON'],**(config or {})}
        return self.service.configure_activity(routine,activity_id,'Second activity',prompt_key=activity_id,
            tools=['save_quotes'] if tools is None else tools,config=settings)


    def test_activity_selection_is_required_when_a_routine_has_multiple_activities(self):
        self.activity()
        for operation in (
            lambda: self.service.process_routine_message("NTNB_RUN", "Alice", self.canonical_run()),
            lambda: self.service.prepare_action("NTNB_RUN", "Alice", "save_quotes", self.quotes()),
            lambda: self.service.load_prompt("NTNB_RUN"),
        ):
            with self.assertRaisesRegex(ValueError, "Selecione uma atividade"):
                operation()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_CHAT"), 0)

    def test_activity_tools_are_explicit_and_revocation_blocks_prepared_writes(self):
        self.activity("readonly", tools=[], config={"workflow": "assistant"})
        self.assertEqual(self.service.load_routine_tools("NTNB_RUN", "readonly"), [])
        with self.assertRaisesRegex(ValueError, "not enabled for this activity"):
            self.proposal(activity_id="readonly")
        proposal = self.proposal(activity_id="NTNB_RUN:DEFAULT")
        self.service.configure_activity("NTNB_RUN", "NTNB_RUN:DEFAULT", "Import", prompt_key="NTNB_RUN", tools=[])
        with self.assertRaisesRegex(ValueError, "not enabled for this activity"):
            self.service.confirm_action(proposal["ID_EXECUTION"], "Alice", activity_id="NTNB_RUN:DEFAULT")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA"), 1)

    def test_activity_prompt_history_and_central_audit_are_isolated(self):
        self.activity("closing", routine="DEBTORS", tools=["debtor_report"])
        PromptStore(self.db).seed("routines", "closing", "Only this closing activity", "closing-model")
        self.service.append_chat_message("DEBTORS", "Alice", "user", "Sibling secret", activity_id="DEBTORS:DEFAULT")
        client = self.mock_api([self.api_response("Closing response", items=self.assistant_response("Closing response").items,
            usage={"input_tokens": 8, "output_tokens": 2, "total_tokens": 10})])
        self.service.process_routine_message("DEBTORS", "Alice", "What can you do?", activity_id="closing")
        request = client.responses.create.call_args.kwargs
        self.assertIn("Only this closing activity", request["instructions"])
        self.assertEqual(request["model"], "closing-model")
        self.assertEqual([tool["name"] for tool in request["tools"]], ["debtor_report"])
        self.assertNotIn("Sibling secret", json.dumps(request["input"]))
        chat = self.service.load_chat_history("DEBTORS", "Alice", activity_id="closing")
        self.assertEqual(len(chat), 1)
        self.assertEqual(chat[0]["ID_ACTIVITY"], "closing")
        self.assertEqual(self.audit_rows()[0]["CHAT_MESSAGE_ID"], chat[0]["ID_MESSAGE"])
        self.assertEqual(self.audit_rows()[0]["TOTAL_TOKENS"], 10)

    def test_model_cannot_call_a_sibling_activity_tool(self):
        self.activity("closing", routine="DEBTORS", tools=["debtor_report"])
        ai = self.seed_chat("closing")
        ai.converse.side_effect = [ConversationResult([
            {"type": "function_call", "call_id": "forbidden", "name": "query_debtors", "arguments": "{}"}
        ], "", "response", "test"), self.assistant_response("Unavailable")]
        self.service.process_routine_message("DEBTORS", "Alice", "Query", activity_id="closing")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS"), 0)
        output = ai.converse.call_args.kwargs["items"][-2]
        self.assertIn("REJECTED", output["output"])

    def test_activity_schedule_is_edited_on_the_activity_itself(self):
        self.activity()
        self.service.configure_activity_schedule('NTNB_RUN','ntnb-second',deadline='10:30',recurrence='DAILY',completion_action='save_quotes')
        activity=self.service.load_activity('NTNB_RUN','ntnb-second')
        self.assertEqual(activity['DEADLINE_TIME'],'10:30')
        self.service.configure_activity_schedule('NTNB_RUN','ntnb-second',deadline='12:30',recurrence='DAILY',completion_action='save_quotes')
        self.assertEqual(self.service.load_activity('NTNB_RUN','ntnb-second')['DEADLINE_TIME'],'12:30')
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_ACTIVITIES WHERE ID_ACTIVITY='ntnb-second'"),1)
        proposal=self.proposal(activity_id='ntnb-second')
        self.service.confirm_action(proposal['ID_EXECUTION'],'Alice')
        with self.assertRaisesRegex(ValueError,'já foi concluída'):
            self.proposal(activity_id='ntnb-second')

    def test_activity_cannot_use_a_sibling_schedule_chat_or_review_card(self):
        step = self.schedule()
        self.activity()
        original = self.proposal(activity_id="NTNB_RUN:DEFAULT")
        interaction_id = self.service.load_chat_history("NTNB_RUN", "Alice", activity_id="NTNB_RUN:DEFAULT")[0]["ID_MESSAGE"]
        for operation in (
            lambda: self.proposal(activity_id=step + "-missing"),
            lambda: self.proposal(activity_id="ntnb-second", interaction_id=interaction_id),
            lambda: self.service.confirm_action(original["ID_EXECUTION"], "Alice", activity_id="ntnb-second"),
            lambda: self.service.cancel_action(original["ID_EXECUTION"], "Alice", activity_id="ntnb-second"),
        ):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(self.service.get_routine_executions("NTNB_RUN", activity_id="ntnb-second"), [])
        self.assertEqual(self.service.get_execution(original["ID_EXECUTION"], "Alice")["STATUS"], "PENDING_REVIEW")

    def test_activity_configuration_cannot_cross_routines_or_grant_unknown_tools(self):
        self.activity()
        with self.assertRaises(ValueError):
            self.activity("ntnb-second", routine="DEBTORS", tools=[])
        with self.assertRaises(ValueError):
            self.activity("arbitrary", tools=["execute_sql"])
        with self.assertRaises(ValueError):
            self.service.load_activity("DEBTORS", "ntnb-second")
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute("INSERT INTO UGM_ROUTINE_EXECUTIONS(ID_ROUTINE,ID_ACTIVITY,DT_REF,USERNAME,NM_ACTION,TS_REQUEST,STATUS,INPUT_JSON) VALUES('DEBTORS','ntnb-second','2026-11-03','Alice','query_debtors','now','PENDING_REVIEW','{}')")
        self.execute("UPDATE UGM_ROUTINE_ACTIVITIES SET FL_ACTIVE=0 WHERE ID_ACTIVITY='ntnb-second'")
        with self.assertRaises(ValueError):
            self.proposal(activity_id="ntnb-second")
        self.assertEqual([a["ID_ACTIVITY"] for a in self.service.list_activities("NTNB_RUN")], ["NTNB_RUN:DEFAULT"])

    def test_overview_exposes_scheduled_and_unscheduled_sibling_activities(self):
        self.schedule("original")
        self.activity("unscheduled")
        self.activity("scheduled")
        self.service.configure_activity_schedule("NTNB_RUN", "scheduled", deadline="11:00", recurrence="DAILY", completion_action="save_quotes")
        self.proposal(activity_id="unscheduled")
        overview = self.service.get_daily_overview()
        rows = [a for a in overview["activities"] if a["ID_ROUTINE"] == "NTNB_RUN"]
        self.assertEqual({a["ID_ACTIVITY"] for a in rows}, {"NTNB_RUN:DEFAULT", "scheduled"})
        other = next(a for a in overview["unscheduled_routines"] if a["ID_ACTIVITY"] == "unscheduled")
        self.assertEqual(other["attention"], "PENDING_REVIEW")
        self.assertIsNone(next(a for a in rows if a["ID_ACTIVITY"] == "scheduled")["progress"])


    def test_competing_proposals_can_complete_an_activity_only_once(self):
        first = self.proposal()
        second = self.service.prepare_action("NTNB_RUN", "Bob", "save_quotes", self.quotes())

        def confirm(item):
            try:
                return RoutineService(self.db, clock=lambda: self.now).confirm_action(item["ID_EXECUTION"], item["USERNAME"])["STATUS"]
            except ValueError:
                return "ALREADY_COMPLETED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(confirm, [first, second]))
        self.assertCountEqual(outcomes, ["SUCCESS", "ALREADY_COMPLETED"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS WHERE STATUS='SUCCESS' AND FL_COMPLETES_ACTIVITY=1"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM FIXED_INCOME_BROKER_DATA WHERE DT_REF='2026-11-03'"), 1)

    def test_failed_and_cancelled_actions_allow_retry_and_new_day_has_own_completion(self):
        cancelled = self.proposal()
        self.service.cancel_action(cancelled["ID_EXECUTION"], "Alice")
        self.assertIsNone(self.service.get_activity_completion("NTNB_RUN", "NTNB_RUN:DEFAULT"))
        failed = self.proposal()
        self.execute("CREATE TRIGGER fail_single_action BEFORE INSERT ON FIXED_INCOME_BROKER_DATA BEGIN SELECT RAISE(ABORT,'failure'); END")
        self.assertEqual(self.service.confirm_action(failed["ID_EXECUTION"], "Alice")["STATUS"], "FAILED")
        self.assertIsNone(self.service.get_activity_completion("NTNB_RUN", "NTNB_RUN:DEFAULT"))
        self.execute("DROP TRIGGER fail_single_action")
        success = self.proposal()
        self.service.confirm_action(success["ID_EXECUTION"], "Alice")
        with self.assertRaisesRegex(ValueError, "já foi concluída"):
            self.proposal()
        self.now += timedelta(days=1)
        next_day = self.proposal()
        self.service.confirm_action(next_day["ID_EXECUTION"], "Alice")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM UGM_ROUTINE_EXECUTIONS WHERE STATUS='SUCCESS' AND FL_COMPLETES_ACTIVITY=1"), 2)




class ImportContractTests(unittest.TestCase):
    def test_canonical_csv_and_tsv_use_same_parser_and_preserve_account_zeroes(self):
        result = parse_import("ID_CLIENT\tNR_DAYS\tNM_RESP_GWM\tDESCR\n00123\t2\tGWM\tWaiting\n", "clients.tsv")
        self.assertEqual(result["rows"][0]["ID_CLIENT"], "00123")
        self.assertEqual(result["text"], "")
        result = parse_import("NM_SECURITY,SIDE,TX_QUOTE\nNTN-B May-27,Bid,0.075\n", "quotes.csv")
        self.assertEqual(result["rows"][0]["TX_QUOTE"], "0.075")

    def test_unknown_vendor_text_is_preserved_for_interpretation(self):
        text = "XP market run\nOriginal vendor text that has no documented schema"
        result = parse_import(text)
        self.assertIsNone(result["rows"])
        self.assertEqual(result["text"], text)
        with self.assertRaises(ValueError):
            parse_import("Bond,Yield\nNTNB,7.5\n", "unknown.csv")

    def test_email_plain_text_can_be_reviewed_without_mailbox_access(self):
        result = parse_import(b"From: broker@example.com\r\nTo: user@example.com\r\nSubject: Run\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nVendor run for review", "run.eml")
        self.assertIsNone(result["rows"])
        self.assertIn("Vendor run for review", result["text"])


class ConversationTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "prompts.db"
        self.store = PromptStore(self.db)
        self.store.seed("routines", "test-chat", "Prompt from the existing store", "model-from-sqlite")
        self.prompt = self.store.get_active("routines", "test-chat")

    def test_existing_transport_replays_complete_items_with_call_ids(self):
        output = [
            {"type": "reasoning", "id": "reasoning-1", "summary": [], "encrypted_content": "opaque"},
            {"type": "function_call", "call_id": "call-1", "name": "save_quotes", "arguments": "{}"},
        ]
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            status="completed", output=output, output_text="", id="response-1", model="model-from-sqlite")
        history = [{"role": "user", "content": "Import this run"}]
        tools = [{"type": "function", "name": "save_quotes", "parameters": {"type": "object", "properties": {}}}]
        result = OpenAIService(client=client, database_path=self.db).converse(prompt=self.prompt, items=history, tools=tools)
        request = client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "model-from-sqlite")
        self.assertEqual(request["instructions"], "Prompt from the existing store")
        self.assertEqual(request["input"], history)
        self.assertEqual(request["tools"], tools)
        self.assertFalse(request["store"])
        self.assertIn("reasoning.encrypted_content", request["include"])
        self.assertEqual(result.items, output)

    def test_provider_details_are_not_exposed_to_chat_user(self):
        client = Mock()
        client.responses.create.side_effect = RuntimeError("sensitive credential or provider detail")
        with self.assertRaises(AIError) as error:
            OpenAIService(client=client, database_path=self.db).converse(prompt=self.prompt, items=[], tools=[])
        self.assertNotIn("sensitive", str(error.exception))

    def test_conversation_transport_uses_api_counts_from_object_or_dict_usage(self):
        cases = [
            (SimpleNamespace(input_tokens=53, output_tokens=7, total_tokens=60), (53, 7, 60)),
            ({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, (0, 0, 0)),
            ({"input_tokens": 5, "output_tokens": 2}, (5, 2, None)),
            (None, (None, None, None)),
            ({"input_tokens": True, "output_tokens": -3, "total_tokens": 1.5}, (None, None, None)),
        ]
        for usage, expected in cases:
            with self.subTest(usage=usage):
                client = Mock()
                response = SimpleNamespace(status="completed", output=[], output_text="", id="usage-response", model="model-from-sqlite")
                if usage is not None:
                    response.usage = usage
                client.responses.create.return_value = response
                result = OpenAIService(client=client, database_path=self.db).converse(prompt=self.prompt, items=[], tools=[])
                self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), expected)

    def test_incomplete_conversation_keeps_api_reported_counts_and_response_identity(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            status="incomplete", output=[], output_text="private-partial-output", id="incomplete-provider-id",
            model="actual-provider-model", usage={"input_tokens": 80, "output_tokens": 12, "total_tokens": 92})
        with self.assertRaises(AIError) as error:
            OpenAIService(client=client, database_path=self.db).converse(prompt=self.prompt, items=[], tools=[])
        self.assertEqual((error.exception.input_tokens, error.exception.output_tokens, error.exception.total_tokens), (80, 12, 92))
        self.assertEqual(error.exception.response_id, "incomplete-provider-id")
        self.assertEqual(error.exception.model, "actual-provider-model")
        self.assertNotIn("private-partial-output", str(error.exception))


if __name__ == "__main__":
    unittest.main()
