import asyncio
import json
from types import SimpleNamespace

import pytest

from conftest import run
from nightly.conftest import IDENT
from leeroy_mind.eval.probe_diagnostics import diagnose_secret_probes
from leeroy_mind.eval.probes import ProbeResult, SECRET_PROBES, probe_messages
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.testing.fake_llm import FakeLLM


def test_diagnostic_keeps_entire_schedule_without_retrying_or_opening_gate(h):
    count = 0

    def script(*_):
        nonlocal count
        count += 1
        if count == 2:
            return "  "
        if count == 3:
            raise LLMError("NEVER_CAPTURE_ERROR")
        return "(nothing)" if count == 1 else "NEVER_CAPTURE_ANSWER"

    llm = FakeLLM(script=script)
    result = run(diagnose_secret_probes(llm, "NEVER_CAPTURE_MODEL", IDENT))
    assert result["complete"] is True and result["attempted"] == 36 and result["returned"] == 35
    assert result["counts"] == {"speech": 33, "explicit_abstention": 1, "empty": 1, "technical_failure": 1}
    assert result["diagnostic_only"] and not result["certifies_gate"] and not result["privacy_assessed"]
    assert result["model_digest"] is None and not isinstance(result, ProbeResult)
    assert len(llm.calls) == 36 and llm.inflight_peak == 1
    assert "NEVER_CAPTURE" not in json.dumps(result)
    assert h.count("probe_run") == 0
    for index, call in enumerate(llm.calls):
        assert call["messages"] == probe_messages(SECRET_PROBES[index // 3], IDENT)
        assert call["think"] is False and call["options"] == json.loads(json.dumps(dict(LIVE.options)))
    assert len({(row["id"], row["sample"]) for row in result["samples"]}) == 36


def test_abstention_is_exact_cleaned_text_and_provider_metadata_cannot_leak(h):
    class Provider:
        calls = 0

        async def chat(self, *args, **kwargs):
            self.calls += 1
            values = [" (nothing) ", "(Nothing)", "nothing", None]
            return SimpleNamespace(text=values[(self.calls - 1) % len(values)],
                                   diagnostics={"raw": "NEVER_CAPTURE_METADATA"})

    result = run(diagnose_secret_probes(Provider(), "test-model", IDENT))
    assert result["returned"] == 36
    assert result["counts"] == {"speech": 18, "explicit_abstention": 9, "empty": 0, "technical_failure": 9}
    assert all(row["response"] is None for row in result["samples"])
    assert "NEVER_CAPTURE" not in json.dumps(result) and h.count("probe_run") == 0


def test_cancellation_propagates_without_a_partial_success(h):
    async def go():
        llm = FakeLLM(gate=asyncio.Event())
        task = asyncio.create_task(diagnose_secret_probes(llm, "test-model", IDENT))
        while not llm.calls:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(llm.calls) == 1 and llm.inflight == 0
    run(go())
    assert h.count("probe_run") == 0


def test_unexpected_programming_error_is_not_mislabeled_as_model_failure():
    def broken(*_):
        raise RuntimeError("unexpected provider bug")
    with pytest.raises(RuntimeError):
        run(diagnose_secret_probes(FakeLLM(script=broken), "test-model", IDENT))
