"""Ambient game lines carry a send-time combat fence, including delayed fragments."""
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from engine_rig import engine_clock, make_rig
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Ack, Hello, Say, encode_command
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.wire import bridge_send


@pytest.fixture
def rig(store, tmp_path):
    r = make_rig(store, engine_clock(), tmp_path)
    r.engine.cfg = replace(r.cfg, talk=replace(r.cfg.talk, combat_quiet=True))
    r.engine.native_combat_quiet = True
    yield r
    r.close()


def deliver(rig, kind=TurnKind.CONTINUE, channel="guild", *, followup=False):
    turn = Turn(kind, 101, channel, to_guid=1)
    meta = TurnMeta("test")
    meta.realism_followup = followup
    return rig.sync(rig.engine._deliver(
        turn, meta, rig.engine.personas.get(101), "The next pull can wait.", []))


@pytest.mark.parametrize("kind", list(TurnKind))
@pytest.mark.parametrize("channel", ["guild", "raid", "party", "whisper", "say"])
def test_native_fence_marks_ambient_lines_but_not_answers_or_tactics(rig, kind, channel):
    commands = []

    async def reject(cmd):
        commands.append(cmd)
        return Ack(ts=0, id=cmd.id, ok=False, error="combat_quiet")

    rig.engine.send = reject
    assert deliver(rig, kind, channel) == "combat_quiet"
    assert commands[0].quiet_in_combat is (kind not in {
        TurnKind.REPLY_HUMAN, TurnKind.CALLOUT, TurnKind.URGENT})
    assert rig.store.conn.execute("SELECT COUNT(*) FROM chat_line WHERE origin='mind'").fetchone()[0] == 0
    assert not rig.engine._unacked  # a refused send must never be recovered as a successful echo


def test_combat_rejection_drops_a_delayed_fragment_without_recording_or_retry(rig):
    record = Mock()
    rig.engine._record = record
    calls = []

    @bridge_send
    async def send(cmd):
        calls.append(cmd)
        return Ack(ts=0, id=cmd.id, ok=len(calls) == 1,
                   error="" if len(calls) == 1 else "combat_quiet")

    rig.engine.send = send
    assert deliver(rig) is None  # first fragment landed before combat
    assert deliver(rig, followup=True) == "combat_quiet"
    assert len(calls) == 2 and all(c.quiet_in_combat for c in calls)
    assert record.call_count == 1 and not rig.engine._unacked


@pytest.mark.parametrize("enabled,native,expected", [
    (False, False, None), (False, True, None), (True, False, "combat_quiet_unavailable"),
])
def test_old_native_is_safe_and_off_preserves_the_wire(rig, enabled, native, expected):
    rig.engine.cfg = replace(rig.engine.cfg, talk=replace(rig.engine.cfg.talk, combat_quiet=enabled))
    rig.engine.native_combat_quiet = native
    rig.engine._record = Mock()
    calls = []

    @bridge_send
    async def send(cmd):
        calls.append(cmd)
        return Ack(ts=0, id=cmd.id, ok=True, error="")

    rig.engine.send = send
    assert deliver(rig) == expected
    if expected:
        assert not calls
        assert deliver(rig, TurnKind.REPLY_HUMAN) is None
    assert len(calls) == 1
    assert "quiet_in_combat" not in json.loads(encode_command(calls[0], 0))


@pytest.mark.parametrize("channel", ["dm:101", "discord:123"])
def test_discord_never_uses_the_native_fence(rig, channel):
    rig.engine.native_combat_quiet = False
    rig.engine._deliver_discord = Mock(return_value=None)
    rig.engine.send = Mock(side_effect=AssertionError("Discord reached the game bridge"))
    assert deliver(rig, channel=channel) is None
    rig.engine._deliver_discord.assert_called_once()


def test_optional_wire_flag_is_strict_and_absent_by_default():
    say = Say(id="s", guid=101, channel="guild", text="Ready.")
    assert "quiet_in_combat" not in json.loads(encode_command(say, 0))
    assert json.loads(encode_command(replace(say, quiet_in_combat=True), 0))["quiet_in_combat"] is True
    for bad in (1, "false", None):
        with pytest.raises(ValueError, match="quiet_in_combat"):
            encode_command(replace(say, quiet_in_combat=bad), 0)


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), ('"true"', False), ("1", False)])
def test_config_is_on_by_default_and_malformed_values_are_off(tmp_path, value, expected):
    path = tmp_path / "mind.toml"
    assert load_config(path).talk.combat_quiet is True  # since 2026-11-25
    path.write_text(f"[talk]\ncombat_quiet = {value}\n", encoding="utf-8")
    assert load_config(path).talk.combat_quiet is expected


def test_hello_and_disconnect_reset_native_support(tmp_path, clock):
    import asyncio
    from leeroy_mind.app import MindApp
    from leeroy_mind.testing.fake_llm import FakeLLM

    async def check():
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
        try:
            assert not app.engine.native_combat_quiet
            for features in (("combat_quiet",), (), ("combat_quiet",)):
                await app.handle(Hello(ts=1, server="mod-leeroy", version="0.1.0", guild_id=21,
                                       human_guid=0, features=features))
                assert app.engine.native_combat_quiet is bool(features)
            await app._on_connection(False)
            assert not app.engine.native_combat_quiet
        finally:
            await app.stop()

    asyncio.run(check())
