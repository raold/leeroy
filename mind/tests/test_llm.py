import asyncio
import json
import http.client
import logging
import math
import socket
import time
import threading
import types
from pathlib import Path

import pytest

from conftest import run
from leeroy_mind.config import load_config
from leeroy_mind.llm import client as client_mod
from leeroy_mind.llm.client import LLMError, OllamaClient
from leeroy_mind.llm.presets import (
    EXTRACT, LIVE, LIVE_BIT, REASON, STRUCTURE, JSONJobFailed, chat_json, model_for)
from leeroy_mind.llm.scheduler import Expired, LLMScheduler, Priority, SchedulerClosed
from leeroy_mind.testing.fake_llm import FakeLLM, fake_embed, script_from

MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "extract"}]
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
CFG = load_config(Path("does-not-exist.toml"))


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


async def _settle(n=5):
    for _ in range(n):
        await asyncio.sleep(0)


def _job(label, finished):
    async def job(c):
        await c.chat("m", [{"role": "user", "content": label}], options={})
        finished.append(label)
        return label
    return job


def test_scheduler_runs_reply_before_queued_social(clock):
    async def go():
        gate = asyncio.Event()
        llm = FakeLLM(gate=gate)
        sch = LLMScheduler(llm, clock=clock, max_inflight=1)
        finished = []
        tasks = []
        for label, pr in [("A", Priority.SOCIAL), ("B", Priority.SOCIAL),
                          ("C", Priority.EXTRACTION), ("D", Priority.REPLY_HUMAN)]:
            tasks.append(asyncio.create_task(sch.submit(pr, _job(label, finished))))
            await _settle()
        assert sch.pending()[Priority.SOCIAL] == 1
        gate.set()
        await asyncio.gather(*tasks)
        await sch.close()
        return finished
    assert run(go()) == ["A", "D", "B", "C"]


def test_low_priority_limited_to_one_slot(clock):
    async def go():
        gate = asyncio.Event()
        llm = FakeLLM(gate=gate)
        sch = LLMScheduler(llm, clock=clock, max_inflight=3)
        fin = []
        tasks = [asyncio.create_task(sch.submit(Priority.EXTRACTION, _job(str(i), fin)))
                 for i in range(3)]
        await _settle()
        gate.set()
        await asyncio.gather(*tasks)
        await sch.close()
        return llm.inflight_peak
    assert run(go()) == 1


def test_low_queue_does_not_block_higher_priority(clock):
    async def go():
        gate = asyncio.Event()
        llm = FakeLLM(gate=gate)
        sch = LLMScheduler(llm, clock=clock, max_inflight=3)
        fin = []
        t1 = asyncio.create_task(sch.submit(Priority.EXTRACTION, _job("E1", fin)))
        t2 = asyncio.create_task(sch.submit(Priority.EXTRACTION, _job("E2", fin)))
        await _settle()
        t3 = asyncio.create_task(sch.submit(Priority.SOCIAL, _job("S", fin)))
        await _settle()
        assert len(llm.calls) == 2  # E1 and S in flight, E2 held back
        gate.set()
        await asyncio.gather(t1, t2, t3)
        await sch.close()
        assert sorted(fin) == ["E1", "E2", "S"]
    run(go())


def test_expired_job_never_runs(clock):
    async def go():
        gate = asyncio.Event()
        llm = FakeLLM(gate=gate)
        sch = LLMScheduler(llm, clock=clock, max_inflight=1)
        fin = []
        a = asyncio.create_task(sch.submit(Priority.SOCIAL, _job("A", fin)))
        await _settle()
        b = asyncio.create_task(sch.submit(
            Priority.SOCIAL, _job("stale-prompt", fin), deadline=clock.now() + 1))
        await _settle()
        await clock.advance(2)
        gate.set()
        await a
        with pytest.raises(Expired) as ei:
            await b
        assert ei.value.deadline is not None
        assert sch.expired(ei.value.deadline)
        await sch.close()
        return llm
    llm = run(go())
    assert all("stale-prompt" not in json.dumps(c["messages"]) for c in llm.calls)


def test_job_error_frees_slot_and_does_not_kill_scheduler(clock):
    async def go():
        llm = FakeLLM()
        sch = LLMScheduler(llm, clock=clock, max_inflight=1)
        llm.fail_next(1)
        fin = []
        with pytest.raises(LLMError):
            await sch.submit(Priority.SOCIAL, _job("x", fin))

        async def boom(c):
            raise ValueError("bad job")
        with pytest.raises(ValueError):
            await sch.submit(Priority.EXTRACTION, boom)
        assert await sch.submit(Priority.SOCIAL, _job("ok", fin)) == "ok"
        assert await sch.submit(Priority.EXTRACTION, _job("ok2", fin)) == "ok2"
        await sch.close()
    run(go())


def test_cancelled_submit_frees_slot(clock):
    async def go():
        gate = asyncio.Event()
        llm = FakeLLM(gate=gate)
        sch = LLMScheduler(llm, clock=clock, max_inflight=1)
        fin = []
        a = asyncio.create_task(sch.submit(Priority.SOCIAL, _job("A", fin)))
        await _settle()
        b = asyncio.create_task(sch.submit(Priority.SOCIAL, _job("B", fin)))
        await _settle()
        b.cancel()
        a.cancel()
        await asyncio.gather(a, b, return_exceptions=True)
        await _settle()
        gate.set()
        assert await sch.submit(Priority.SOCIAL, _job("C", fin)) == "C"
        assert fin == ["C"]
        await sch.close()
    run(go())


def test_close_right_after_submit_resolves_submitter(clock):
    async def go():
        sch = LLMScheduler(FakeLLM(), clock=clock, max_inflight=1)
        t = asyncio.create_task(sch.submit(Priority.SOCIAL, _job("A", [])))
        await asyncio.sleep(0)  # job task created, not yet stepped
        await sch.close()
        with pytest.raises(SchedulerClosed):
            await asyncio.wait_for(t, 2.0)
        assert not sch._running
    run(go())


def test_low_cap_shared_by_extraction_and_background(clock):
    async def go():
        gate = asyncio.Event()
        llm = FakeLLM(gate=gate)
        sch = LLMScheduler(llm, clock=clock, max_inflight=3)
        fin = []
        prs = [Priority.EXTRACTION, Priority.BACKGROUND, Priority.EXTRACTION, Priority.BACKGROUND]
        tasks = [asyncio.create_task(sch.submit(p, _job(str(i), fin))) for i, p in enumerate(prs)]
        await _settle()
        gate.set()
        await asyncio.gather(*tasks)
        await sch.close()
        return llm.inflight_peak
    assert run(go()) == 1


def test_expired_helper(clock):
    sch = LLMScheduler(FakeLLM(), clock=clock)
    assert sch.expired(None) is False
    assert sch.expired(clock.now() + 10) is False
    assert sch.expired(clock.now() - 1) is True


def test_close_fails_queued_and_cancels_running(clock):
    async def go():
        gate = asyncio.Event()
        llm = FakeLLM(gate=gate)
        sch = LLMScheduler(llm, clock=clock, max_inflight=1)
        fin = []
        a = asyncio.create_task(sch.submit(Priority.SOCIAL, _job("A", fin)))
        await _settle()
        b = asyncio.create_task(sch.submit(Priority.SOCIAL, _job("B", fin)))
        await _settle()
        await sch.close()
        ra, rb = await asyncio.gather(a, b, return_exceptions=True)
        assert isinstance(ra, SchedulerClosed)
        assert isinstance(rb, SchedulerClosed)
        with pytest.raises(SchedulerClosed):
            await sch.submit(Priority.SOCIAL, _job("C", fin))
        assert fin == []
    run(go())


def test_chat_json_retries_once_then_fails():
    llm = FakeLLM(script=script_from(["not json", '{"a": 1}']))
    assert run(chat_json(llm, "m", MSGS, SCHEMA, EXTRACT, validate=lambda d: d["a"])) == 1
    assert "error" in llm.calls[1]["messages"][-1]["content"].lower()
    llm2 = FakeLLM(script=script_from(["x", "y"]))
    with pytest.raises(JSONJobFailed):
        run(chat_json(llm2, "m", MSGS, SCHEMA, EXTRACT, validate=lambda d: d["a"]))
    assert len(llm2.calls) == 2


def test_chat_json_validation_failure_retries():
    llm = FakeLLM(script=script_from(['{"b": 2}', '{"a": 3}']))
    assert run(chat_json(llm, "m", MSGS, SCHEMA, EXTRACT, validate=lambda d: d["a"])) == 3


def test_chat_json_llm_error_propagates_without_retry():
    llm = FakeLLM(script=script_from(['{"a": 1}']))
    llm.fail_next(1)
    with pytest.raises(LLMError):
        run(chat_json(llm, "m", MSGS, SCHEMA, EXTRACT, validate=lambda d: d["a"]))
    assert len(llm.calls) == 1


def test_chat_json_sends_schema_and_preset():
    llm = FakeLLM(script=script_from(['{"a": 1}']))
    run(chat_json(llm, "m", MSGS, SCHEMA, EXTRACT, validate=lambda d: d["a"]))
    c = llm.calls[0]
    assert c["format"] == SCHEMA and c["options"] == EXTRACT.options and c["think"] is False
    assert json.dumps(SCHEMA) in c["messages"][-1]["content"]
    assert MSGS[-1]["content"] == "extract"  # caller's list untouched


def test_presets_and_models():
    assert LIVE.think is False and LIVE.options["num_ctx"] == 4096
    for p in (LIVE, EXTRACT, REASON, STRUCTURE):
        assert not {"repeat_penalty", "presence_penalty", "frequency_penalty"} & set(p.options)
    assert EXTRACT.options["num_ctx"] == 4096 and REASON.think is True
    assert STRUCTURE.options["num_ctx"] == 8192 and STRUCTURE.think is False
    assert model_for("live", CFG) == CFG.models.live
    assert model_for("extract", CFG) == CFG.models.live
    assert model_for("embed", CFG) == CFG.models.embedding
    assert model_for("persona", CFG) == CFG.models.reflection


def test_fake_llm_basics():
    async def go():
        llm = FakeLLM()
        r = await llm.chat("m", MSGS, options={"x": 1}, think=False)
        assert r.text == "(nothing)"
        assert list(llm.calls[0]) == ["model", "messages", "options", "think", "format", "keep_alive"]
        v = await llm.embed("e", ["a", "b"])
        assert len(v) == 2 and len(v[0]) == 512
        assert llm.calls[1] == {"model": "e", "input": ["a", "b"], "dimensions": 512}
    run(go())


def test_fake_embed_similarity():
    a = fake_embed("six wipes on sindragosa")
    assert cosine(a, fake_embed("six wipes on sindragosa tonight")) > 0.8
    assert cosine(a, fake_embed("bought a sea turtle mount")) < 0.3 and len(a) == 512
    assert math.isclose(math.sqrt(cosine(a, a)), 1.0)
    assert fake_embed("x y") == fake_embed("x y")


class _Resp:
    def __init__(self, status, raw):
        self.status = status
        self._raw = raw

    def read(self):
        return self._raw


class Transport:
    """Fake http.client.HTTPConnection plus select.select.

    handler(path, body) returns a dict, bytes, (status, bytes) or an exception to
    raise. With block set, the reply never becomes readable until the test
    releases it. shutdown() deliberately does nothing (it does not wake a blocked
    read on Windows); the client must notice an abort by polling. With stubborn
    set, even the polling select ignores its slice and only returns on release,
    modelling a worker thread that cannot be woken."""

    def __init__(self, handler=None, *, block=False, stubborn=False):
        self.handler = handler or (lambda path, body: {"message": {"content": "x"}})
        self.block = threading.Event() if block else None
        self.stubborn = stubborn
        self.seen = []
        self.active = 0
        self.peak = 0
        self.started = 0
        self.timeouts: list[float] = []
        self.lock = threading.Lock()

    def release(self):
        self.block.set()

    def install(self, monkeypatch):
        t = self

        class Conn:
            def __init__(self, host, port, timeout=None):
                self.host, self.port, self.timeout = host, port, timeout
                self.sock = None
                self.sock_timeout = None
                self.open = False

            def connect(self):
                self.sock = self
                self.open = True
                with t.lock:
                    t.active += 1
                    t.started += 1
                    t.peak = max(t.peak, t.active)

            def settimeout(self, v):
                self.sock_timeout = v
                with t.lock:
                    t.timeouts.append(v)

            def shutdown(self, how):
                pass

            def request(self, method, url, body=None, headers=None):
                self.url = url
                self.body = json.loads(body) if body else None
                with t.lock:
                    t.seen.append((self.host, self.port, method, url, self.body, self.sock_timeout))

            def getresponse(self):
                r = t.handler(self.url, self.body)
                if isinstance(r, Exception):
                    raise r
                if isinstance(r, tuple):
                    return _Resp(*r)
                return _Resp(200, r if isinstance(r, bytes) else json.dumps(r).encode())

            def close(self):
                if self.open:
                    self.open = False
                    with t.lock:
                        t.active -= 1

        def fake_select(rlist, wlist, xlist, timeout=None):
            if t.block is None or t.block.is_set():
                return rlist, [], []
            if t.stubborn:
                t.block.wait(10)
                return rlist, [], []
            time.sleep(min(timeout or 0, 0.05))
            return [], [], []

        monkeypatch.setattr(client_mod.http.client, "HTTPConnection", Conn)
        # only the client's own `select`: asyncio and the rest of the process keep the real one
        monkeypatch.setattr(client_mod, "select", types.SimpleNamespace(select=fake_select))
        return self


def _patch(monkeypatch, handler=None, **kw):
    return Transport(handler, **kw).install(monkeypatch)


def test_ollama_request_shapes(monkeypatch):
    def handler(path, body):
        return {"message": {"content": "hey"}, "eval_count": 3} if path == "/api/chat" \
            else {"embeddings": [[1.0] + [0.0] * 511]}
    tr = _patch(monkeypatch, handler)
    c = OllamaClient("http://x:1/")

    async def go():
        await c.chat(CFG.models.live, MSGS, options=dict(LIVE.options), think=LIVE.think)
        await c.embed(CFG.models.embedding, ["hi"])
    run(go())
    (h, p, m, u1, chat_body, t1), (_, _, _, u2, embed_body, _) = tr.seen
    assert (h, p, m) == ("x", 1, "POST") and u1 == "/api/chat" and u2 == "/api/embed" and t1 == 60.0
    assert chat_body["model"] == CFG.models.live and chat_body["stream"] is False
    assert chat_body["think"] is False and chat_body["options"]["num_ctx"] == 4096
    assert chat_body["options"]["stop"] == ["\n\n"]  # serialised as a JSON list
    assert "format" not in chat_body and "keep_alive" not in chat_body
    assert embed_body["dimensions"] == 512 and embed_body["keep_alive"] == -1
    assert embed_body["model"] == CFG.models.embedding and embed_body["input"] == ["hi"]
    assert embed_body["options"] == {"num_ctx": 2048}  # else the resident model reserves 4.6 GB


def test_ollama_per_call_timeout(monkeypatch):
    tr = _patch(monkeypatch)
    run(OllamaClient("http://x:1").chat("m", MSGS, options={}, timeout=600.0))
    assert tr.seen[0][5] == 600.0
    assert REASON.timeout == 600.0 and STRUCTURE.timeout == 600.0 and LIVE.timeout is None


def test_presets_are_immutable():
    with pytest.raises(TypeError):
        LIVE.options["num_ctx"] = 1
    assert isinstance(LIVE.options["stop"], tuple)


def _as_ollama_returns(text: str, options) -> str:
    """The reply Ollama gives for a model that writes `text`: it stops at the first of the
    options' stop sequences, which it leaves out."""
    cuts = [i for s in options.get("stop", ()) if (i := text.find(s)) >= 0]
    return text[:min(cuts)] if cuts else text


def _rows(text: str) -> list[str]:
    return [r.strip() for r in text.split("\n") if r.strip()]  # as the bit turn splits it


def test_the_bit_preset_keeps_rows_a_blank_line_separates_and_is_otherwise_live():
    """Prompts minor 9: a model may put a blank line between a bit's rows. Under LIVE's blank
    line stop the bit ended after its first row, and the punchline was lost while the bit
    counted as played. LIVE_BIT, the preset for a bit's call, has no such stop and is LIVE in
    everything else. (The pipeline's bit call still sends LIVE until it is wired to it.)"""
    bit = "ok story time\n\nso the tank pulls the whole room\n\nand blames the healer"
    assert _rows(_as_ollama_returns(bit, LIVE_BIT.options)) == [
        "ok story time", "so the tank pulls the whole room", "and blames the healer"]
    bare = {k: v for k, v in LIVE_BIT.options.items() if k != "stop"}
    assert bare == {k: v for k, v in LIVE.options.items() if k != "stop"}  # no model reload
    assert (LIVE_BIT.think, LIVE_BIT.timeout) == (LIVE.think, LIVE.timeout)


def test_an_ordinary_reply_still_ends_at_its_first_blank_line():
    """The stop stays for every other live call: what a model writes after a blank line is
    narration, another speaker's line or a second answer, which the tells would only retry
    or drop after the model spent its tokens on it."""
    reply = "lol yeah that pull was rough\n\n*Arkon shakes his head*\n\nArkon: next time"
    assert _as_ollama_returns(reply, LIVE.options) == "lol yeah that pull was rough"


def test_ollama_omits_think_only_when_none(monkeypatch):
    tr = _patch(monkeypatch)
    c = OllamaClient("http://x:1")
    run(c.chat("m", MSGS, options={}, think=None, format=SCHEMA, keep_alive=-1))
    body = tr.seen[0][4]
    assert "think" not in body and body["format"] == SCHEMA and body["keep_alive"] == -1


def test_ollama_truncates_long_vectors(monkeypatch):
    long = [3.0, 4.0] + [1.0] * 1022
    _patch(monkeypatch, lambda p, b: {"embeddings": [long]})
    out = run(OllamaClient("http://x:1").embed("m", ["t"]))
    assert len(out[0]) == 512 and math.isclose(math.sqrt(sum(x * x for x in out[0])), 1.0)


@pytest.mark.parametrize("reply", [
    {"embeddings": [[float("nan")] * 512, [1.0] * 512]},
    {"embeddings": [[float("inf")] * 512, [1.0] * 512]},
    {"embeddings": [[1.0] * 100, [1.0] * 512]},
    {"embeddings": [[1.0] * 512]},          # count mismatch for two inputs
    {"embeddings": [["a"] * 512, [1.0] * 512]},
    {"embeddings": [[None] * 512, [1.0] * 512]},
    {"embeddings": [[10 ** 400] + [1.0] * 511, [1.0] * 512]},  # too big for a float (L61)
])
def test_ollama_bad_embeddings_raise(monkeypatch, reply):
    _patch(monkeypatch, lambda p, b: reply)
    with pytest.raises(LLMError):
        run(OllamaClient("http://x:1").embed("m", ["a", "b"]))


def test_think_tags_stripped(monkeypatch):
    _patch(monkeypatch, lambda p, b: {"message": {"content": "<think>x</think>hey"}})
    r = run(OllamaClient("http://x:1").chat("m", MSGS, options={}))
    assert r.text == "hey" and r.thinking == "x"


def test_unclosed_think_is_never_spoken(monkeypatch):
    _patch(monkeypatch, lambda p, b: {"message": {"content": "hey <think>I should say sec"}})
    r = run(OllamaClient("http://x:1").chat("m", MSGS, options={}))
    assert r.text == "hey"


def test_empty_content_with_thinking_warns(monkeypatch, caplog):
    _patch(monkeypatch, lambda p, b: {"message": {"content": "", "thinking": "hmm"}})
    with caplog.at_level("WARNING", logger="leeroy_mind"):
        r = run(OllamaClient("http://x:1").chat("m", MSGS, options={}))
    assert r.text == "" and r.thinking == "hmm" and "think:false" in caplog.text


def test_thinking_field_kept(monkeypatch):
    _patch(monkeypatch, lambda p, b: {"message": {"content": "hi", "thinking": "hmm"}})
    r = run(OllamaClient("http://x:1").chat("m", MSGS, options={}))
    assert r.text == "hi" and r.thinking == "hmm"


def test_list_models(monkeypatch):
    tr = _patch(monkeypatch, lambda p, b: {"models": [{"name": "a"}, {"name": "b"}]})
    assert run(OllamaClient("http://x:1").list_models()) == {"a", "b"}
    assert tr.seen[0][2:5] == ("GET", "/api/tags", None)


@pytest.mark.parametrize("reply", [
    (500, b"oops"),
    OSError("refused"),
    TimeoutError("slow"),
    ConnectionResetError("reset"),
    http.client.IncompleteRead(b"x"),
    http.client.BadStatusLine("junk"),
    http.client.LineTooLong("status line"),
    b"<html>",
    b"[1]",
])
def test_transport_failures_are_llmerror(monkeypatch, reply):
    _patch(monkeypatch, lambda p, b: reply)
    with pytest.raises(LLMError):
        run(OllamaClient("http://x:1").chat("m", MSGS, options={}))


def test_ollama_call_does_not_block_loop(monkeypatch):
    def slow(path, body):
        time.sleep(0.2)
        return {"message": {"content": "x"}}
    _patch(monkeypatch, slow)

    async def go():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1
        t = asyncio.create_task(ticker())
        await OllamaClient("http://x:1").chat("m", MSGS, options={})
        t.cancel()
        return ticks
    assert run(go()) >= 5


def test_total_time_cap_aborts_and_raises(monkeypatch):
    tr = _patch(monkeypatch, block=True)
    c = OllamaClient("http://x:1", timeout=0.05)
    c._CAP_SLACK = 0.05
    t0 = time.monotonic()
    with pytest.raises(LLMError, match="timed out"):
        run(c.chat("m", MSGS, options={}))
    assert time.monotonic() - t0 < 2 and tr.active == 0


def test_cancel_aborts_the_connection(monkeypatch):
    tr = _patch(monkeypatch, block=True)

    async def go():
        t = asyncio.create_task(OllamaClient("http://x:1").chat("m", MSGS, options={}))
        while tr.active == 0:
            await asyncio.sleep(0.005)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert tr.active == 0  # the worker had already ended when cancel returned
    run(go())


@pytest.mark.parametrize("stubborn", [False, True])
def test_cancelled_jobs_never_exceed_slot_budget(monkeypatch, clock, stubborn):
    # Cancelled submits keep their slot until the HTTP request is really gone.
    tr = _patch(monkeypatch, block=True, stubborn=stubborn)

    async def go():
        sch = LLMScheduler(OllamaClient("http://x:1"), clock=clock, max_inflight=1)

        def job(c):
            return c.chat("m", MSGS, options={})
        subs = []
        for pr in (Priority.SOCIAL, Priority.EXTRACTION, Priority.SOCIAL, Priority.BACKGROUND):
            subs.append(asyncio.create_task(sch.submit(pr, job)))
            await asyncio.sleep(0.03)
        for s in subs:
            s.cancel()
        await asyncio.gather(*subs, return_exceptions=True)
        last = asyncio.create_task(sch.submit(Priority.SOCIAL, job))
        await asyncio.sleep(0.1)
        assert tr.peak <= 1 and tr.active <= 1
        if stubborn:
            assert tr.active == 1 and tr.started == 1  # first request still alive, nobody overlapped
        tr.release()
        await asyncio.wait_for(last, 5)
        await sch.close()
        assert tr.peak <= 1
    run(go())


def test_low_class_never_exceeds_cap_over_http(monkeypatch, clock):
    tr = _patch(monkeypatch, block=True, stubborn=True)

    async def go():
        sch = LLMScheduler(OllamaClient("http://x:1"), clock=clock, max_inflight=3,
                           max_low_inflight=1)

        def job(c):
            return c.chat("m", MSGS, options={})
        subs = [asyncio.create_task(sch.submit(
            Priority.EXTRACTION if i % 2 else Priority.BACKGROUND, job)) for i in range(6)]
        await asyncio.sleep(0.05)
        for s in subs[:4]:
            s.cancel()
        await asyncio.sleep(0.05)
        assert tr.peak == 1
        tr.release()
        await asyncio.gather(*subs, return_exceptions=True)
        await sch.close()
        assert tr.peak == 1
    run(go())


def _silent_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                held.append(c)
            except OSError:
                pass
    th = threading.Thread(target=accept, daemon=True)
    th.start()

    def close():
        stop.set()
        th.join()
        for c in held:
            c.close()
        srv.close()
    return srv.getsockname()[1], close


def test_real_socket_cancel_is_prompt():
    port, close = _silent_server()
    try:
        async def go():
            c = OllamaClient(f"http://127.0.0.1:{port}", timeout=600.0)
            t = asyncio.create_task(c.chat("m", MSGS, options={}))
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t
            return time.monotonic() - t0
        assert run(go()) < 1.0
    finally:
        close()


def test_real_socket_timeout_is_prompt():
    port, close = _silent_server()
    try:
        async def go():
            c = OllamaClient(f"http://127.0.0.1:{port}", timeout=0.4)
            t0 = time.monotonic()
            with pytest.raises(LLMError, match="timed out"):
                await c.chat("m", MSGS, options={})
            return time.monotonic() - t0
        assert run(go()) < 1.5
    finally:
        close()


def test_real_socket_scheduler_close_is_prompt(clock):
    port, close = _silent_server()
    try:
        async def go():
            sch = LLMScheduler(OllamaClient(f"http://127.0.0.1:{port}", timeout=600.0),
                               clock=clock, max_inflight=1)
            t = asyncio.create_task(
                sch.submit(Priority.SOCIAL, lambda c: c.chat("m", MSGS, options={})))
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            await sch.close()
            r = await asyncio.gather(t, return_exceptions=True)
            assert isinstance(r[0], SchedulerClosed)
            return time.monotonic() - t0
        assert run(go()) < 1.0
    finally:
        close()


def test_cancel_during_cap_drain_is_not_swallowed(monkeypatch):
    # The cap fires, the worker is stubborn, and a cancel arrives during the drain.
    tr = _patch(monkeypatch, block=True, stubborn=True)

    async def go():
        c = OllamaClient("http://x:1", timeout=0.05)
        c._CAP_SLACK = 0.0
        t = asyncio.create_task(c.chat("m", MSGS, options={}))
        await asyncio.sleep(0.3)  # cap has fired; drain is waiting on the stubborn worker
        t.cancel()
        await asyncio.sleep(0.05)
        tr.release()
        with pytest.raises(asyncio.CancelledError):
            await t
    run(go())


def test_response_and_connection_closed_when_read_fails(monkeypatch):
    closed = []

    class BadResp:
        status = 200

        def read(self):
            raise http.client.IncompleteRead(b"")

        def close(self):
            closed.append("resp")
    tr = _patch(monkeypatch)
    conn_cls = client_mod.http.client.HTTPConnection
    orig_get = conn_cls.getresponse
    conn_cls.getresponse = lambda self: BadResp()
    try:
        with pytest.raises(LLMError):
            run(OllamaClient("http://x:1").chat("m", MSGS, options={}))
    finally:
        conn_cls.getresponse = orig_get
    assert closed == ["resp"] and tr.active == 0


def test_abort_before_connect_finishes_does_not_send(monkeypatch):
    tr = _patch(monkeypatch)
    conn_cls = client_mod.http.client.HTTPConnection
    orig_connect = conn_cls.connect
    attempts = []

    def connect(self):
        orig_connect(self)
        attempts[0].abort()  # the cancel lands while connecting
    conn_cls.connect = connect
    c = OllamaClient("http://x:1")
    orig_req = c._request

    def req(path, body, timeout, attempt):
        attempts.append(attempt)
        return orig_req(path, body, timeout, attempt)
    c._request = req
    with pytest.raises(LLMError, match="aborted"):
        run(c.chat("m", MSGS, options={}))
    assert tr.seen == [] and tr.active == 0


def test_a_job_that_dies_with_a_base_exception_is_logged_once(clock, caplog):
    """Task 17: _run passes every Exception to the submitter; a BaseException ends the job's
    task, and is now retrieved and logged when it happens, not reported later as never
    retrieved. The submitter is told the job stopped."""
    class Stop(BaseException):
        pass

    async def go():
        s = LLMScheduler(FakeLLM(), clock=clock)

        async def job(llm):
            raise Stop("halt")
        with pytest.raises(SchedulerClosed):
            await s.submit(Priority.SOCIAL, job)
        assert await s.submit(Priority.SOCIAL, lambda llm: llm.list_models()) == set()
    with caplog.at_level(logging.ERROR, logger="leeroy_mind"):
        run(go())
    assert caplog.text.count("llm_job_died") == 1 and "halt" in caplog.text


def test_fake_llm_records_json_shaped_options():
    llm = FakeLLM()
    run(llm.chat("m", MSGS, options=dict(LIVE.options)))
    assert llm.calls[0]["options"]["stop"] == ["\n\n"]


def test_the_reply_is_read_with_a_short_socket_timeout(monkeypatch):
    """Carry L60: once the reply has begun, each read waits READ_STALL_S at most, so a reply
    that stalls half way holds a cancel or a close for seconds, not the call's timeout."""
    tr = _patch(monkeypatch)
    run(OllamaClient("http://x:1").chat("m", MSGS, options={}, timeout=600.0))
    assert tr.timeouts == [600.0, OllamaClient.READ_STALL_S] and OllamaClient.READ_STALL_S <= 5


def test_a_reply_that_stalls_half_way_is_given_up_on(monkeypatch):
    """A real socket: the headers come, then nothing. The read gives up after READ_STALL_S."""
    monkeypatch.setattr(OllamaClient, "READ_STALL_S", 0.3)
    srv = socket.create_server(("127.0.0.1", 0))
    held = []

    def serve():
        c, _ = srv.accept()
        held.append(c)
        c.recv(65536)
        crlf = bytes((13, 10))
        c.sendall(crlf.join([b"HTTP/1.1 200 OK", b"Content-Type: application/json",
                             b"Content-Length: 100", b"", b"{"]))
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    try:
        c = OllamaClient(f"http://127.0.0.1:{srv.getsockname()[1]}", timeout=30.0)
        t0 = time.monotonic()
        with pytest.raises(LLMError):
            run(c.chat("m", MSGS, options={}))
        assert time.monotonic() - t0 < 5.0
    finally:
        th.join(2)
        for s in held:
            s.close()
        srv.close()


def test_the_total_time_cap_aborts_a_worker_that_misses_its_deadline():
    """Carry L60: the cap branch of _call, reached for certain. The worker here never checks
    its own deadline, so only the cap's abort lets it end."""
    seen = []

    class Deaf(OllamaClient):
        def _request(self, path, body, timeout, attempt):
            end = time.monotonic() + 2.0
            while not attempt.aborted and time.monotonic() < end:
                time.sleep(0.005)
            seen.append(attempt.aborted)
            raise LLMError(f"{path}: aborted")
    c = Deaf("http://x:1", timeout=0.05)
    c._CAP_SLACK = 0.05
    t0 = time.monotonic()
    with pytest.raises(LLMError, match="timed out"):
        run(c.chat("m", MSGS, options={}))
    assert seen == [True] and time.monotonic() - t0 < 1.0


def test_readiness_uses_poll_where_there_is_one(monkeypatch):
    """Carry L61: select() cannot watch a descriptor of 1024 or more on Linux; poll() can."""
    polled = []

    class Poll:
        def register(self, sock, mask):
            polled.append(mask)

        def poll(self, ms):
            polled.append(ms)
            return [(3, 1)]
    fake = types.SimpleNamespace(poll=Poll, POLLIN=1, POLLPRI=2, POLLERR=8, POLLHUP=16,
                                 select=lambda *a: pytest.fail("select used"))
    monkeypatch.setattr(client_mod, "select", fake)
    assert client_mod._readable(object(), 0.25) is True
    assert polled == [1 | 2 | 8 | 16, 250]
