from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from chat_test_support import chat_completion
from services.openai_client import AIConfigurationError, AIError, OpenAIService
from services.ai_audit import AIRequestLog
from services.prompts import PromptStore


class ConnectionFailure(Exception):
    pass


class StatusFailure(Exception):
    def __init__(self, status):
        super().__init__("private-provider-details")
        self.status_code = status


class OpenAIAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "test.db"
        self.config_path = self.root / ".env"
        for context in [patch.dict(os.environ, {}, clear=True),
                        patch("services.openai_client.DEFAULT_CONFIG_PATH", self.config_path)]:
            context.start()
            self.addCleanup(context.stop)
        self.ubs, self.personal = Mock(), Mock()
        self.openai = ModuleType("openai")
        self.openai.OpenAI = Mock(side_effect=lambda **kw: self.ubs if "http_client" in kw else self.personal)
        self.openai.APIConnectionError = ConnectionFailure
        self.openai.APIStatusError = StatusFailure
        self.truststore, self.httpx, self.aiaas = [ModuleType(name) for name in ("truststore", "httpx", "aiaas_auth")]
        self.truststore.inject_into_ssl = Mock()
        self.httpx.Client = Mock()
        self.aiaas.AuthMode = SimpleNamespace(DEVPOD=object())
        self.aiaas.AzureAuthClient = Mock()
        self.auth = self.aiaas.AzureAuthClient.return_value
        self.auth.authenticate_broker.return_value = SimpleNamespace(access_token="ubs-token")
        modules = patch.dict("sys.modules", {"openai": self.openai, "truststore": self.truststore,
                                              "httpx": self.httpx, "aiaas_auth": self.aiaas})
        modules.start()
        self.addCleanup(modules.stop)

    def configure_ubs(self):
        os.environ.update(UBS_BROKER_URL="https://broker.example.test/",
                          UBS_OPENAI_BASE_URL="https://gateway.example.test/aiaas/v1",
                          UBS_BROKER_VERSION="", UBS_SSL_VERIFY="false")

    def test_ubs_priority_matches_script_and_refreshes_tokens(self):
        self.configure_ubs()
        os.environ["OPENAI_API_KEY"] = "personal-key"
        service = OpenAIService()
        self.assertIs(service._get_client(), self.ubs)
        self.aiaas.AzureAuthClient.assert_called_once_with(
            broker_url="https://broker.example.test/", mode=self.aiaas.AuthMode.DEVPOD, broker_version="")
        self.httpx.Client.assert_called_once_with(verify=False, timeout=45.0)
        self.truststore.inject_into_ssl.assert_called_once_with()
        self.openai.OpenAI.assert_called_once_with(
            base_url="https://gateway.example.test/aiaas/v1", api_key="ubs-token",
            http_client=self.httpx.Client.return_value, timeout=45.0, max_retries=0)
        self.auth.authenticate_broker.return_value.access_token = "renewed-token"
        self.assertIs(service._get_client(), self.ubs)
        self.assertEqual(self.ubs.api_key, "renewed-token")
        self.assertEqual(self.auth.authenticate_broker.call_count, 2)
        self.openai.OpenAI.assert_called_once()

    def test_ubs_needs_no_personal_key_and_defaults_to_verified_tls(self):
        self.configure_ubs()
        del os.environ["UBS_SSL_VERIFY"]
        os.environ["UBS_BROKER_VERSION"] = "work-version"
        self.assertIs(OpenAIService()._get_client(), self.ubs)
        self.httpx.Client.assert_called_once_with(verify=True, timeout=45.0)
        self.assertEqual(self.aiaas.AzureAuthClient.call_args.kwargs["broker_version"], "work-version")

    def test_personal_key_has_fixed_destination_and_needs_no_work_packages(self):
        os.environ.update(OPENAI_API_KEY=" personal-key ", OPENAI_BASE_URL="https://wrong.example.test")
        with patch.dict("sys.modules", {"truststore": None, "aiaas_auth": None}):
            service = OpenAIService()
            self.assertIs(service._get_client(), self.personal)
            self.assertIs(service._get_client(), self.personal)
        self.openai.OpenAI.assert_called_once_with(
            base_url="https://api.openai.com/v1", api_key="personal-key", timeout=45.0, max_retries=0)
        self.httpx.Client.assert_not_called()

    def test_broker_failure_falls_back_and_next_operation_retries_ubs(self):
        self.configure_ubs()
        os.environ["OPENAI_API_KEY"] = "personal-key"
        self.auth.authenticate_broker.side_effect = RuntimeError("sensitive-broker-detail")
        service = OpenAIService()
        with self.assertLogs("services.openai_client", level="WARNING") as logs:
            self.assertIs(service._get_client(), self.personal)
        self.assertNotIn("sensitive-broker-detail", " ".join(logs.output))
        self.auth.authenticate_broker.side_effect = None
        self.assertIs(service._get_client(), self.ubs)

    def test_missing_work_packages_or_partial_config_allow_fallback(self):
        self.configure_ubs()
        os.environ["OPENAI_API_KEY"] = "personal-key"
        with patch.dict("sys.modules", {"aiaas_auth": None}):
            self.assertIs(OpenAIService()._get_client(), self.personal)
        del os.environ["UBS_BROKER_URL"]
        self.assertIs(OpenAIService()._get_client(), self.personal)
        self.auth.authenticate_broker.assert_not_called()

    def test_unavailable_ubs_without_key_is_sanitized(self):
        self.configure_ubs()
        self.auth.authenticate_broker.side_effect = RuntimeError("sensitive-broker-detail")
        with self.assertRaises(AIConfigurationError) as raised:
            OpenAIService()._get_client()
        self.assertNotIn("sensitive-broker-detail", str(raised.exception))
        self.openai.OpenAI.assert_not_called()

    def test_empty_token_and_invalid_tls_setting_prevent_gateway_access(self):
        self.configure_ubs()
        for token, verify in [("", "true"), (None, "true"), ("token", "invalid")]:
            self.auth.authenticate_broker.return_value.access_token = token
            os.environ["UBS_SSL_VERIFY"] = verify
            with self.assertRaises(AIConfigurationError):
                OpenAIService()._get_client()
        self.openai.OpenAI.assert_not_called()

    def test_failed_client_initialization_closes_transport(self):
        self.configure_ubs()
        self.openai.OpenAI.side_effect = ValueError("private-detail")
        with self.assertRaises(AIConfigurationError):
            OpenAIService()._get_client()
        self.httpx.Client.return_value.close.assert_called_once()

    def test_missing_config_and_injected_clients(self):
        with self.assertRaisesRegex(AIConfigurationError, "OPENAI_API_KEY"):
            OpenAIService()._get_client()
        client = Mock()
        with patch.object(OpenAIService, "_load_settings", side_effect=AssertionError):
            self.assertIs(OpenAIService(client=client)._get_client(), client)

    @unittest.skipUnless(importlib.util.find_spec("dotenv"), "Requires python-dotenv")
    def test_file_loading_and_environment_precedence_without_global_mutation(self):
        self.config_path.write_text('OPENAI_API_KEY="personal-${LITERAL}-key"\n', encoding="utf-8")
        self.assertIs(OpenAIService()._get_client(), self.personal)
        self.assertEqual(self.openai.OpenAI.call_args.kwargs["api_key"], "personal-${LITERAL}-key")
        self.assertNotIn("OPENAI_API_KEY", os.environ)
        os.environ["OPENAI_API_KEY"] = "env-key"
        OpenAIService()._get_client()
        self.assertEqual(self.openai.OpenAI.call_args.kwargs["api_key"], "env-key")
        os.environ["OPENAI_API_KEY"] = ""
        with self.assertRaises(AIConfigurationError):
            OpenAIService()._get_client()

    def setup_request(self):
        self.configure_ubs()
        os.environ["OPENAI_API_KEY"] = "personal-key"
        store = PromptStore(self.db)
        store.seed("test", "extract", "Extract fields", "model")
        self.ubs.chat.completions.create.return_value = self.response()
        self.personal.chat.completions.create.return_value = self.response()
        self.service = OpenAIService(database_path=self.db)
        self.request = dict(prompt=store.get_active("test", "extract"), payload={"text": "hello"},
                            schema={"type": "object"}, schema_name="test")

    @staticmethod
    def response():
        return chat_completion('{"value": 7}', model="model", response_id="id")

    def audit_rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM AI_REQUEST_LOG ORDER BY LOG_ID")]

    def test_gateway_fallback_has_committed_audits_and_no_secret_leaks(self):
        self.setup_request()
        self.ubs.chat.completions.create.side_effect = StatusFailure(503)

        def personal_request(**kwargs):
            self.assertEqual([row["STATUS"] for row in self.audit_rows()], ["failed", "running"])
            return self.response()

        self.personal.chat.completions.create.side_effect = personal_request
        result = self.service.extract(**self.request)
        rows = self.audit_rows()
        self.assertEqual([row["STATUS"] for row in rows], ["failed", "completed"])
        self.assertEqual(result.log_id, rows[1]["LOG_ID"])
        self.assertEqual(self.ubs.chat.completions.create.call_count, 1)
        self.assertEqual(self.personal.chat.completions.create.call_count, 1)
        for secret in ["personal-key", "ubs-token", "broker.example.test", "gateway.example.test", "private-provider-details"]:
            self.assertNotIn(secret, json.dumps(rows))

    def test_connection_and_auth_failures_allow_one_fallback(self):
        self.setup_request()
        for error in [ConnectionFailure(), StatusFailure(401), StatusFailure(403), StatusFailure(408), StatusFailure(429)]:
            self.ubs.chat.completions.create.side_effect = error
            self.service.extract(**self.request)
        self.assertEqual(self.personal.chat.completions.create.call_count, 5)

    def test_bad_requests_and_invalid_outputs_do_not_fallback(self):
        self.setup_request()
        for error in [StatusFailure(400), StatusFailure(404), RuntimeError("private-details")]:
            self.ubs.chat.completions.create.side_effect = error
            with self.assertRaises(AIError):
                self.service.extract(**self.request)
        self.ubs.chat.completions.create.side_effect = None
        self.ubs.chat.completions.create.return_value.choices[0].message.content = "invalid-json"
        with self.assertRaises(AIError):
            self.service.extract(**self.request)
        self.personal.chat.completions.create.assert_not_called()

    def test_audit_failure_prevents_fallback(self):
        self.setup_request()
        self.ubs.chat.completions.create.side_effect = StatusFailure(503)
        with patch.object(AIRequestLog, "finish", side_effect=sqlite3.OperationalError("private-db-detail")):
            with self.assertRaises(AIError) as raised:
                self.service.extract(**self.request)
        self.assertEqual(raised.exception.error_type, "AuditPersistenceError")
        self.personal.chat.completions.create.assert_not_called()

    def test_personal_failure_is_not_retried_and_no_key_disables_fallback(self):
        self.setup_request()
        self.ubs.chat.completions.create.side_effect = StatusFailure(503)
        self.personal.chat.completions.create.side_effect = ConnectionFailure()
        with self.assertRaises(AIError):
            self.service.extract(**self.request)
        self.assertEqual([row["STATUS"] for row in self.audit_rows()], ["failed", "failed"])
        self.personal.chat.completions.create.assert_called_once()
        self.personal.chat.completions.create.reset_mock()
        os.environ["OPENAI_API_KEY"] = ""
        with self.assertRaises(AIError):
            OpenAIService(database_path=self.db).extract(**self.request)
        self.personal.chat.completions.create.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("openai") and importlib.util.find_spec("httpx")
                     and importlib.util.find_spec("dotenv") and importlib.util.find_spec("truststore"),
                     "Install requirements-ai.txt with Python 3.10+ for SDK integration tests")
class RealSDKAuthenticationTests(unittest.TestCase):
    def test_actual_sdk_uses_separate_credentials_and_audits_on_gateway_fallback(self):
        from importlib.metadata import version
        if int(version("openai").split(".")[0]) != 2:
            self.skipTest("Install the OpenAI SDK 2.x version declared in requirements-ai.txt")
        import httpx

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".env"
            config.write_text(
                'UBS_BROKER_URL=https://broker.example.test/\n'
                'UBS_OPENAI_BASE_URL=https://gateway.example.test/aiaas/v1\n'
                'UBS_SSL_VERIFY=false\nOPENAI_API_KEY=personal-key\n', encoding="utf-8")
            store = PromptStore(root / "test.db")
            store.seed("test", "extract", "Extract fields", "model")
            broker = ModuleType("aiaas_auth")
            broker.AuthMode = SimpleNamespace(DEVPOD="devpod")
            broker.AzureAuthClient = Mock()
            broker.AzureAuthClient.return_value.authenticate_broker.return_value = SimpleNamespace(access_token="ubs-token")
            sent = []

            def send(client, request, **kwargs):
                sent.append((str(request.url), request.headers["authorization"]))
                if request.url.host == "gateway.example.test":
                    return httpx.Response(503, request=request, json={"error": {"message": "unavailable"}})
                self.assertEqual(str(request.url), "https://api.openai.com/v1/chat/completions")
                return httpx.Response(200, request=request, json={
                    "id": "chat-id", "created": 0, "object": "chat.completion", "model": "model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": '{"value": 7}'}}],
                })

            # Stub the broker and HTTP send boundary; all SDK construction/parsing is real.
            with patch.dict(os.environ, {}, clear=True), patch.dict("sys.modules", {"aiaas_auth": broker}), \
                    patch("truststore.inject_into_ssl"), patch("httpx.Client.send", autospec=True, side_effect=send):
                service = OpenAIService(database_path=root / "test.db", config_path=config)
                try:
                    result = service.extract(prompt=store.get_active("test", "extract"), payload={}, schema={}, schema_name="test")
                    self.assertEqual(result.data, {"value": 7})
                    self.assertEqual(sent, [("https://gateway.example.test/aiaas/v1/chat/completions", "Bearer ubs-token"),
                                            ("https://api.openai.com/v1/chat/completions", "Bearer personal-key")])
                    with closing(sqlite3.connect(root / "test.db")) as conn:
                        self.assertEqual(conn.execute("SELECT STATUS FROM AI_REQUEST_LOG ORDER BY LOG_ID").fetchall(),
                                         [("failed",), ("completed",)])
                finally:
                    if service._ubs_client is not None:
                        service._ubs_client.close()
                    if service._personal_client is not None:
                        service._personal_client.close()


if __name__ == "__main__":
    unittest.main()
