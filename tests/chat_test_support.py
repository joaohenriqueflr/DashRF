"""Chat Completions fixtures without SDK or network dependencies."""

from types import SimpleNamespace


def chat_completion(content="", *, tool_calls=None, refusal=None, finish_reason="stop",
                    usage=None, model="test-model", response_id="chat-test"):
    message = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls, refusal=refusal)
    return SimpleNamespace(id=response_id, model=model, usage=usage,
                           choices=[SimpleNamespace(index=0, message=message, finish_reason=finish_reason)])
