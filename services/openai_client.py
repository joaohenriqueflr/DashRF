"""Shared OpenAI Responses transport with mandatory per-request SQLite auditing."""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from services.ai_audit import AIRequestLog

logger = logging.getLogger(__name__)
DEFAULT_DATABASE_PATH = Path(__file__).resolve().parents[1] / "data" / "fixed_income.db"


class AIError(Exception):
    """An AI request could not produce a usable result."""

    def __init__(self, message, *, output_text=None, response_id=None, model=None, error_type=None,
                 input_tokens=None, output_tokens=None, total_tokens=None, log_id=None):
        super().__init__(message)
        self.output_text = output_text
        self.response_id = response_id
        self.model = model
        self.error_type = error_type
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens
        self.log_id = log_id


class AIConfigurationError(AIError):
    """The SDK, API key, or prompt configuration is missing."""


@dataclass(frozen=True)
class StructuredResult:
    data: dict
    model: str
    response_id: str
    prompt_id: int
    prompt_version: int
    output_text: str = None
    input_tokens: int = None
    output_tokens: int = None
    total_tokens: int = None
    log_id: int = None


@dataclass(frozen=True)
class ConversationResult:
    """Full Responses items, including call IDs and opaque reasoning state."""

    items: list
    output_text: str
    response_id: str
    model: str
    input_tokens: int = None
    output_tokens: int = None
    total_tokens: int = None
    log_id: int = None


def build_structured_request(*, prompt, payload, schema, schema_name):
    """The exact credential-free request body used by both transport and audit."""
    return {
        "model": (prompt.model or "").strip(),
        "instructions": prompt.instructions,
        "input": json.dumps(payload, ensure_ascii=False, allow_nan=False),
        "text": {"format": {
            "type": "json_schema", "name": schema_name, "schema": schema, "strict": True,
        }},
        "max_output_tokens": 4000,
        "store": False,
        "metadata": {"feature": prompt.feature, "prompt_key": prompt.key,
                     "prompt_id": str(prompt.id), "prompt_version": str(prompt.version)},
    }


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _output_items(response):
    return [item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump")
            else dict(item) for item in (_field(response, "output") or [])]


def _request_context(prompt, payload, items, audit_context):
    context = dict(audit_context or {})
    payload = payload if isinstance(payload, dict) else {}
    text = context.get("text")
    if text is None:
        text = payload.get("request_text", payload.get("text"))
    if text is None and items:
        for item in reversed(items):
            if _field(item, "role") == "user":
                text = _field(item, "content", "")
                break
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, allow_nan=False)
    return {
        "feature": prompt.feature,
        "form_type": context.get("form_type") or prompt.key,
        "username": context.get("username") or payload.get("requester_username") or "system",
        "text": text,
        "chat_message_id": context.get("chat_message_id"),
    }


class OpenAIService:
    def __init__(self, *, client=None, database_path=None):
        self._client = client
        self.database_path = Path(database_path) if database_path is not None else DEFAULT_DATABASE_PATH

    def _get_client(self):
        if self._client is None:
            if not os.environ.get("OPENAI_API_KEY"):
                raise AIConfigurationError("OPENAI_API_KEY is not configured.")
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise AIConfigurationError("Install requirements-ai.txt in the app environment.") from exc
            self._client = OpenAI(timeout=45.0, max_retries=0)
        return self._client

    @staticmethod
    def _validate_prompt(prompt):
        if not prompt.model or not prompt.model.strip():
            raise AIConfigurationError("The active prompt has no model configured in AI_PROMPTS.")
        if not prompt.instructions or not prompt.instructions.strip():
            raise AIConfigurationError("The active prompt has no instructions configured in AI_PROMPTS.")

    @staticmethod
    def _finish_audit(audit, log_id, *, status, result=None, error=None):
        try:
            audit.finish(log_id, status=status, result=result, error=error)
        except Exception as exc:
            # The committed running row survives. Do not issue a second API call
            # or hand an unaudited response to an operational write workflow.
            logger.error("Unable to finish AI request audit %s.", log_id)
            source = result if result is not None else error
            output_text = getattr(source, "output_text", None)
            if result is not None and hasattr(result, "items"):
                output_text = json.dumps(result.items, ensure_ascii=False, allow_nan=False)
            raise AIError("The AI response could not be recorded. Please check the request history before retrying.",
                          error_type="AuditPersistenceError", log_id=log_id,
                          output_text=output_text,
                          response_id=getattr(source, "response_id", None), model=getattr(source, "model", None),
                          **{name: getattr(source, name, None) for name in ("input_tokens", "output_tokens", "total_tokens")}) from exc

    def _request(self, *, prompt, request, parse, payload=None, items=None, audit_context=None):
        """The only SDK access point; begin is committed before any API access."""
        audit = AIRequestLog(self.database_path)
        audit.ensure_schema()
        context = _request_context(prompt, payload, items, audit_context)
        log_id = audit.begin(prompt=prompt, full_prompt=request, **context)
        details = {"log_id": log_id}
        try:
            client = self._get_client()
            try:
                response = client.responses.create(**request)
            except Exception as exc:
                raise AIError("The AI service could not complete the request. Please try again.",
                              error_type=type(exc).__name__, log_id=log_id) from exc
            details.update(output_text=_field(response, "output_text"),
                           response_id=_field(response, "id"), model=_field(response, "model"),
                           **_response_token_usage(response))
            if not details["output_text"] and _field(response, "output"):
                # Incomplete tool responses may have no aggregate output_text.
                # Retain their original structured output for audit/recovery.
                try:
                    details["output_text"] = json.dumps(_output_items(response), ensure_ascii=False, allow_nan=False)
                except (TypeError, ValueError):
                    pass
            if _field(response, "status") != "completed":
                raise AIError("The AI response was incomplete. Please try again.",
                              error_type="IncompleteResponse", **details)
            for item in _field(response, "output") or []:
                for part in _field(item, "content") or []:
                    if _field(part, "type") == "refusal":
                        details["output_text"] = _field(part, "refusal") or details["output_text"]
                        raise AIError("The model could not process this request. Please try again.",
                                      error_type="ModelRefusal", **details)
            result = parse(response, details)
        except AIError as exc:
            exc.log_id = log_id
            status = "invalid_response" if exc.error_type in {"InvalidJSON", "InvalidJSONShape", "InvalidResponse"} else "failed"
            self._finish_audit(audit, log_id, status=status, error=exc)
            raise
        except Exception as exc:
            error = AIError("The AI returned an unreadable response. Please try again.",
                            error_type="InvalidResponse", **details)
            self._finish_audit(audit, log_id, status="invalid_response", error=error)
            raise error from exc
        self._finish_audit(audit, log_id, status="completed", result=result)
        return result

    def converse(self, *, prompt, items, tools, audit_context=None):
        """Replay conversation through the same audited transport used by RFQ."""
        self._validate_prompt(prompt)
        request = {
            "model": prompt.model.strip(), "instructions": prompt.instructions,
            "input": items, "tools": tools, "parallel_tool_calls": False,
            "include": ["reasoning.encrypted_content"], "store": False,
            "max_output_tokens": 4000,
            "metadata": {"feature": prompt.feature, "prompt_key": prompt.key,
                         "prompt_id": str(prompt.id), "prompt_version": str(prompt.version)},
        }

        def parse(response, details):
            try:
                output = _output_items(response)
            except (TypeError, ValueError) as exc:
                raise AIError("The AI returned an unreadable response. Please try again.",
                              error_type="InvalidResponse", **details) from exc
            return ConversationResult(output, _field(response, "output_text", ""), details["response_id"], details["model"],
                                      **{name: details[name] for name in ("input_tokens", "output_tokens", "total_tokens", "log_id")})

        return self._request(prompt=prompt, request=request, parse=parse, items=items, audit_context=audit_context)

    def extract(self, *, prompt, payload, schema, schema_name, audit_context=None):
        """One strict structured-output request, centrally audited before parsing."""
        self._validate_prompt(prompt)
        request = build_structured_request(prompt=prompt, payload=payload, schema=schema, schema_name=schema_name)

        def parse(response, details):
            try:
                data = json.loads(_field(response, "output_text"), parse_constant=_reject_json_constant)
            except (TypeError, ValueError) as exc:
                raise AIError("The AI returned an unreadable result. Please try again.",
                              error_type="InvalidJSON", **details) from exc
            if not isinstance(data, dict):
                raise AIError("The AI returned an invalid result. Please try again.",
                              error_type="InvalidJSONShape", **details)
            return StructuredResult(data, details["model"], details["response_id"], prompt.id, prompt.version,
                                    _field(response, "output_text"),
                                    **{name: details[name] for name in ("input_tokens", "output_tokens", "total_tokens", "log_id")})

        return self._request(prompt=prompt, request=request, parse=parse, payload=payload, audit_context=audit_context)


def _response_token_usage(response):
    """Read API-reported counts; missing usage is unknown rather than zero."""
    usage = _field(response, "usage")
    counts = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        value = _field(usage, name)
        counts[name] = value if type(value) is int and value >= 0 else None
    return counts


def _reject_json_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")
