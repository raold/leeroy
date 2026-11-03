"""The GPU arbiter (P1c Task 3): HttpOllamaAdmin's requests, the lease that says who may use the
GPU, and the hand-over between the live and the reflection model. Fakes only: the opener that
sends HttpOllamaAdmin's requests is replaced and FakeAdmin stands in for Ollama, so no model is
ever loaded or unloaded."""
import asyncio
import io
import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from conftest import run
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.presets import LIVE as LIVE_PRESET
from leeroy_mind.llm.presets import Preset
from leeroy_mind.nightly import gpu
from leeroy_mind.nightly.gpu import GpuArbiter, HttpOllamaAdmin, NullAdmin, norm
from nightly.conftest import FakeAdmin


class Sent(list):
    """What the fake network saw: (path, body) pairs, with the Request objects in .requests, their
    timeouts in .timeouts, the opener that sent each in .openers, the thread of each call in
    .threads and the replies read in .reads."""

    def __init__(self):
        super().__init__()
        self.requests, self.timeouts, self.openers, self.threads = [], [], [], []
        self.reads = 0


class Reply:
    """What the fake returns: a response that reads its bytes and works in a `with`."""

    def __init__(self, raw: bytes, sent: Sent):
        self.raw, self.sent = raw, sent

    def read(self, *args):
        self.sent.reads += 1
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture_urlopen(monkeypatch, ps=None, *, answer=None) -> Sent:
    """Stand in for the network: nothing leaves the process. HttpOllamaAdmin sends through an
    opener of its own, one that ignores the system proxy settings, so this replaces the opener's
    open() and makes urllib.request.urlopen, which follows those settings, fail the test.
    /api/ps answers `ps`, anything else {"done": true}; `answer(path, body)` may instead return
    the raw reply or raise."""
    sent = Sent()

    def fake(opener, req, data=None, timeout=None):
        assert req.data is None or isinstance(req.data, bytes)     # urllib refuses a str body
        path, body = urlsplit(req.full_url).path, (json.loads(req.data) if req.data else None)
        sent.append((path, body))
        sent.requests.append(req)
        sent.timeouts.append(timeout)
        sent.openers.append(opener)
        sent.threads.append(threading.get_ident())
        if answer is not None:
            return Reply(answer(path, body), sent)
        return Reply(json.dumps(ps if path.endswith("/api/ps") else {"done": True}).encode(), sent)

    def no_urlopen(*args, **kw):
        raise AssertionError("urlopen follows the system proxy settings")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake)
    monkeypatch.setattr(urllib.request, "urlopen", no_urlopen)
    return sent


def raising(make):
    """An `answer` for capture_urlopen that raises make() afresh on every call, as a server does
    (an HTTPError's body can be read once)."""
    def answer(path, body):
        raise make()
    return answer


class WatchedAdmin(FakeAdmin):
    """FakeAdmin that notes what `probe()` says at every unload and preload, before it acts."""

    def __init__(self, loaded, probe):
        super().__init__(loaded)
        self.probe, self.seen = probe, []

    async def unload(self, model):
        self.seen.append(("unload", self.probe()))
        await super().unload(model)

    async def preload(self, model, keep_alive, **kw):
        self.seen.append(("preload", self.probe()))
        await super().preload(model, keep_alive, **kw)


class FlakyAdmin(FakeAdmin):
    """FakeAdmin whose unload fails (LLMError) while `down` is set."""

    down = False

    async def unload(self, model):
        if self.down:
            raise LLMError("/api/generate: connection refused")
        await super().unload(model)


class Dispatch:
    """What Task 4's on_event does with the arbiter (PR18): the lease flips inline, in event
    order, and the GPU work of a first lease is a tracked task."""

    def __init__(self, arb):
        self.arb, self.back = arb, None

    async def login(self):
        if self.arb.hold("human"):
            self.back = asyncio.create_task(self.arb.hand_over())

    async def logout(self):
        await self.arb.release_live("human")


# -- HttpOllamaAdmin -----------------------------------------------------------------------------

def test_http_admin_request_shapes(monkeypatch):
    sent = capture_urlopen(monkeypatch, ps={"models": [{"name": "m:tag"}]})   # test-local: records (path, body)
    a = HttpOllamaAdmin("http://localhost:11434")
    assert run(a.loaded_models()) == ["m:tag"]
    run(a.unload("m:tag")); run(a.preload("m:tag", -1))
    assert sent[-2:] == [("/api/generate", {"model": "m:tag", "keep_alive": 0}),
                         ("/api/generate", {"model": "m:tag", "keep_alive": -1})]


def test_http_admin_methods_headers_timeouts_and_reads(monkeypatch):
    sent = capture_urlopen(monkeypatch, ps={"models": []})
    a = HttpOllamaAdmin("http://localhost:11434", timeout=7.5, preload_timeout=99.0)
    run(a.loaded_models()); run(a.unload("m")); run(a.preload("m", "10m"))
    assert sent == [("/api/ps", None), ("/api/generate", {"model": "m", "keep_alive": 0}),
                    ("/api/generate", {"model": "m", "keep_alive": "10m"})]
    assert [r.get_method() for r in sent.requests] == ["GET", "POST", "POST"]
    assert [r.get_header("Content-type") for r in sent.requests] == [None] + ["application/json"] * 2
    assert sent.timeouts == [7.5, 7.5, 99.0]        # a load gets a wait of its own
    assert sent.reads == 3                  # a call returns only once Ollama has answered in full
    default = capture_urlopen(monkeypatch, ps={"models": []})
    d = HttpOllamaAdmin("http://localhost:11434")
    run(d.loaded_models()); run(d.unload("m")); run(d.preload("m", -1))
    assert default.timeouts == [30.0, 30.0, 120.0]


def test_http_admin_preload_options_ride_along_only_when_given(monkeypatch):
    sent = capture_urlopen(monkeypatch)
    a = HttpOllamaAdmin("http://localhost:11434")
    run(a.preload("m", -1, options={"num_ctx": 4096}))
    run(a.preload("m", -1, options=None)); run(a.preload("m", -1, options={})); run(a.preload("m", -1))
    plain = ("/api/generate", {"model": "m", "keep_alive": -1})
    assert sent == [("/api/generate", {"model": "m", "keep_alive": -1, "options": {"num_ctx": 4096}}),
                    plain, plain, plain]


def test_http_admin_sends_through_an_opener_that_ignores_the_system_proxy(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(var, "http://proxy.invalid:3128")
    sent = capture_urlopen(monkeypatch, ps={"models": []})
    run(HttpOllamaAdmin("http://localhost:11434").loaded_models())
    # A default opener would hold a ProxyHandler built from those variables; ProxyHandler({})
    # keeps it out, and registers nothing itself.
    assert [h for h in sent.openers[0].handlers if isinstance(h, urllib.request.ProxyHandler)] == []


@pytest.mark.parametrize("base, path", [("http://localhost:11434/", "/api/ps"),
                                        ("http://host/ollama", "/ollama/api/ps"),
                                        ("http://host/ollama/", "/ollama/api/ps")])
def test_http_admin_joins_base_url_and_path(monkeypatch, base, path):
    sent = capture_urlopen(monkeypatch, ps={"models": []})
    run(HttpOllamaAdmin(base).loaded_models())
    assert sent[0][0] == path


@pytest.mark.parametrize("ps, names", [
    ({}, []), ({"models": None}, []), ({"models": []}, []),
    ({"models": [{"name": "a:1"}, {"model": "b:1"}, "junk", {"name": 5}, {"name": "c:2"}]},
     ["a:1", "c:2"])])
def test_http_admin_loaded_models_reads_the_names(monkeypatch, ps, names):
    capture_urlopen(monkeypatch, ps=ps)
    assert run(HttpOllamaAdmin("http://localhost:11434").loaded_models()) == names


def test_http_admin_failures_are_llm_errors(monkeypatch):
    refused = lambda: urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
    timed_out = lambda: TimeoutError("timed out")
    not_pulled = lambda: urllib.error.HTTPError(
        "http://localhost:11434/api/generate", 404, "Not Found", {},
        io.BytesIO(b'{"error": "model \'x\' not found"}'))
    a = HttpOllamaAdmin("http://localhost:11434")
    for make, call, needle in ((refused, a.loaded_models, "/api/ps: .*refused"),
                               (timed_out, a.loaded_models, "/api/ps: timed out"),
                               (not_pulled, lambda: a.preload("x", -1),
                                "/api/generate: HTTP 404 .*model 'x' not found"),
                               (not_pulled, lambda: a.unload("x"), "HTTP 404 .*not found")):
        capture_urlopen(monkeypatch, answer=raising(make))
        with pytest.raises(LLMError, match=needle):
            run(call())
    for raw, needle in ((b"<html>", "not JSON"), (b"[]", "not an object")):
        capture_urlopen(monkeypatch, answer=lambda path, body, raw=raw: raw)
        with pytest.raises(LLMError, match=needle):
            run(a.loaded_models())


def test_http_admin_waits_for_ollama_off_the_event_loop(monkeypatch):
    sent = capture_urlopen(monkeypatch, ps={"models": []})

    async def go():
        await HttpOllamaAdmin("http://localhost:11434").loaded_models()
        return threading.get_ident()
    loop_thread = run(go())
    assert len(sent.threads) == 1 and sent.threads[0] != loop_thread


def test_norm_adds_the_default_tag_only_to_a_name_without_one():
    assert norm("m") == "m:latest" and norm("m:tag") == "m:tag"
    assert norm("hf.co/a/b") == "hf.co/a/b:latest" and norm("hf.co/a/b:IQ4_XS") == "hf.co/a/b:IQ4_XS"
    assert norm("localhost:5000/m") == "localhost:5000/m:latest"        # a registry port is no tag
    assert norm("localhost:5000/m:v1") == "localhost:5000/m:v1"


# -- GpuArbiter ----------------------------------------------------------------------------------

def test_login_preempts_then_unloads_then_preloads(h):
    order = []
    admin = FakeAdmin([h.REFL, h.EMB]); admin.calls = order
    arb = GpuArbiter(admin, h.settings)
    async def preempt(): order.append(("preempt",))
    arb.set_preempt(preempt)
    run(arb.acquire_live("human"))
    assert order == [("preempt",), ("unload", h.REFL), ("preload", h.LIVE, -1)] and not arb.reflection_allowed()


def test_holders_and_release(h):
    admin = FakeAdmin([h.EMB]); arb = GpuArbiter(admin, h.settings)
    async def go():
        async def noop(): pass
        arb.set_preempt(noop)
        await arb.acquire_live("human"); n = len(admin.calls)
        await arb.acquire_live("offline_raid"); assert len(admin.calls) == n
        await arb.release_live("human"); assert not arb.reflection_allowed()
        await arb.release_live("offline_raid"); assert arb.reflection_allowed()
    run(go())


def test_ensure_model_and_foreign(h):
    admin = FakeAdmin([h.LIVE, h.EMB, h.REFL]); arb = GpuArbiter(admin, h.settings)
    run(arb.ensure_model(h.REFL))
    assert admin.calls == [("unload", h.LIVE)]
    admin.loaded.append(h.LIVE)                     # mod-ollama-chat reloads the live model mid-stage
    assert run(arb.foreign_models(h.REFL)) == [h.LIVE]


def test_the_arbiter_exposes_its_admin_and_hands_over_without_a_preempt_callback(h):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings)
    assert arb.admin is admin and arb.holders == frozenset() and arb.reflection_allowed()
    run(arb.acquire_live("human"))                  # no set_preempt: nothing to cancel
    assert admin.calls == [("unload", h.REFL), ("preload", h.LIVE, -1)]
    assert admin.loaded == [h.EMB, h.LIVE]


def test_the_hand_back_preloads_the_live_model_at_the_live_context(h):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings)
    run(arb.acquire_live("human"))
    assert admin.calls[-1] == ("preload", h.LIVE, -1)
    # A different num_ctx reloads a model, so the preload must ask for the one live requests use.
    assert admin.preload_options == [{"num_ctx": LIVE_PRESET.options["num_ctx"]}]


def test_the_hand_back_takes_the_context_from_the_live_preset(h, monkeypatch):
    monkeypatch.setattr(gpu, "LIVE_PRESET", Preset(False, {"num_ctx": 2048}))
    admin = FakeAdmin([h.EMB]); arb = GpuArbiter(admin, h.settings)
    run(arb.acquire_live("human"))                  # one source: nothing copied from the preset
    assert admin.preload_options == [{"num_ctx": 2048}]


def test_the_lease_is_held_from_the_start_of_the_hand_over(h):
    seen = []
    admin = WatchedAdmin([h.REFL, h.EMB], lambda: (arb.holders, arb.reflection_allowed()))
    arb = GpuArbiter(admin, h.settings)
    async def preempt(): seen.append(("preempt", arb.holders, arb.reflection_allowed()))
    arb.set_preempt(preempt)
    run(arb.acquire_live("human"))                  # a night must not start while the GPU changes hands
    assert seen == [("preempt", frozenset({"human"}), False)]
    assert admin.seen == [("unload", (frozenset({"human"}), False)),
                          ("preload", (frozenset({"human"}), False))]


def test_only_the_first_holder_hands_over(h):
    admin = FakeAdmin([h.EMB]); arb = GpuArbiter(admin, h.settings); preempts = []
    async def preempt(): preempts.append(1)
    arb.set_preempt(preempt)
    async def go():
        await arb.acquire_live("human")
        assert admin.calls == [("preload", h.LIVE, -1)]      # the reflection model is not loaded
        assert len(preempts) == 1
        await arb.acquire_live("human"); await arb.acquire_live("offline_raid")
        assert admin.calls == [("preload", h.LIVE, -1)] and len(preempts) == 1
        assert arb.holders == {"human", "offline_raid"}
    run(go())


def test_a_holder_is_one_lease_however_often_it_asks(h):
    admin = FakeAdmin([h.EMB]); arb = GpuArbiter(admin, h.settings)
    async def go():
        await arb.acquire_live("human"); n = len(admin.calls)
        await arb.acquire_live("human"); assert len(admin.calls) == n    # hello, then login
        assert arb.holders == {"human"} and type(arb.holders) is frozenset
        await arb.release_live("human"); assert arb.reflection_allowed()  # one logout ends it
        await arb.release_live("human"); await arb.release_live("ghost")  # nobody held these
        assert arb.reflection_allowed() and arb.holders == frozenset()
    run(go())


# -- the lease flips at the call; only the GPU work waits its turn -------------------------------

def test_hold_takes_the_lease_with_no_await_and_names_the_first_holder(h):
    arb = GpuArbiter(FakeAdmin([h.EMB]), h.settings)
    assert arb.hold("human") is True
    assert arb.holders == {"human"} and not arb.reflection_allowed()    # nothing was awaited
    assert arb.hold("offline_raid") is False and arb.hold("human") is False
    assert arb.holders == {"human", "offline_raid"}


def test_a_spawned_hand_back_then_an_inline_logout_leaves_nobody_holding(h):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings); preempts = []
    async def preempt(): preempts.append(1)
    arb.set_preempt(preempt)
    async def go():
        assert arb.hold("human")                            # the login flips the lease inline ...
        back = asyncio.create_task(arb.hand_over())          # ... and the GPU work leaves the dispatcher
        assert arb.holders == {"human"} and not back.done()
        await arb.release_live("human")                      # the logout, inline, before that task has run
        assert arb.holders == frozenset() and arb.reflection_allowed()
        await back
        assert arb.holders == frozenset()                    # the logout was not undone by the late task
        assert admin.calls == [] and preempts == []          # and the lease that ended got no GPU work
    run(go())


def test_a_holders_check_right_after_the_events_sees_the_new_state(h):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings); d = Dispatch(arb)
    async def go():
        await d.logout()                                     # a hello with nobody online
        await d.login()
        assert arb.holders == {"human"} and not d.back.done()      # before the hand-back has run
        await d.back
        assert admin.calls == [("unload", h.REFL), ("preload", h.LIVE, -1)]
        await d.logout(); await d.login()                    # a quick relog
        assert arb.holders == {"human"}
        await d.back
        assert arb.holders == {"human"} and admin.loaded == [h.EMB, h.LIVE]
    run(go())


def test_a_logout_does_not_wait_for_a_hand_over_in_progress(h):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings)
    async def go():
        gate = asyncio.Event()
        async def preempt(): await gate.wait()
        arb.set_preempt(preempt)
        login = asyncio.create_task(arb.acquire_live("human"))
        await asyncio.sleep(0)                      # under way, held at the preempt
        assert arb.holders == {"human"}
        await asyncio.wait_for(arb.release_live("human"), 1)    # the logout does not queue behind it
        assert arb.holders == frozenset() and arb.reflection_allowed()
        relogin = asyncio.create_task(arb.acquire_live("human"))
        await asyncio.sleep(0)
        assert arb.holders == {"human"} and not (login.done() or relogin.done())
        gate.set(); await asyncio.gather(login, relogin)
        assert arb.holders == {"human"} and not arb.reflection_allowed()   # the last word was a login
        assert admin.loaded == [h.EMB, h.LIVE]      # and the GPU ended up with the live model
    run(go())


def test_ensure_model_waits_for_a_hand_over_in_progress(h):
    admin = FakeAdmin([h.LIVE, h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings)
    async def go():
        gate = asyncio.Event()
        async def preempt(): await gate.wait()
        arb.set_preempt(preempt)
        login = asyncio.create_task(arb.acquire_live("human"))
        await asyncio.sleep(0)
        ensure = asyncio.create_task(arb.ensure_model(h.REFL))
        for _ in range(5): await asyncio.sleep(0)
        assert admin.calls == [] and not ensure.done()          # it takes its turn
        gate.set(); await asyncio.gather(login, ensure)
        assert admin.calls == [("unload", h.REFL), ("preload", h.LIVE, -1), ("unload", h.LIVE)]
    run(go())


def test_a_failed_hand_over_keeps_the_lease_and_frees_the_arbiter(h):
    admin = FlakyAdmin([h.REFL, h.EMB]); admin.down = True
    arb = GpuArbiter(admin, h.settings)
    async def go():
        with pytest.raises(LLMError, match="connection refused"):
            await arb.acquire_live("human")
        assert arb.holders == {"human"} and not arb.reflection_allowed()   # he is online all the same
        admin.down = False
        await arb.release_live("human"); assert arb.reflection_allowed()   # the lock was let go
        await arb.ensure_model(h.LIVE)
        assert admin.calls == [("unload", h.REFL)]
    run(go())


# -- live(): the lease for a block, whatever happens to its caller --------------------------------

def test_live_holds_the_lease_for_the_block_and_gives_it_back(h):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings)
    async def go():
        async with arb.live("offline_raid"):
            assert arb.holders == {"offline_raid"} and not arb.reflection_allowed()
            assert admin.calls == [("unload", h.REFL), ("preload", h.LIVE, -1)]    # handed over first
        assert arb.holders == frozenset() and arb.reflection_allowed()
    run(go())


def test_live_gives_the_lease_back_when_the_block_raises(h):
    arb = GpuArbiter(FakeAdmin([h.EMB]), h.settings)
    async def go():
        with pytest.raises(RuntimeError, match="raid crashed"):
            async with arb.live("offline_raid"):
                raise RuntimeError("raid crashed")
        assert arb.reflection_allowed()
    run(go())


def test_live_gives_the_lease_back_when_the_hand_over_fails(h):
    admin = FlakyAdmin([h.REFL, h.EMB]); admin.down = True; arb = GpuArbiter(admin, h.settings)
    async def go():
        ran = []
        with pytest.raises(LLMError):
            async with arb.live("offline_raid"):
                ran.append(1)
        assert ran == [] and arb.reflection_allowed()
    run(go())


def test_live_never_leaks_the_lease_when_its_caller_is_cancelled_mid_hand_over(h):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings)
    async def go():
        gate = asyncio.Event()
        async def preempt(): await gate.wait()
        arb.set_preempt(preempt)
        async def raid():
            async with arb.live("offline_raid"):
                await asyncio.sleep(3600)
        t = asyncio.create_task(raid())
        await asyncio.sleep(0)                      # held at the preempt: the hand-over is under way
        assert arb.holders == {"offline_raid"}
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert arb.holders == frozenset() and arb.reflection_allowed()
        await asyncio.wait_for(arb.ensure_model(h.LIVE), 5)         # and the lock was let go
    run(go())


def test_a_bare_acquire_that_is_cancelled_keeps_the_lease_until_released(h):
    # The lease counts from the call, so a caller that does not use live() releases in a finally
    # that also covers a failed or cancelled acquire, and puts the acquire inside its try.
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings)
    async def go():
        gate = asyncio.Event()
        async def preempt(): await gate.wait()
        arb.set_preempt(preempt)
        t = asyncio.create_task(arb.acquire_live("offline_raid"))
        await asyncio.sleep(0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert arb.holders == {"offline_raid"}                      # held: the caller's finally releases it
        await asyncio.wait_for(arb.ensure_model(h.LIVE), 5)         # the lock was let go
        await arb.release_live("offline_raid")
        assert arb.reflection_allowed()
    run(go())


# -- the preempt callback is bounded -------------------------------------------------------------

def test_a_preempt_callback_that_never_returns_cannot_wedge_the_arbiter(h, caplog):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings, preempt_timeout=0.05)
    async def stuck(): await asyncio.Event().wait()
    arb.set_preempt(stuck)
    async def go():
        await asyncio.wait_for(arb.acquire_live("human"), 5)
        assert admin.calls == [("unload", h.REFL), ("preload", h.LIVE, -1)]   # the GPU still changed hands
        await asyncio.wait_for(arb.ensure_model(h.LIVE), 5)                   # and the lock is free
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(go())
    assert "nightly_preempt_timeout: seconds=0.05" in caplog.text


def test_a_preempt_callback_that_swallows_the_deadline_is_still_logged(h, caplog):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings, preempt_timeout=0.05)
    async def swallowing():                 # a cancel_run that suppresses CancelledError
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
    arb.set_preempt(swallowing)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(asyncio.wait_for(arb.acquire_live("human"), 5))
    assert "nightly_preempt_timeout: seconds=0.05" in caplog.text
    assert admin.calls == [("unload", h.REFL), ("preload", h.LIVE, -1)]


def test_a_timeout_raised_by_the_preempt_callback_itself_is_not_its_expiry(h, caplog):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings)
    async def boom(): raise TimeoutError("its own")
    arb.set_preempt(boom)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        with pytest.raises(TimeoutError, match="its own"):
            run(arb.acquire_live("human"))
    assert "nightly_preempt_timeout" not in caplog.text
    assert admin.calls == [] and arb.holders == {"human"}       # the error stops the hand-over; the lease stays


def test_a_cancel_path_that_calls_back_into_the_arbiter_costs_the_timeout_not_a_wedge(h):
    admin = FakeAdmin([h.REFL, h.EMB]); arb = GpuArbiter(admin, h.settings, preempt_timeout=0.05)
    async def go():
        started = asyncio.Event()
        async def night():
            started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                await arb.ensure_model(h.LIVE)          # a cleanup that calls back into the arbiter
        nt = asyncio.create_task(night()); await started.wait()
        async def cancel_run():
            nt.cancel()
            await asyncio.gather(nt, return_exceptions=True)
        arb.set_preempt(cancel_run)
        await asyncio.wait_for(arb.acquire_live("human"), 5)
        assert ("preload", h.LIVE, -1) in admin.calls
        await asyncio.wait_for(arb.release_live("human"), 1)    # never queued behind the hand-over
        assert arb.reflection_allowed()
        await asyncio.wait_for(asyncio.gather(nt, return_exceptions=True), 5)
        assert nt.done()
    run(go())


# -- ensure_model, foreign_models, names ----------------------------------------------------------

def test_ensure_model_touches_only_the_live_and_reflection_models(h):
    other = "qwen3:8b"
    admin = FakeAdmin([h.LIVE, h.REFL, h.EMB, other]); arb = GpuArbiter(admin, h.settings)
    run(arb.ensure_model(h.LIVE))
    assert admin.calls == [("unload", h.REFL)]
    run(arb.ensure_model(h.REFL))
    assert admin.calls == [("unload", h.REFL), ("unload", h.LIVE)]
    run(arb.ensure_model(h.REFL))
    assert len(admin.calls) == 2                    # nothing else to unload, and it never preloads
    assert admin.loaded == [h.EMB, other]


def test_ensure_model_needs_no_admin_call_when_nothing_is_in_the_way(h):
    admin = FakeAdmin([h.EMB]); arb = GpuArbiter(admin, h.settings)
    run(arb.ensure_model(h.REFL)); run(arb.ensure_model(h.LIVE))
    assert admin.calls == []


def test_nothing_is_swapped_when_the_reflection_model_is_the_live_model(h):
    admin = FakeAdmin([h.LIVE, h.EMB]); arb = GpuArbiter(admin, replace(h.settings, reflection_model=h.LIVE))
    run(arb.acquire_live("human"))
    assert admin.calls == [("preload", h.LIVE, -1)]         # no unload and reload of the one model
    run(arb.ensure_model(h.LIVE))
    assert admin.calls == [("preload", h.LIVE, -1)]
    run(arb.ensure_model(h.EMB))                            # asked for another model, it goes once
    assert admin.calls[1:] == [("unload", h.LIVE)]


def test_names_are_compared_with_their_default_tag(h):
    s = replace(h.settings, reflection_model="deep-model", embedding_model="embed-model")
    admin = FakeAdmin([h.LIVE, "deep-model:latest", "embed-model:latest"]); arb = GpuArbiter(admin, s)
    run(arb.ensure_model(h.LIVE))
    assert admin.calls == [("unload", "deep-model:latest")]        # unloaded under the name Ollama lists
    admin.loaded.append("deep-model:latest")
    assert run(arb.foreign_models("deep-model")) == [h.LIVE]
    assert run(arb.foreign_models(h.LIVE)) == ["deep-model:latest"]
    run(arb.acquire_live("human"))
    assert admin.calls[-2:] == [("unload", "deep-model:latest"), ("preload", h.LIVE, -1)]
    bare = GpuArbiter(FakeAdmin(["deep-model", "embed-model"]), s)       # listed without a tag
    assert run(bare.foreign_models("deep-model:latest")) == []


def test_foreign_models_are_what_is_loaded_besides_the_expected_and_the_embedding(h):
    admin = FakeAdmin([h.REFL, h.EMB, h.LIVE, "qwen3:8b"]); arb = GpuArbiter(admin, h.settings)
    assert run(arb.foreign_models(h.REFL)) == [h.LIVE, "qwen3:8b"]
    assert run(arb.foreign_models(h.LIVE)) == [h.REFL, "qwen3:8b"]
    assert run(arb.foreign_models("qwen3:8b")) == [h.REFL, h.LIVE]
    assert admin.calls == []                                        # it only looks
    assert run(GpuArbiter(FakeAdmin([h.REFL, h.EMB]), h.settings).foreign_models(h.REFL)) == []
    assert run(GpuArbiter(FakeAdmin([]), h.settings).foreign_models(h.REFL)) == []


def test_null_admin_loads_nothing_and_does_nothing(h):
    admin = NullAdmin(); arb = GpuArbiter(admin, h.settings)
    async def go():
        assert await admin.loaded_models() == []
        assert await admin.unload("m") is None and await admin.preload("m", -1) is None
        assert await admin.preload("m", -1, options={"num_ctx": 4096}) is None
        await arb.acquire_live("human"); await arb.ensure_model(h.REFL)
        assert await arb.foreign_models(h.REFL) == [] and arb.holders == {"human"}
    run(go())
    assert arb.admin is admin and type(arb.admin).__name__ == "NullAdmin"
