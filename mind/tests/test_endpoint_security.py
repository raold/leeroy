"""Control security checks use an endpoint started on its own free port and scratch key file."""
import json
import logging
import os

import pytest

from conftest import run
from leeroy_mind import endpoint
from test_endpoint import KEY, ask, raw, served


def test_duplicate_control_fields_are_not_last_value_wins(tmp_path):
    async def scenario():
        async with served(tmp_path) as (app, ep):
            wire = ('{"v":1,"key":"' + KEY + '","op":"status","op":"stop"}\n').encode()
            reply = json.loads(await raw(ep, wire))
            assert reply["ok"] is False
            assert app.stops == 0
    run(scenario())


def test_bad_keys_cannot_flood_control_logs(tmp_path, caplog):
    now = [0.0]
    async def scenario():
        async with served(tmp_path, clock=lambda: now[0]) as (app, ep):
            for _ in range(12):
                assert (await ask(ep, "stop", key="wrong-key"))["ok"] is False
            assert app.stops == 0
            now[0] = endpoint.REFUSAL_LOG_S
            assert (await ask(ep, "status", key="wrong-key"))["ok"] is False
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(scenario())
    assert sum("control_refused" in r.message for r in caplog.records) == 2
    assert "suppressed=11" in caplog.text
    assert KEY not in caplog.text and "wrong-key" not in caplog.text


def test_line_protocol_and_argument_guards_precede_actions(tmp_path, caplog):
    async def scenario():
        async with served(tmp_path, read_s=.1) as (app, ep):
            for wire in [b"GET / HTTP/1.1\r\n", b"{\xff}\n", b"{" + b" " * endpoint.MAX_LINE + b"}\n",
                         b'{"nested":' + b'[' * 40 + b'0' + b']' * 40 + b'}\n',
                         b'{"n":' + b'9' * 5000 + b'}\n']:
                assert json.loads(await raw(ep, wire))["ok"] is False
            for op in endpoint.OPS:
                assert (await ask(ep, op, key="bad"))["ok"] is False
            for op, args in [("raid", {"action": "form", "raid": "naxx", "size": True, "heroic": False, "lead": None}),
                             ("tune", {"action": "undo", "change": True}),
                             ("selfie", {"action": "more", "guid": 1 << 32}),
                             ("say", {"channel": "whisper", "text": "synthetic private text", "to": True}),
                             ("board", {"action": "withdraw", "id": True}),
                             ("library", {"query": "synthetic private text", "k": True})]:
                assert (await ask(ep, op, args))["ok"] is False
            first = json.dumps({"v": 1, "key": KEY, "op": "status"}).encode() + b"\n"
            second = json.dumps({"v": 1, "key": KEY, "op": "stop"}).encode() + b"\n"
            assert json.loads(await raw(ep, first + second))["ok"] is True
            assert app.stops == 0 and app.speech.lines == [] and app.board.asks == []
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(scenario())
    assert KEY not in caplog.text and "synthetic private text" not in caplog.text


def test_say_and_board_share_text_cleaning_and_never_truncate(tmp_path):
    async def scenario():
        async with served(tmp_path) as (app, ep):
            for op, args in [("say", {"channel": "guild", "text": "  alpha|\x00\n beta  "}),
                             ("board", {"action": "reply", "kind": "post", "to": 1, "text": "  alpha|\x00\n beta  "})]:
                assert (await ask(ep, op, args))["ok"] is True
            assert app.speech.lines[-1]["text"] == app.board.asks[-1]["text"] == "alpha beta"
            for text in ("", "|\x00", ".command", "!command", "x" * 256, "\ud800"):
                assert (await ask(ep, "say", {"channel": "guild", "text": text}))["ok"] is False
            for kind, size in (("post", 200), ("letter", 400)):
                assert (await ask(ep, "board", {"action": "reply", "kind": kind, "to": 1,
                                               "text": "x" * (size + 1)}))["ok"] is False
    run(scenario())


def test_control_key_file_is_private_bounded_and_not_a_link(tmp_path):
    path = tmp_path / "control.json"
    record = {"v": 1, "pid": os.getpid(), "port": 40001, "key": KEY}
    endpoint.write_key_file(path, record)
    with path.open("rb") as stream:
        assert endpoint.private_problem(stream.fileno()) is None
    assert endpoint.read_key_file(path) == {k: record[k] for k in ("pid", "port", "key")}
    other = tmp_path / "link.json"
    try:
        other.symlink_to(path)
    except OSError:
        pytest.skip("file symlinks unavailable")
    assert endpoint.read_key_file(other) is None
    path.write_bytes(b"x" * 2000)
    assert endpoint.read_key_file(path) is None
