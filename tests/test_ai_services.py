from contextlib import closing
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services.openai_client import AIConfigurationError, AIError, OpenAIService
from services.prompts import PromptStore
from services.ai_audit import AIRequestLog


class AIServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "test.db"
        self.store = PromptStore(self.db)
        self.store.seed("screening", "extract", "Extract test fields", "model-from-sqlite")

    def test_scoped_prompts_are_seeded_once_and_versions_preserved(self):
        original = self.store.get_active("screening", "extract")
        new = self.store.save_version("screening", "extract", "Updated extraction", "new-model")
        self.store.seed("screening", "extract", "Never overwrite", "ignored-model")
        self.store.seed("portfolio", "extract", "Portfolio instructions", "portfolio-model")
        self.assertEqual(new.version, 2)
        self.assertEqual(new.model, "new-model")
        self.assertEqual(self.store.get_active("screening", "extract"), new)
        self.assertNotEqual(self.store.get_active("portfolio", "extract").instructions, new.instructions)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT FL_ACTIVE FROM AI_PROMPTS WHERE PROMPT_ID = ?", (original.id,)).fetchone()[0], 0)

    def test_strict_stateless_sdk_request_and_prompt_metadata(self):
        self.store.save_version("screening", "extract", "Extract test fields", "model-from-sqlite",
                                comment="DEVELOPMENT_NOTE_ONLY: used by the screening callback")
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            status="completed", output=[SimpleNamespace(content=None)], output_text=json.dumps({"value": 123}),
            model="test-model", id="response-123",
            usage=SimpleNamespace(input_tokens=1200, output_tokens=75, total_tokens=1275),
        )
        service = OpenAIService(client=client, database_path=self.db)
        result = service.extract(
            prompt=self.store.get_active("screening", "extract"),
            payload={"text": "RFQ"}, schema={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}, schema_name="rfq_test",
        )
        args = client.responses.create.call_args.kwargs
        self.assertFalse(args["store"])
        self.assertTrue(args["text"]["format"]["strict"])
        self.assertNotIn("tools", args)
        self.assertNotIn("previous_response_id", args)
        self.assertEqual(args["metadata"]["feature"], "screening")
        self.assertEqual(args["model"], "model-from-sqlite")
        self.assertEqual(result.data, {"value": 123})
        self.assertNotIn("DEVELOPMENT_NOTE_ONLY", json.dumps(args))
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (1200, 75, 1275))

    def test_provider_failures_are_sanitized_and_refusals_are_handled(self):
        client = Mock()
        service = OpenAIService(client=client, database_path=self.db)
        kwargs = dict(prompt=self.store.get_active("screening", "extract"), payload={}, schema={}, schema_name="test")
        client.responses.create.side_effect = RuntimeError("sensitive-provider-detail")
        with self.assertRaises(AIError) as error:
            service.extract(**kwargs)
        self.assertNotIn("sensitive-provider-detail", str(error.exception))
        client.responses.create.side_effect = None
        for status, output, text in [
            ("incomplete", [], "{}"),
            ("completed", [SimpleNamespace(content=[SimpleNamespace(type="refusal")])], ""),
            ("completed", [], "not JSON"),
            ("completed", [], "[]"),
        ]:
            client.responses.create.return_value = SimpleNamespace(
                status=status, output=output, output_text=text,
                usage=SimpleNamespace(input_tokens=900, output_tokens=80, total_tokens=980),
            )
            with self.assertRaises(AIError) as error:
                service.extract(**kwargs)
            self.assertEqual((error.exception.input_tokens, error.exception.output_tokens, error.exception.total_tokens), (900, 80, 980))

    def test_missing_token_usage_is_unknown_and_reported_zero_is_preserved(self):
        client = Mock()
        response = SimpleNamespace(status="completed", output=[], output_text='{}', model="model", id="id")
        client.responses.create.return_value = response
        service = OpenAIService(client=client, database_path=self.db)
        kwargs = dict(prompt=self.store.get_active("screening", "extract"), payload={}, schema={}, schema_name="test")
        result = service.extract(**kwargs)
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (None, None, None))
        response.usage = SimpleNamespace(input_tokens=0, output_tokens=0, total_tokens=0)
        result = service.extract(**kwargs)
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (0, 0, 0))

    def test_missing_model_and_instructions_fail_without_calling_sdk(self):
        from dataclasses import replace
        prompt = self.store.get_active("screening", "extract")
        client = Mock()
        for invalid in [replace(prompt, model=""), replace(prompt, instructions="")]:
            with self.assertRaises(AIConfigurationError):
                OpenAIService(client=client, database_path=self.db).extract(prompt=invalid, payload={}, schema={}, schema_name="test")
        client.responses.create.assert_not_called()

    def test_legacy_schema_adds_model_without_overwriting_prompts(self):
        path = Path(self.temp.name) / "legacy.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE AI_PROMPTS (PROMPT_ID INTEGER PRIMARY KEY, FEATURE TEXT, PROMPT_KEY TEXT, VERSION INTEGER, INSTRUCTIONS TEXT, FL_ACTIVE INTEGER, DT_CREATED TEXT)")
            conn.execute("INSERT INTO AI_PROMPTS VALUES (1, 'test', 'extract', 1, 'Existing instructions', 1, '2026-01-01')")
        legacy = PromptStore(path)
        legacy.ensure_schema()
        prompt = legacy.get_active("test", "extract")
        self.assertIsNone(prompt.model)
        self.assertEqual(prompt.instructions, "Existing instructions")
        self.assertIsNone(prompt.comment)

    def test_prompt_comments_are_editable_and_carried_into_new_versions(self):
        note = 'Where: pages/example.py\nWhen: user clicks "Apply"; owner\'s notes — João.'
        first = self.store.save_version("screening", "extract", "Version two", "model", comment=note)
        self.assertEqual(first.comment, note)
        inherited = self.store.save_version("screening", "extract", "Version three", "model")
        self.assertEqual(inherited.comment, note)
        self.store.seed("screening", "extract", "Ignored", "ignored", comment="Do not overwrite notes")
        self.assertEqual(self.store.get_active("screening", "extract").comment, note)
        replaced = self.store.save_version("screening", "extract", "Version four", "model", comment="New usage notes")
        self.assertEqual(replaced.comment, "New usage notes")
        cleared = self.store.save_version("screening", "extract", "Version five", "model", comment="")
        self.assertEqual(cleared.comment, "")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT "COMMENT" FROM AI_PROMPTS WHERE PROMPT_ID=?', (first.id,)).fetchone()[0], note)

    def test_missing_api_key_is_a_configuration_error(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaises(AIConfigurationError):
                OpenAIService(database_path=self.db)._get_client()

    def test_validation_status_preserves_existing_model_failure_and_usage(self):
        audit = AIRequestLog(self.db)
        audit.ensure_schema()
        log_id = audit.begin(feature="screening", form_type="test", username="Test User", text="Request",
                             prompt=self.store.get_active("screening", "extract"))
        error = AIError("Original provider error", error_type="ProviderError",
                        input_tokens=12, output_tokens=6, total_tokens=18)
        audit.finish(log_id, status="failed", error=error)
        audit.mark_invalid(log_id, error_type="ValidationError", message="User-facing failure")
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute("SELECT STATUS, ERROR_TYPE, ERROR_MESSAGE, INPUT_TOKENS, OUTPUT_TOKENS, TOTAL_TOKENS FROM AI_REQUEST_LOG WHERE LOG_ID=?", (log_id,)).fetchone()
        self.assertEqual(row, ("failed", "ProviderError", "Original provider error", 12, 6, 18))

    def test_audit_initialization_only_creates_ai_request_history(self):
        audit = AIRequestLog(self.db)
        audit.ensure_schema()
        audit.ensure_schema()
        with closing(sqlite3.connect(self.db)) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )}
        self.assertEqual(tables, {"AI_PROMPTS", "AI_REQUEST_LOG"})

    def test_audit_migration_keeps_records_and_only_one_output_column(self):
        audit = AIRequestLog(self.db)
        audit.ensure_schema()
        prompt = self.store.get_active("screening", "extract")
        ids = [audit.begin(feature="screening", form_type="test", username="Test User", text=f"Request {index}", prompt=prompt)
               for index in range(3)]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for column in ["RESPONSE_ID", "JSON_OUTPUT", "RAW_OUTPUT"]:
                conn.execute(f"ALTER TABLE AI_REQUEST_LOG ADD COLUMN {column} TEXT")
            conn.execute("ALTER TABLE AI_REQUEST_LOG DROP COLUMN MODEL_OUTPUT")
            conn.execute("ALTER TABLE AI_REQUEST_LOG DROP COLUMN FULL_PROMPT")
            for column in ("INPUT_TOKENS", "OUTPUT_TOKENS", "TOTAL_TOKENS"):
                conn.execute(f"ALTER TABLE AI_REQUEST_LOG DROP COLUMN {column}")
            conn.executemany(
                "UPDATE AI_REQUEST_LOG SET RESPONSE_ID = 'unused', RAW_OUTPUT = ?, JSON_OUTPUT = ? WHERE LOG_ID = ?",
                [('  {"value": 1}  ', '{"value": 1}', ids[0]),
                 ('', '{"value": 2}', ids[1]), ('invalid model text', None, ids[2])],
            )
        audit.ensure_schema()
        audit.ensure_schema()
        with closing(sqlite3.connect(self.db)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(AI_REQUEST_LOG)")}
            rows = conn.execute("SELECT LOG_ID, PROMPT_TEXT, MODEL_OUTPUT FROM AI_REQUEST_LOG ORDER BY LOG_ID").fetchall()
        self.assertIn("MODEL_OUTPUT", columns)
        self.assertIn("FULL_PROMPT", columns)
        self.assertTrue({"INPUT_TOKENS", "OUTPUT_TOKENS", "TOTAL_TOKENS"}.issubset(columns))
        with closing(sqlite3.connect(self.db)) as conn:
            usage = conn.execute("SELECT INPUT_TOKENS, OUTPUT_TOKENS, TOTAL_TOKENS FROM AI_REQUEST_LOG").fetchall()
        self.assertEqual(usage, [(None, None, None)] * 3)
        self.assertFalse(columns & {"RESPONSE_ID", "JSON_OUTPUT", "RAW_OUTPUT"})
        self.assertEqual(rows, [(ids[0], 'Request 0', '  {"value": 1}  '),
                                (ids[1], 'Request 1', '{"value": 2}'),
                                (ids[2], 'Request 2', 'invalid model text')])

    def audit_rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM AI_REQUEST_LOG ORDER BY LOG_ID")]

    def response(self, **changes):
        return SimpleNamespace(**{
            "status": "completed", "output": [], "output_text": '{ "value": 7 }',
            "model": "actual-model", "id": "response-test",
            "usage": {"input_tokens": 20, "output_tokens": 3, "total_tokens": 23}, **changes,
        })

    def test_transport_commits_one_audit_row_before_each_actual_sdk_call(self):
        client = Mock()
        prompt = self.store.get_active("screening", "extract")
        service = OpenAIService(client=client, database_path=self.db)

        def respond(**request):
            row = self.audit_rows()[-1]
            self.assertEqual(row["STATUS"], "running")
            self.assertIsNone(row["DT_FINISHED"])
            self.assertEqual(json.loads(row["FULL_PROMPT"]), request)
            return self.response()

        client.responses.create.side_effect = respond
        kwargs = dict(prompt=prompt, payload={"request_text": "original text", "requester_username": "Alice"},
                      schema={}, schema_name="test", audit_context={"form_type": "NTNB_RUN", "chat_message_id": 42})
        first, second = service.extract(**kwargs), service.extract(**kwargs)
        rows = self.audit_rows()
        self.assertEqual(len(rows), client.responses.create.call_count)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["LOG_ID"] for row in rows], [first.log_id, second.log_id])
        self.assertNotEqual(first.log_id, second.log_id)
        for row in rows:
            self.assertEqual((row["FEATURE"], row["FORM_TYPE"], row["USERNAME"]), ("screening", "NTNB_RUN", "Alice"))
            self.assertEqual(row["PROMPT_TEXT"], "original text")
            self.assertEqual((row["RECORD_TYPE"], row["CHAT_MESSAGE_ID"]), ("REQUEST", 42))
            self.assertEqual(row["MODEL_OUTPUT"], '{ "value": 7 }')
            self.assertEqual((row["MODEL"], row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]), ("actual-model", 20, 3, 23))
            self.assertEqual((row["PROMPT_ID"], row["PROMPT_VERSION"]), (prompt.id, prompt.version))
            self.assertEqual(row["STATUS"], "completed")

    def test_conversation_tool_response_has_one_central_record_with_original_items(self):
        client = Mock()
        output = [{"type": "function_call", "name": "query_debtors", "call_id": "call-1", "arguments": "{}"}]
        client.responses.create.return_value = self.response(output=output, output_text="")
        service = OpenAIService(client=client, database_path=self.db)
        result = service.converse(prompt=self.store.get_active("screening", "extract"),
                                  items=[{"role": "user", "content": "query the cases"}], tools=[],
                                  audit_context={"username": "Bob", "form_type": "DEBTORS", "chat_message_id": 10})
        row, = self.audit_rows()
        self.assertEqual(row["LOG_ID"], result.log_id)
        self.assertEqual(json.loads(row["MODEL_OUTPUT"]), output)
        self.assertEqual((row["USERNAME"], row["PROMPT_TEXT"], row["CHAT_MESSAGE_ID"]), ("Bob", "query the cases", 10))
        self.assertEqual(json.loads(row["FULL_PROMPT"]), client.responses.create.call_args.kwargs)

    def test_all_provider_and_response_failures_keep_one_record_and_usage(self):
        client = Mock()
        service = OpenAIService(client=client, database_path=self.db)
        kwargs = dict(prompt=self.store.get_active("screening", "extract"), payload={}, schema={}, schema_name="test")
        cases = [
            ({"status": "incomplete", "output_text": '{"value":'}, "failed", "IncompleteResponse"),
            ({"output": [{"content": [{"type": "refusal", "refusal": "Cannot process"}]}], "output_text": ""}, "failed", "ModelRefusal"),
            ({"output_text": "broken JSON"}, "invalid_response", "InvalidJSON"),
            ({"output_text": "[]"}, "invalid_response", "InvalidJSONShape"),
        ]
        for response_changes, status, error_type in cases:
            with self.subTest(error_type=error_type):
                client.responses.create.return_value = self.response(**response_changes)
                before = len(self.audit_rows()) if client.responses.create.call_count else 0
                with self.assertRaises(AIError) as raised:
                    service.extract(**kwargs)
                rows = self.audit_rows()
                self.assertEqual(len(rows), before + 1)
                row = rows[-1]
                self.assertEqual(row["LOG_ID"], raised.exception.log_id)
                self.assertEqual((row["STATUS"], row["ERROR_TYPE"]), (status, error_type))
                self.assertEqual((row["INPUT_TOKENS"], row["OUTPUT_TOKENS"], row["TOTAL_TOKENS"]), (20, 3, 23))
                self.assertIsNotNone(row["DT_FINISHED"])
        client.responses.create.side_effect = RuntimeError("private provider detail")
        with self.assertRaises(AIError) as raised:
            service.extract(**kwargs)
        row = self.audit_rows()[-1]
        self.assertEqual((row["STATUS"], row["ERROR_TYPE"]), ("failed", "RuntimeError"))
        self.assertEqual(row["LOG_ID"], raised.exception.log_id)
        self.assertNotIn("private provider detail", row["ERROR_MESSAGE"])
        self.assertIsNone(row["TOTAL_TOKENS"])

    def test_audit_begin_failure_prevents_sdk_call_and_finish_failure_prevents_result_use(self):
        client = Mock()
        client.responses.create.return_value = self.response()
        service = OpenAIService(client=client, database_path=self.db)
        kwargs = dict(prompt=self.store.get_active("screening", "extract"), payload={}, schema={}, schema_name="test")
        with patch.object(AIRequestLog, "begin", side_effect=sqlite3.OperationalError("private DB detail")):
            with self.assertRaises(sqlite3.Error):
                service.extract(**kwargs)
        client.responses.create.assert_not_called()
        self.assertEqual(self.audit_rows(), [])
        with patch.object(AIRequestLog, "finish", side_effect=sqlite3.OperationalError("private DB detail")):
            with self.assertRaises(AIError) as raised:
                service.extract(**kwargs)
        self.assertEqual(client.responses.create.call_count, 1)
        row, = self.audit_rows()
        self.assertEqual(row["STATUS"], "running")
        self.assertEqual(raised.exception.error_type, "AuditPersistenceError")
        self.assertEqual(raised.exception.log_id, row["LOG_ID"])
        self.assertEqual((raised.exception.output_text, raised.exception.total_tokens), ('{ "value": 7 }', 23))
        self.assertNotIn("private DB detail", str(raised.exception))

    def test_nullable_prompt_migration_preserves_records_indexes_views_and_sequence(self):
        audit = AIRequestLog(self.db)
        audit.ensure_schema()
        # Reproduce the historical NOT NULL provenance constraints.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE AI_REQUEST_LOG")
            conn.execute("""CREATE TABLE AI_REQUEST_LOG (
                LOG_ID INTEGER PRIMARY KEY AUTOINCREMENT, FEATURE TEXT NOT NULL,
                FORM_TYPE TEXT NOT NULL,USERNAME TEXT NOT NULL,PROMPT_TEXT TEXT NOT NULL,
                DT_STARTED TEXT NOT NULL,DT_FINISHED TEXT,PROMPT_ID INTEGER NOT NULL,
                PROMPT_VERSION INTEGER NOT NULL,MODEL TEXT,MODEL_OUTPUT TEXT,STATUS TEXT NOT NULL,
                ERROR_TYPE TEXT,ERROR_MESSAGE TEXT)""")
            conn.execute("CREATE INDEX custom_audit_status ON AI_REQUEST_LOG(STATUS)")
            conn.execute("CREATE VIEW custom_audit_view AS SELECT LOG_ID,STATUS FROM AI_REQUEST_LOG")
            conn.execute("""INSERT INTO AI_REQUEST_LOG
                (LOG_ID,FEATURE,FORM_TYPE,USERNAME,PROMPT_TEXT,DT_STARTED,PROMPT_ID,PROMPT_VERSION,STATUS)
                VALUES(100,'rfq','sovereign','Alice','preserved','2026-11-04',1,1,'completed')""")
            conn.execute("UPDATE sqlite_sequence SET seq=150 WHERE name='AI_REQUEST_LOG'")
        audit.ensure_schema()
        audit.ensure_schema()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            columns = {row[1]: row for row in conn.execute("PRAGMA table_info(AI_REQUEST_LOG)")}
            self.assertFalse(columns["PROMPT_ID"][3])
            self.assertFalse(columns["PROMPT_VERSION"][3])
            self.assertEqual(conn.execute("SELECT PROMPT_TEXT,RECORD_TYPE FROM AI_REQUEST_LOG WHERE LOG_ID=100").fetchone(), ("preserved", "REQUEST"))
            self.assertEqual(conn.execute("SELECT * FROM custom_audit_view").fetchall(), [(100, "completed")])
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(AI_REQUEST_LOG)")}
            self.assertIn("custom_audit_status", indexes)
            self.assertIn("idx_ai_request_log_legacy_chat", indexes)
            values = ('routines', 'NTNB_RUN', 'Alice', 'legacy interaction', '2026-11-04', 'completed', 'LEGACY_AGGREGATE', 42)
            conn.execute("""INSERT INTO AI_REQUEST_LOG
                (FEATURE,FORM_TYPE,USERNAME,PROMPT_TEXT,DT_STARTED,STATUS,RECORD_TYPE,CHAT_MESSAGE_ID)
                VALUES(?,?,?,?,?,?,?,?)""", values)
            legacy_id = conn.execute("SELECT LOG_ID FROM AI_REQUEST_LOG WHERE CHAT_MESSAGE_ID=42").fetchone()[0]
            self.assertGreater(legacy_id, 150)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("""INSERT INTO AI_REQUEST_LOG
                    (FEATURE,FORM_TYPE,USERNAME,PROMPT_TEXT,DT_STARTED,STATUS,RECORD_TYPE,CHAT_MESSAGE_ID)
                    VALUES(?,?,?,?,?,?,?,?)""", values)
        log_id = audit.begin(feature="screening", form_type="test", username="Alice", text="actual request",
                             prompt=self.store.get_active("screening", "extract"), chat_message_id=42)
        self.assertGreater(log_id, legacy_id)

    def test_default_database_path_remains_mandatory_and_sdk_access_is_centralized(self):
        import ast
        from services.openai_client import DEFAULT_DATABASE_PATH
        self.assertEqual(OpenAIService().database_path, DEFAULT_DATABASE_PATH)
        root = Path(__file__).resolve().parents[1]
        call_sites = []
        for directory in (root / "services", root / "pages"):
            for path in directory.glob("*.py"):
                for node in ast.walk(ast.parse(path.read_text())):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "create" and isinstance(node.func.value, ast.Attribute)
                            and node.func.value.attr == "responses"):
                        call_sites.append(path.relative_to(root).as_posix())
        self.assertEqual(call_sites, ["services/openai_client.py"])


if __name__ == "__main__":
    unittest.main()
