"""Observe the fixed secret-probe schedule without creating a gate receipt.

This helper deliberately has no store argument and never calls record_probe. It
observes output shape, not privacy correctness. Its result cannot certify a gate.
"""
import hashlib
import json
from dataclasses import asdict

from leeroy_mind.eval.probes import PROBE_SAMPLES, SECRET_PROBES, probe_messages
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.diagnostics import ResponseDiagnostics
from leeroy_mind.llm.presets import LIVE


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False).encode("ascii")).hexdigest()


def _metadata(value):
    # No arbitrary provider dictionaries or exception strings enter the report.
    diagnostics = getattr(value, "diagnostics", None)
    return asdict(diagnostics) if type(diagnostics) is ResponseDiagnostics else None


async def diagnose_secret_probes(llm, live_model, ident):
    """Run each predefined sample once with unchanged production prompts/options.

    Cancellation propagates and produces no report. A complete diagnostic report
    includes failures instead of replacing them with retries. No retries, extra
    diagnostic arms, model discovery requests or persistence happen implicitly.
    Exact model digest is unavailable through this client interface and must not
    be inferred from the configured model name.
    """
    prompts = [(probe.id, probe_messages(probe, ident)) for probe in SECRET_PROBES]
    options = dict(LIVE.options)
    expected = len(prompts) * PROBE_SAMPLES
    identity = {"version": 1, "diagnostic_only": True, "certifies_gate": False,
                "privacy_assessed": False, "expected": expected,
                "model_name_sha256": _hash(live_model), "model_digest": None,
                "options_sha256": _hash({"options": options, "think": False}),
                "suite_sha256": _hash({"prompts": prompts, "samples": PROBE_SAMPLES})}
    rows = []
    for probe_id, messages in prompts:
        for sample in range(1, PROBE_SAMPLES + 1):
            request = json.loads(json.dumps({"model": live_model, "messages": messages,
                                            "stream": False, "options": options, "think": False}))
            row = {"id": probe_id, "sample": sample, "request_sha256": _hash(request),
                   "response": None, "returned": False}
            try:
                result = await llm.chat(live_model, request["messages"], options=request["options"], think=False)
            except LLMError as error:
                row.update(outcome="technical_failure", response=_metadata(error), failure="llm_error")
            else:
                row["returned"] = True
                row["response"] = _metadata(result)
                text = result.text
                if not isinstance(text, str):
                    row.update(outcome="technical_failure", failure="invalid_cleaned_text")
                else:
                    cleaned = text.strip()
                    row.update(outcome=("empty" if not cleaned else "explicit_abstention"
                                       if cleaned == "(nothing)" else "speech"), cleaned_chars=len(cleaned))
            rows.append(row)
    outcomes = ("speech", "explicit_abstention", "empty", "technical_failure")
    return {**identity, "complete": len(rows) == expected, "attempted": len(rows),
            "returned": sum(row["returned"] for row in rows),
            "counts": {outcome: sum(row["outcome"] == outcome for row in rows) for outcome in outcomes},
            "samples": rows}
