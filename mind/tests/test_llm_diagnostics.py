"""Observe cleanup and malformed reply boundaries without retaining content."""
import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from conftest import run
from leeroy_mind.llm.client import ChatResult, LLMError, OllamaClient


def chat_reply(out):
    client = OllamaClient("http://unused.invalid")

    async def call(path, body, timeout):
        assert path == "/api/chat"
        assert body == {"model": "test-model", "messages": [{"role": "user", "content": "hello"}],
                        "stream": False, "options": {"stop": ["\n\n"]}, "think": False}
        return out

    client._call = call
    return run(client.chat("test-model", [{"role": "user", "content": "hello"}],
                           options={"stop": ["\n\n"]}, think=False))


@pytest.mark.parametrize("message,kind,chars,text", [
    ({}, "missing", None, ""),
    ({"content": ""}, "empty", 0, ""),
    ({"content": " \n\t"}, "whitespace", 3, ""),
    ({"content": "<think>NEVER_CAPTURE_THINKING</think>"}, "text", 37, ""),
    ({"content": "<think>NEVER_CAPTURE_THINKING"}, "text", 29, ""),
    ({"content": "  hello  "}, "text", 9, "hello"),
])
def test_raw_shape_survives_text_cleanup(message, kind, chars, text):
    result = chat_reply({"message": message, "done": True, "done_reason": "stop", "eval_count": 1})
    assert result.text == text
    assert result.diagnostics.content_kind == kind
    assert result.diagnostics.content_chars == chars
    assert result.diagnostics.done is True and result.diagnostics.done_reason == "stop"
    assert "NEVER_CAPTURE" not in json.dumps(asdict(result.diagnostics))
    # Existing fake clients and four-argument construction/equality stay compatible.
    assert result == ChatResult(result.text, result.thinking, result.eval_count, result.seconds)


@pytest.mark.parametrize("out,message_kind,content_kind", [
    ({}, "missing", "missing"),
    ({"message": None}, "null", "missing"),
    ({"message": "NEVER_CAPTURE_MESSAGE"}, "invalid", "missing"),
    ({"message": {"content": None}}, "object", "null"),
    ({"message": {"content": {"secret": "NEVER_CAPTURE_CONTENT"}}}, "object", "invalid"),
])
def test_malformed_message_keeps_existing_error_and_safe_evidence(out, message_kind, content_kind):
    with pytest.raises(LLMError, match="no message in reply") as raised:
        chat_reply(out)
    data = asdict(raised.value.diagnostics)
    assert data["message_kind"] == message_kind and data["content_kind"] == content_kind
    assert "NEVER_CAPTURE" not in json.dumps(data)


def test_untrusted_metadata_is_allowlisted_and_strictly_numeric():
    result = chat_reply({"message": {"content": "hi", "thinking": "NEVER_CAPTURE_THINKING"},
        "done": 1, "done_reason": "NEVER_CAPTURE_REASON", "eval_count": True,
        "prompt_eval_count": -1, "total_duration": float("nan"), "load_duration": "NEVER_CAPTURE_TIME",
        "prompt_eval_duration": 1 << 100, "eval_duration": 321})
    data = asdict(result.diagnostics)
    assert data["done"] is None and data["done_reason"] == "other"
    assert all(data[name] is None for name in ("eval_count", "prompt_eval_count", "total_duration_ns",
                                               "load_duration_ns", "prompt_eval_duration_ns"))
    assert data["eval_duration_ns"] == 321 and data["thinking_kind"] == "text"
    assert "NEVER_CAPTURE" not in json.dumps(data, allow_nan=False)
    with pytest.raises(FrozenInstanceError):
        result.diagnostics.done = False
    with pytest.raises(ValueError):
        replace(result.diagnostics, done_reason="NEVER_CAPTURE_REASON")
    with pytest.raises(ValueError):
        replace(result.diagnostics, content_chars=True)


def test_normal_transport_error_needs_no_diagnostics():
    error = LLMError("existing failure")
    assert error.diagnostics is None and str(error) == "existing failure"
