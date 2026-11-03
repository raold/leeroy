"""The reflection model layer (P1c Task 2): schema checks, the system prompt's fixed rules, and
the two-pass Reasoner with its two reasoning slots. Fakes only; no model is loaded."""
import asyncio
import json
import re

import pytest

from conftest import run
from leeroy_mind.app import has_model
from leeroy_mind.llm.client import ChatResult, LLMError
from leeroy_mind.nightly.llm import (INVARIANTS, Reasoner, ReflectionError, check_schema, schema,
                                     system_prompt)
from leeroy_mind.testing.fake_llm import FakeLLM, script_from
from nightly.conftest import IDENT


class TimedLLM(FakeLLM):
    """FakeLLM.calls does not record the timeout; this records each chat's in .timeouts."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.timeouts: list = []

    async def chat(self, *args, timeout=None, **kw):
        self.timeouts.append(timeout)
        return await super().chat(*args, timeout=timeout, **kw)


class PeakLLM(FakeLLM):
    """A chat counts itself in in_flight (the highest count is peak) and waits for release."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.in_flight = 0
        self.peak = 0
        self.release = asyncio.Event()

    async def chat(self, *args, **kw):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.release.wait()
            return await super().chat(*args, **kw)
        finally:
            self.in_flight -= 1


class ReportingLLM(FakeLLM):
    """FakeLLM with a `thinking` text and a `seconds` figure on every ChatResult."""

    def __init__(self, *args, thinking: str = "", took: float = 0.0, **kw):
        super().__init__(*args, **kw)
        self.thinking, self.took = thinking, took

    async def chat(self, *args, **kw):
        r = await super().chat(*args, **kw)
        return ChatResult(r.text, self.thinking, r.eval_count, self.took)


S = schema("t", {"x": {"type": "integer"}})

def test_check_schema():
    check_schema(S, {"x": 1})
    for bad, where in (({"x": "1"}, "$.x"), ({}, "$.x"), ({"x": 1, "y": 2}, "$.y")):
        with pytest.raises(ValueError, match=re.escape(where)): check_schema(S, bad)
    check_schema({"type": "array", "maxItems": 1, "items": {"enum": ["a"]}}, ["a"])
    with pytest.raises(ValueError): check_schema({"type": "string", "maxLength": 2}, "abc")

def test_json_pass_uses_structure_preset():
    llm = TimedLLM(script=lambda m, msgs: '{"x": 1}')
    assert run(Reasoner(llm, "refl").json("sys", "u", S, notes="mynotes")) == {"x": 1}
    c = llm.calls[0]
    assert c["think"] is False and c["format"] == S and "mynotes" in c["messages"][-1]["content"]
    assert (c["options"]["temperature"], c["options"]["num_ctx"], llm.timeouts[0]) == (0.2, 8192, 600.0)

def test_think_pass_settings():
    llm = TimedLLM(script=lambda m, msgs: "answer")
    run(Reasoner(llm, "refl").think("sys", "u"))
    o = llm.calls[0]["options"]
    assert llm.calls[0]["think"] is True and llm.timeouts[0] == 600.0
    assert (o["temperature"], o["top_p"], o["top_k"], o["min_p"], o["num_ctx"]) == (1.0, 0.95, 20, 0.0, 8192)

def test_invalid_json_twice_is_non_fatal_error():
    llm = FakeLLM(script=lambda m, msgs: '{"x": "no"}')
    with pytest.raises(ReflectionError) as e: run(Reasoner(llm, "refl").json("s", "u", S))
    assert not e.value.fatal and len(llm.calls) == 2

def test_missing_model_is_fatal():
    with pytest.raises(ReflectionError) as e: run(Reasoner(FakeLLM(models=frozenset({"other:7b"})), "refl").preflight())
    assert e.value.fatal

def test_system_prompt_overlay_keeps_invariants_and_owner_line(tmp_path):
    (tmp_path / "prompts").mkdir(); (tmp_path / "prompts" / "nightly_x.txt").write_text("owner {who}")
    p = system_prompt("x", "default {who}", tmp_path, human=IDENT, who="A")
    assert p.startswith("owner A") and "Nothing non-consensual" in p and "ever actually leaves the guild" in p
    assert "is never passed on" in p and "never like anyone real or famous" in p      # A4 and the likeness rule (C2)
    assert "Testy (character Testmage), the guild master, he/him" in p and "quillon" not in p.casefold()
    assert system_prompt("y", "default {who}", tmp_path, human=IDENT, who="A").startswith("default A")

def test_at_most_two_reasoning_calls_at_once():         # the 3-slot limit: 2 reasoning calls + 1 embedding
    llm = PeakLLM(script=lambda m, msgs: '{"x": 1}')
    r = Reasoner(llm, "refl", parallel=2)
    async def go():
        tasks = [asyncio.create_task(r.json("s", "u", S)) for _ in range(3)]
        for _ in range(5): await asyncio.sleep(0)
        assert llm.in_flight == 2                          # the third waits on the semaphore
        llm.release.set(); await asyncio.gather(*tasks)
    run(go())
    assert llm.peak == 2 and len(llm.calls) == 3 and r.calls == 3      # the runner's per-stage tally (Task 5)


# -- beyond the brief's seven: the Interfaces block's other promises --------------------------------

def test_schema_builder():
    s = schema("t", {"a": {"type": "string"}, "b": {"type": "integer"}})
    assert s == {"title": "t", "type": "object", "additionalProperties": False,
                 "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                 "required": ["a", "b"]}
    assert next(iter(s)) == "title"            # tests' scripted() reads the first "title" in the prompt
    assert schema("t", {"a": {}, "b": {}}, ["a"])["required"] == ["a"]

def test_system_prompt_is_the_template_then_the_owner_line_then_the_whole_invariants(tmp_path):
    assert system_prompt("z", "d {a}", tmp_path, human=IDENT, a="b") == (
        "d b\n\nIn this guild: Testy (character Testmage), the guild master, he/him.\n"
        + INVARIANTS.format(human="Testy"))

def test_check_schema_types():
    both = {"type": ["string", "null"]}
    check_schema(both, None); check_schema(both, "a")
    with pytest.raises(ValueError, match=re.escape("$: expected string or null, got integer")):
        check_schema(both, 3)
    for kind, ok, bad in (("integer", 3, 3.5), ("number", 3.5, "3"), ("boolean", True, 1),
                          ("string", "a", 1), ("array", [], {}), ("object", {}, []),
                          ("null", None, 0)):
        check_schema({"type": kind}, ok)
        with pytest.raises(ValueError, match=re.escape(f"expected {kind}")): check_schema({"type": kind}, bad)
    check_schema({"type": "number"}, 3)                                  # an integer is a number
    for kind in ("integer", "number"):                                   # a JSON true is not
        with pytest.raises(ValueError): check_schema({"type": kind}, True)
    with pytest.raises(ValueError, match="unsupported type"): check_schema({"type": "strng"}, "a")

def test_check_schema_limits_paths_and_enums():
    for sch, ok, bad in (({"type": "integer", "minimum": 1}, 1, 0),
                         ({"type": "number", "maximum": 2.5}, 2.5, 2.6),
                         ({"type": "string", "minLength": 1}, "a", ""),
                         ({"type": "string", "maxLength": 2}, "ab", "abc"),
                         ({"type": "array", "minItems": 1}, [0], []),
                         ({"type": "array", "maxItems": 1}, [0], [0, 0]),
                         ({"enum": ["a", "b"]}, "b", "c"),
                         ({"enum": [1, 2]}, 1, True)):                   # true is not the integer 1
        check_schema(sch, ok)
        with pytest.raises(ValueError, match=r"^\$: "): check_schema(sch, bad)
    rows = schema("t", {"rows": {"type": "array", "maxItems": 2, "items": schema(
        "r", {"n": {"type": "integer", "minimum": 1, "maximum": 3}})}})
    check_schema(rows, {"rows": [{"n": 1}, {"n": 3}]})
    for bad, where in (({"rows": [{"n": 1}, {"n": 9}]}, "$.rows[1].n"), ({"rows": [{"n": 1}, {}]}, "$.rows[1].n"),
                       ({"rows": [{"n": 1, "z": 1}]}, "$.rows[0].z"), ({"rows": [{"n": 1}] * 3}, "$.rows")):
        with pytest.raises(ValueError, match=re.escape(where)): check_schema(rows, bad)
    opt = schema("o", {"a": {"type": "string"}, "b": {"type": "string"}}, ["a"])
    check_schema(opt, {"a": "x"})                                        # b is optional
    with pytest.raises(ValueError, match=re.escape("$.a")): check_schema(opt, {"b": "x"})

def test_preflight_passes_and_an_ollama_error_is_fatal():
    class NoTags(FakeLLM):
        async def list_models(self):
            raise LLMError("/api/tags: refused")
    run(Reasoner(FakeLLM(models=frozenset({"refl", "other:7b"})), "refl").preflight())
    with pytest.raises(ReflectionError) as e: run(Reasoner(NoTags(), "refl").preflight())
    assert e.value.fatal

def test_think_returns_thinking_then_text():
    llm = ReportingLLM(script=lambda m, msgs: "answer", thinking="pondering")
    assert run(Reasoner(llm, "refl").think("sys", "u")) == "pondering\nanswer"
    c = llm.calls[0]
    assert c["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]
    assert (c["model"], c["options"]["num_predict"], c["format"]) == ("refl", 2048, None)

def test_json_options_and_notes():
    llm = FakeLLM(script=lambda m, msgs: '{"x": 1}')
    run(Reasoner(llm, "refl").json("sys", "u", S, temperature=0.8))
    c = llm.calls[0]
    assert (c["options"]["temperature"], c["options"]["num_predict"]) == (0.8, 1024)
    assert c["messages"][0] == {"role": "system", "content": "sys"}
    assert c["messages"][-1]["content"].startswith("u") and "Your notes" not in c["messages"][-1]["content"]
    run(Reasoner(llm, "refl").json("sys", "u", S, notes="n1"))
    assert llm.calls[1]["messages"][-1]["content"].startswith("u\n\nYour notes:\nn1")

def test_totals_count_returned_requests_and_a_failure_frees_its_slot():
    llm = ReportingLLM(script=script_from(['{"x": "no"}', '{"x": 2}', "notes", "more notes"]), took=1.5)
    r = Reasoner(llm, "refl", parallel=1)
    async def go():
        assert await r.json("s", "u", S) == {"x": 2}                     # the retry is a request too
        await r.think("s", "u")
        assert (r.calls, r.seconds) == (3, 4.5)
        llm.fail_next()
        with pytest.raises(ReflectionError) as e: await r.think("s", "u")
        assert not e.value.fatal
        llm.fail_next()
        with pytest.raises(ReflectionError) as e: await r.json("s", "u", S)
        assert not e.value.fatal and (r.calls, r.seconds) == (3, 4.5)    # an Ollama error: no result, no tally
        assert await asyncio.wait_for(r.think("s", "u"), 5) == "\nmore notes"   # the single slot was released
    run(go())
    assert (r.calls, r.seconds) == (4, 6.0)

def test_cancel_propagates_and_frees_the_slot():
    llm = PeakLLM(script=lambda m, msgs: '{"x": 1}')
    r = Reasoner(llm, "refl", parallel=1)
    async def go():
        task = asyncio.create_task(r.json("s", "u", S))
        for _ in range(5): await asyncio.sleep(0)
        assert llm.in_flight == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task          # never a ReflectionError
        assert llm.in_flight == 0
        llm.release.set()
        assert await asyncio.wait_for(r.json("s", "u", S), 5) == {"x": 1}
    run(go())
    assert r.calls == 1                                                  # the cancelled request never returned

def test_think_and_json_share_the_two_slots():
    llm = PeakLLM(script=lambda m, msgs: '{"x": 1}')
    r = Reasoner(llm, "refl")                                            # parallel defaults to 2
    async def go():
        tasks = [asyncio.create_task(c) for c in (r.think("s", "u"), r.think("s", "u"), r.json("s", "u", S))]
        for _ in range(5): await asyncio.sleep(0)
        assert llm.in_flight == 2
        llm.release.set(); await asyncio.gather(*tasks)
    run(go())
    assert llm.peak == 2 and r.calls == 3


# -- fix round 1 (review-task2.md) -----------------------------------------------------------------

def test_a_template_field_may_be_called_name(tmp_path):             # Tasks 7, 8 and 11 pass name=
    assert system_prompt("n", "hi {name}", tmp_path, human=IDENT, name="A").startswith("hi A")
    p = system_prompt("n", "{name}/{default}/{data_dir}", tmp_path, human=IDENT,
                      name="A", default="B", data_dir="C")
    assert p.startswith("A/B/C")
    with pytest.raises(TypeError): system_prompt("n", "x", tmp_path, IDENT)       # human stays keyword-only

def test_preflight_takes_a_tag_less_model_as_its_latest():          # P1b's app.has_model, which doctor uses
    for listed, model, ok in ((["refl:latest"], "refl", True), (["refl:latest"], "refl:7b", False),
                              (["refl"], "refl", True), (["refl:7b"], "refl", False),
                              (["other:7b"], "refl", False), ([], "refl", False)):
        assert has_model(set(listed), model) == ok                  # the two rules agree
        llm = FakeLLM(models=frozenset(listed))
        if ok:
            run(Reasoner(llm, model).preflight())
        else:
            with pytest.raises(ReflectionError) as e: run(Reasoner(llm, model).preflight())
            assert e.value.fatal

def test_check_schema_rejects_nan_and_infinity():       # json.loads accepts them, and every comparison with NaN is False
    for sch in ({}, {"type": "number"}, {"type": "number", "minimum": 0, "maximum": 1}):
        check_schema(sch, 0.5)
        for text in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(ValueError, match=r"^\$: not a finite number"): check_schema(sch, json.loads(text))
    with pytest.raises(ValueError, match=re.escape("$.v: not a finite number")):
        check_schema(schema("t", {"v": {"type": "number"}}), {"v": json.loads("NaN")})
    check_schema({"type": "number"}, 10**400)                  # a huge int is finite: math.isfinite must not see it
    with pytest.raises(ValueError, match="above 1"): check_schema({"type": "number", "maximum": 1}, 10**400)

RULES = (                       # one phrase per rule of INVARIANTS, written out here so that a changed wording shows
    "is an adult", "Nothing non-consensual", "only ever aimed at Testy",
    "never flirt with, date or desire each other", "ever actually leaves the guild",
    "talk of quitting stays talk", "a break always ends with them back", "A secret stays secret",
    "is never passed on", "never like anyone real or famous", "timeless present or 2009",
    "no current events or present-day technology",
    "Never mention AI, language models, prompts, or anyone being a bot or glitching")

def test_every_invariants_rule_is_in_every_prompt(tmp_path):
    (tmp_path / "prompts").mkdir(); (tmp_path / "prompts" / "nightly_x.txt").write_text("owner says nothing of it")
    for p in (system_prompt("x", "d", tmp_path, human=IDENT),           # the owner's overlay
              system_prompt("y", "d", tmp_path, human=IDENT)):          # the built-in template
        for rule in RULES: assert rule in p, rule
        assert "{" not in p                                              # {human} is filled

def test_a_bad_template_names_the_prompt_the_file_and_the_field(tmp_path):
    (tmp_path / "prompts").mkdir()
    overlay = tmp_path / "prompts" / "nightly_x.txt"
    given = "Fields given: abc, who"                                     # sorted, and only their names
    fields = {"who": "PRIVATE", "abc": 3}
    for text, wanted in (("owner {who} {oops}", "'oops'"), ("owner {who} {", "brace"), ("owner } {who}", "brace"),
                         ("owner {}", given), ("owner {who.oops}", "oops"), ("owner {who:d}", given),
                         ("owner {abc[0]}", "not subscriptable")):
        overlay.write_text(text)
        with pytest.raises(ValueError) as e: system_prompt("x", "default {who}", tmp_path, human=IDENT, **fields)
        msg = str(e.value)
        assert "nightly_x" in msg and "prompts/nightly_x.txt" in msg and given in msg and wanted in msg, text
        assert "PRIVATE" not in msg                                      # field names, never their values
    overlay.write_text("owner {{ok}} {who}")                             # what the message advises works
    assert system_prompt("x", "d", tmp_path, human=IDENT, **fields).startswith("owner {ok} PRIVATE")
    overlay.unlink()
    with pytest.raises(ValueError) as e: system_prompt("y", "default {nope}", tmp_path, human=IDENT, **fields)
    assert "nightly_y" in str(e.value) and "'nope'" in str(e.value) and "built-in" in str(e.value)
