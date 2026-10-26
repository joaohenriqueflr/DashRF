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
from services.ai_audit import AIRequestLog, FormEventLog


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
        service = OpenAIService(client=client)
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
        service = OpenAIService(client=client)
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
        service = OpenAIService(client=client)
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
                OpenAIService(client=client).extract(prompt=invalid, payload={}, schema={}, schema_name="test")
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
                OpenAIService()._get_client()

    def test_form_event_preserves_existing_model_failure_and_usage(self):
        audit = AIRequestLog(self.db)
        events = FormEventLog(self.db)
        audit.ensure_schema()
        events.ensure_schema()
        log_id = audit.begin(feature="screening", form_type="test", username="Test User", text="Request",
                             prompt=self.store.get_active("screening", "extract"))
        error = AIError("Original provider error", error_type="ProviderError",
                        input_tokens=12, output_tokens=6, total_tokens=18)
        audit.finish(log_id, status="failed", error=error)
        events.record(feature="screening", form_type="test", username="Test User", level="error",
                      source="test_form", message="User-facing failure", ai_log_id=log_id)
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute("SELECT STATUS, ERROR_TYPE, ERROR_MESSAGE, INPUT_TOKENS, OUTPUT_TOKENS, TOTAL_TOKENS FROM AI_REQUEST_LOG WHERE LOG_ID=?", (log_id,)).fetchone()
        self.assertEqual(row, ("failed", "ProviderError", "Original provider error", 12, 6, 18))

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


if __name__ == "__main__":
    unittest.main()
