"""Reusable OpenAI Responses client, independent of Dash and RFQ controls."""

import json
import os
from dataclasses import dataclass


class AIError(Exception):
    """An AI request could not produce a usable result."""

    def __init__(self, message, *, output_text=None, response_id=None, model=None, error_type=None,
                 input_tokens=None, output_tokens=None, total_tokens=None):
        super().__init__(message)
        self.output_text = output_text
        self.response_id = response_id
        self.model = model
        self.error_type = error_type
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens


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


class OpenAIService:
    def __init__(self, *, client=None):
        self._client = client

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

    def extract(self, *, prompt, payload, schema, schema_name):
        """One stateless request with a strict schema and no tools or chat history."""
        if not prompt.model or not prompt.model.strip():
            raise AIConfigurationError("The active prompt has no model configured in AI_PROMPTS.")
        if not prompt.instructions or not prompt.instructions.strip():
            raise AIConfigurationError("The active prompt has no instructions configured in AI_PROMPTS.")
        client = self._get_client()
        try:
            request = build_structured_request(prompt=prompt, payload=payload, schema=schema, schema_name=schema_name)
            response = client.responses.create(**request)
        except Exception as exc:
            # Never expose SDK errors, credentials, or supplied request text in the UI.
            raise AIError("The AI service could not complete the request. Please try again.",
                          error_type=type(exc).__name__) from exc
        usage = _response_token_usage(response)
        response_details = {
            "output_text": getattr(response, "output_text", None),
            "response_id": getattr(response, "id", None),
            "model": getattr(response, "model", None),
            **usage,
        }
        if response.status != "completed":
            raise AIError("The AI response was incomplete. Please try again.",
                          error_type="IncompleteResponse", **response_details)
        for item in response.output:
            for part in getattr(item, "content", None) or ():
                if getattr(part, "type", None) == "refusal":
                    response_details["output_text"] = getattr(part, "refusal", None) or response_details["output_text"]
                    raise AIError("The model could not process this request. Please try again.",
                                  error_type="ModelRefusal", **response_details)
        try:
            data = json.loads(response.output_text, parse_constant=_reject_json_constant)
        except (TypeError, ValueError) as exc:
            raise AIError("The AI returned an unreadable result. Please try again.",
                          error_type="InvalidJSON", **response_details) from exc
        if not isinstance(data, dict):
            raise AIError("The AI returned an invalid result. Please try again.",
                          error_type="InvalidJSONShape", **response_details)
        return StructuredResult(data, response.model, response.id, prompt.id, prompt.version,
                                response.output_text, **usage)


def _response_token_usage(response):
    """Read API-reported counts; missing usage is unknown rather than zero."""
    usage = getattr(response, "usage", None)
    counts = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        counts[name] = value if type(value) is int and value >= 0 else None
    return counts


def _reject_json_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")
