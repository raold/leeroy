"""The control endpoint's test doubles (leeroy_mind.testing.control) keep the real endpoint's
contract: what parse_request refuses they refuse in its words, a scripted answer crosses the
wire as a real one does, and each breach fails the test that made it (conftest). A real
endpoint on 127.0.0.1:0 with a fake app checks the refusals word for word."""
import asyncio
import contextlib

import pytest

from conftest import run
from leeroy_mind import endpoint
from leeroy_mind.console.mindlink import MindLink
from leeroy_mind.console.netinfo import LISTEN, Conn
from leeroy_mind.endpoint import KEY_FILE, ControlCfg, ControlEndpoint, write_key_file
from leeroy_mind.testing import control


@pytest.fixture
def breaches():
    """The breaches a test makes on purpose, taken out before the conftest check."""
    yield control.breaches
    control.breaches.clear()


def test_a_request_the_endpoint_takes_gets_the_scripted_answer_as_the_wire_carries_it(breaches):
    reply = {"ok": True, "pair": (1, 2), "by_guid": {101: "Arkon"}}
    got = control.answer("raid", {"action": "call"}, reply)
    assert got == {"ok": True, "pair": [1, 2], "by_guid": {"101": "Arkon"}}
    assert control.answer("status", None, lambda op, args: {"ok": True, "op": op}) == {
        "ok": True, "op": "status"}
    assert breaches == []


@pytest.mark.parametrize("op, args, words", [
    ("raid", {"action": "nope"}, "raid: an action"),
    ("raid", None, "bad request: raid needs args"),
    ("status", {"action": "x"}, "bad request: status takes no args"),
    ("bank", {}, "bad request: bank takes no args"),
    ("summon", {"action": "x"}, "bad request: op is one of"),
    ("say", {"channel": "guild", "text": "hi", "to": 101}, "A guild line takes exactly"),
    ("board", {"action": "reply", "kind": "post", "to": "7", "text": "hi"}, "to is the post"),
])
def test_a_request_the_endpoint_refuses_gets_its_words_and_breaks_the_contract(
        breaches, op, args, words):
    got = control.answer(op, args, {"ok": True})
    assert got["ok"] is False and words in got["error"]
    assert len(breaches) == 1 and repr(op) in breaches[0]


def test_a_request_that_cannot_be_sent_is_a_contract_error(breaches):
    with pytest.raises(control.ContractError, match="cannot be sent"):
        control.answer("library", {"query": {1, 2}}, {"ok": True})
    with pytest.raises(control.ContractError, match="cannot be sent"):
        control.answer("library", {"query": "x", "k": float("nan")}, {"ok": True})
    assert len(breaches) == 2


def test_a_line_over_max_line_is_refused_as_the_endpoint_refuses_it(breaches):
    got = control.answer("board", {"action": "reply", "kind": "letter", "to": 7,
                                   "text": "\U0001f600" * (endpoint.MAX_LINE // 12)}, {"ok": True})
    assert got == {"ok": False, "error": "bad request"} and len(breaches) == 1


@pytest.mark.parametrize("reply", [
    None, {}, {"ok": 1}, {"ok": "true"}, [True], {"ok": True, "x": float("nan")},
    {"ok": True, "x": object()}, {"ok": True, "x": "a" * endpoint.MAX_REPLY},
])
def test_an_answer_the_endpoint_cannot_give_is_a_contract_error(breaches, reply):
    with pytest.raises(control.ContractError, match="cannot give"):
        control.answer("status", None, reply)
    assert len(breaches) == 1


def test_the_request_stand_in_serves_a_real_mindlink_by_the_same_rules(tmp_path, breaches):
    key = "q" * 43
    write_key_file(tmp_path / KEY_FILE, {"v": 1, "pid": 4242, "port": 40000, "key": key})
    fake = control.request({"ok": True, "stopping": True})
    link = MindLink(tmp_path, tcp=lambda: [Conn("127.0.0.1", 40000, "0.0.0.0", 0, LISTEN, 4242)],
                    request=fake)
    assert link.call((4242,), "stop") == {"ok": True, "stopping": True}
    assert fake.calls == [(40000, key, "stop", None)]
    assert link.call((4242,), "stop", {"now": True})["error"] == "bad request: stop takes no args"
    assert len(breaches) == 1


class _App:
    def __init__(self, data_dir):
        self.data_dir, self.stopping, self.guild = data_dir, False, None


@pytest.mark.parametrize("op, args", [
    ("raid", {"action": "nope"}), ("raid", None), ("status", {"x": 1}), ("nope", None),
    ("tune", {"action": "set", "scope": "world"}), ("say", {"channel": "guild", "text": ""}),
    ("selfie", {"action": "pick", "guid": 0, "set": 1, "index": 0}),
    ("discord", {"action": "react", "message": 1, "emoji": "not one"}),
    ("library", {"query": ""}), ("decision", {"action": "vote", "poll": True, "option": "a"}),
    ("string", {"action": "nope"}), ("raidlead", {"action": "set"}), ("ops", {"action": "nope"}),
    ("looks", {"action": "undo"}), ("library", {"query": "x" * endpoint.MAX_LINE}),
])
def test_the_doubles_refuse_in_the_real_endpoints_words(tmp_path, breaches, op, args):
    async def go():
        ep = ControlEndpoint(_App(tmp_path), ControlCfg(), key=control.KEY)
        task = asyncio.create_task(ep.serve())
        await asyncio.wait_for(ep.listening.wait(), 5)
        try:
            return await asyncio.to_thread(endpoint.request, ep.port, control.KEY, op, args,
                                           timeout=5)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    real = run(go())
    assert real["ok"] is False
    assert control.answer(op, args, {"ok": True}) == real
    assert len(breaches) == 1


def test_a_breach_the_console_swallowed_still_fails_the_check():
    """The conftest's check after every test: a breach left behind fails it, named."""
    control.answer("raid", {"action": "nope"}, {"ok": True})
    control.answer("bank", {}, {"ok": True})
    with pytest.raises(AssertionError, match="contract broken: control 'raid'.*; control 'bank'"):
        control.verify()
    assert control.breaches == []
    control.verify()
