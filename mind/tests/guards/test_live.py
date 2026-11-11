import asyncio
from dataclasses import replace
import hashlib
import logging

import pytest

from conftest import unit
from engine_rig import engine_clock, make_rig
from leeroy_mind.claims.store import add
from leeroy_mind.config import load_config
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.channels import channel_id, is_discord
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.guards import GuardsCfg, Verdict
from leeroy_mind.protocol import Chat, Emote, Group
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind


@pytest.fixture
def rig(store, tmp_path):
    r = make_rig(store, engine_clock(), tmp_path, reply="the ferry leaves at seven")
    r.clock._yields = 0  # read-only SQLite workers need real scheduling between fake ticks
    r.engine.live_review.config = GuardsCfg(enabled=True)
    r.world.apply(Group(0, 1, True, (1, 101, 102, 103, 104, 105)), r.clock.now())
    yield r
    r.close()


def turn(channel="guild", **kw):
    return Turn(TurnKind.REPLY_HUMAN, 101, channel, to_guid=1 if channel in {"whisper", "dm:101"} else 0,
                addressee=1, **kw)


def speak(rig, t, meta=None):
    meta = meta or TurnMeta(rig.engine._new_id(), why="test", answers_human=True)
    async def go():
        task = asyncio.create_task(rig.engine._speak(t, meta))
        await rig.advance(100)
        assert task.done()
        return task.result()
    return rig.sync(go()), meta


def source(rig, text, channel="guild", who=1, to=0):
    audience = rig.world.audience(channel, from_guid=who, to_guid=to)
    return rig.store.insert_chat_line(Chat(0, channel, who, "Invented", to, text, 0, 10),
        session_id=None, origin="human" if who == 1 else "mind", audience=audience)


@pytest.mark.parametrize("value", ['false', '"yes"', '1', '[]', '{}'])
def test_invalid_or_disabled_aware_is_off(tmp_path, value):
    path = tmp_path / "mind.toml"
    path.write_text("[talk]\naware=" + value, encoding="utf-8")
    assert load_config(path).talk.aware is False


def test_unknown_talk_setting_preserves_aware(tmp_path):
    path = tmp_path / "mind.toml"
    path.write_text("[talk]\naware=true\nunrecognized=true", encoding="utf-8")
    assert load_config(path).talk.aware is True


@pytest.mark.parametrize("channel", ["guild", "party", "raid", "whisper", "discord:general", "dm:101"])
def test_composed_prompts_get_one_bounded_card_only_when_on(rig, channel):
    e = rig.engine
    t = turn(channel)
    before, _ = rig.sync(e.compose(t))
    assert live.BEGIN not in str(before)
    e.cfg = replace(e.cfg, talk=replace(e.cfg.talk, aware=True))
    after, _ = rig.sync(e.compose(t))
    text = "\n".join(m["content"] for m in after)
    assert text.count(live.BEGIN) == text.count(live.END) == 1
    assert "Channel: " + channel in text
    e.cfg = replace(e.cfg, talk=replace(e.cfg.talk, aware=False))
    again, _ = rig.sync(e.compose(t))
    assert again == before


@pytest.mark.parametrize("channel,peer,shown", [
    ("guild", 0, False), ("party", 0, False), ("raid", 0, False),
    ("whisper", 1, True), ("whisper", 102, False), ("discord:general", 0, False), ("dm:101", 1, False)])
def test_owner_claim_card_inherits_whisper_pair_and_medium(rig, channel, peer, shown):
    line = source(rig, "PAIR_ONLY", "whisper", who=101, to=1)
    add(rig.store, 101, kind="opinion", subject="ferry", text="PAIR_ONLY", polarity=1,
        source="owner", source_line=line, embedding=pack_vec(unit(0)), now=rig.clock.now())
    e = rig.engine
    e.cfg = replace(e.cfg, talk=replace(e.cfg.talk, aware=True))
    t = turn(channel)
    t.to_guid = peer
    block = e.live_review.block(t, TurnMeta("test"), close=False)
    assert ("PAIR_ONLY" in block) is shown


def test_discord_context_has_parent_thread_reactions_mentions_and_no_other_medium(rig):
    e = rig.engine
    e.cfg = replace(e.cfg, talk=replace(e.cfg.talk, aware=True))
    source(rig, "GAME_ONLY")
    dstore.post(rig.store, channel="dm:102", author=1, origin="human", text="OTHER_PAIR", now=rig.clock.now())
    own = dstore.post(rig.store, channel="general", author=101, origin="mind", text="ferry time", now=rig.clock.now())
    dstore.react(rig.store, own, member=1, emoji="👍", now=rig.clock.now())
    parent = dstore.post(rig.store, channel="general", author=1, origin="human",
        text="@Arkon when is the ferry?", now=rig.clock.now())
    dstore.pin(rig.store, parent, now=rig.clock.now())
    meta = TurnMeta("test", discord=DiscordTarget(parent, None))
    block = e.live_review.block(turn("discord:general"), meta, close=False)
    assert "Reply to:" in block and "Thread:" in block and "Pin:" in block
    assert "reacted 👍" in block and "mentioned you" in block
    assert "GAME_ONLY" not in block and "OTHER_PAIR" not in block


@pytest.mark.parametrize("action,expected", [("send", True), ("drop", False)])
def test_send_and_drop(rig, monkeypatch, action, expected):
    seen = []
    def review(stores, draft):
        seen.append(draft)
        return Verdict(action)
    monkeypatch.setattr(live, "review", review)
    sent, meta = speak(rig, Turn(TurnKind.OPEN,101,"guild"),TurnMeta("idle"))
    assert bool(sent) is expected and seen
    assert meta.guard_verdict == action


@pytest.mark.parametrize("channel", ["discord:general", "dm:101"])
@pytest.mark.parametrize("text", ["😂👍", "👩🏽‍💻 🔥", "🇬🇧", "❤️"])
def test_emoji_only_discord_messages_pass_both_real_guard_checks(rig, monkeypatch, channel, text):
    rig.script(text)
    calls = []
    original = live.review
    def checked(stores, draft):
        calls.append(draft.text)
        return original(stores, draft)
    monkeypatch.setattr(live, "review", checked)
    sent, meta = speak(rig, turn(channel))
    assert sent == [text] and len(calls) == 2
    assert meta.guard_verdict == "send" and not rig.sent.log


@pytest.mark.parametrize("channel", ["discord:general", "dm:101"])
@pytest.mark.parametrize("text", [" ", "...", "\u200d"])
def test_wordless_nonemoji_is_still_empty(rig, channel, text):
    verdict = rig.sync(rig.engine.live_review.check(turn(channel), TurnMeta("t1"), text))
    assert verdict.action == "drop" and verdict.codes == ("empty",)


def voice_line(rig, text, *, channel="voice:guild_hall", who=1, readers=(101, 102)):
    return rig.store.insert_chat_line(Chat(int((rig.clock.now()-1)*1000), channel,
        who, "Invented", 0, text, 0, 10), session_id=None,
        origin="human" if who == 1 else "mind", audience=readers)


def test_voice_card_uses_actual_room_and_captured_audience(rig):
    e = rig.engine
    e.cfg = replace(e.cfg, talk=replace(e.cfg.talk, aware=True))
    e.world.room_audience = lambda _: frozenset({101, 102})
    parent = voice_line(rig, "VISIBLE_PARENT when is the ferry?")
    voice_line(rig, "HIDDEN_SAME_ROOM", readers=(102,))
    voice_line(rig, "OTHER_ROOM", channel="voice:healers")
    voice_line(rig, "OFFICER_ROOM", channel="voice:officers")
    source(rig, "PRIVATE_PAIR", "whisper", who=102, to=1)
    source(rig, "TYPED_ONLY")
    t, meta = turn("voice:guild_hall", trigger_line=parent), TurnMeta("test", typed_on=("guild", 0))
    history, context = e.live_review.history(t, meta)
    assert [h.id for h in history] == [parent]
    assert e.live_review.draft(t, meta, "answer").channel == "voice"
    block = e.live_review.block(t, meta, close=False)
    assert "Channel: voice:guild_hall" in block and f"Reply to: {e.human.called}: VISIBLE_PARENT" in block
    assert all(text not in block for text in ("HIDDEN_SAME_ROOM", "OTHER_ROOM", "OFFICER_ROOM", "PRIVATE_PAIR", "TYPED_ONLY"))


def test_voice_own_repeat_and_parent_relevance_use_spoken_history(rig):
    e = rig.engine
    text = "the copper lantern hangs above the doorway"
    own = voice_line(rig, text, who=101)
    t, meta = turn("voice:guild_hall"), TurnMeta("test")
    verdict = rig.sync(e.live_review.check(t, meta, text))
    assert verdict.action == "drop" and "own_repeat_same_channel" in verdict.codes
    assert any(r.id == own and r.kind == "line" for r in verdict.related)
    parent = voice_line(rig, "when is the ferry arriving tonight?")
    t.trigger_line = parent
    e.live_review.config = replace(e.live_review.config, semantic_enabled=True)
    async def embed(texts, priority):
        return [pack_vec(unit(0 if text == "a different thought about cooking supper" else 1)) for text in texts]
    e.recall._embedder.embed = embed
    verdict = rig.sync(e.live_review.check(t, meta, "a different thought about cooking supper"))
    assert "low_parent_and_thread_relevance" in verdict.codes
    assert any(s.code == "parent_relevance" and s.value == 0 for s in verdict.scores)


@pytest.mark.parametrize("second", ["send", "rewrite", "drop"])
def test_rewrite_asks_once_and_checks_the_new_draft(rig, monkeypatch, second):
    calls = []
    def review(stores, draft):
        calls.append(draft.text)
        return Verdict("rewrite" if len(calls) == 1 else second, ("gag_daily_cap",))
    monkeypatch.setattr(live, "review", review)
    rig.script("the ferry leaves at seven", "bring a flask for the trip")
    sent, meta = speak(rig, turn())
    assert len(rig.chat_calls()) == 2
    assert "gag_daily_cap" in rig.last_user()
    assert sent  # advisory guards cannot suppress the answer owed to him
    assert meta.guard_rewritten


def test_signed_template_is_never_rewritten(rig, monkeypatch):
    monkeypatch.setattr(live, "review", lambda *a: Verdict("rewrite", ("gag_daily_cap",)))
    sent, _ = speak(rig, turn(template="approved exact message"), TurnMeta("signed", before_transport=lambda: True))
    assert sent == ["approved exact message"] and rig.chat_calls() == []


@pytest.mark.parametrize("channel", ["guild", "whisper", "discord:general", "dm:101"])
def test_react_uses_only_its_medium_and_no_game_clock_for_discord(rig, monkeypatch, channel):
    monkeypatch.setattr(live, "review", lambda *a: Verdict("react", ("filler_after_question",), reaction="👍"))
    t = Turn(TurnKind.OPEN,101,channel,to_guid=1 if channel in {"whisper","dm:101"} else 0)
    meta = TurnMeta("react")
    if is_discord(channel):
        parent = dstore.post(rig.store, channel=channel_id(channel), author=1, origin="human",
                            text="when is the ferry?", now=rig.clock.now())
        meta.discord = DiscordTarget(parent, None)
    before = rig.world.human_last_active
    sent, _ = speak(rig, t, meta)
    assert not sent
    emotes = [c for _, c in rig.sent.log if isinstance(c, Emote)]
    assert bool(emotes) is (channel == "guild")
    assert not rig.store.conn.execute("SELECT 1 FROM chat_line WHERE origin='mind'").fetchone()
    if is_discord(channel):
        assert dstore.reactions(rig.store.conn, [parent])[parent][0]["members"] == [101]
        assert rig.world.human_last_active == before and not rig.sent.log


def test_review_exception_fails_open_without_logging_content(rig, monkeypatch, caplog):
    def broken(*a):
        raise ValueError("PRIVATE_PROMPT")
    monkeypatch.setattr(live, "review", broken)
    with caplog.at_level(logging.WARNING):
        sent, meta = speak(rig, turn())
    assert sent and meta.guard_verdict == "failed_open"
    assert "guard_review_failed_open" in caplog.text and "PRIVATE_PROMPT" not in caplog.text


def test_corrupt_cached_vector_is_fail_open_and_cancellation_propagates(rig, monkeypatch):
    adapter = rig.engine.live_review
    adapter.config = replace(adapter.config, semantic_enabled=True)
    text = "a ferry is leaving tonight"
    adapter.vectors[hashlib.sha256(text.encode()).digest()] = b"bad"
    meta = TurnMeta("vector")
    verdict = rig.sync(adapter.check(turn(), meta, text))
    assert verdict.action == "send" and meta.guard_verdict == "failed_open"
    def cancelled(*a):
        raise asyncio.CancelledError()
    monkeypatch.setattr(live, "review", cancelled)
    adapter.config = GuardsCfg(enabled=True)
    with pytest.raises(asyncio.CancelledError):
        rig.sync(adapter.check(turn(), meta, text))


def test_disabled_guards_never_review_or_embed(rig, monkeypatch):
    rig.engine.live_review.config = GuardsCfg()
    def forbidden(*a):
        raise AssertionError("off path")
    monkeypatch.setattr(live, "review", forbidden)
    sent, meta = speak(rig, turn())
    assert sent and meta.guard_verdict == "not_run"


def test_reload_discards_vectors_from_the_previous_model(rig):
    rig.engine.live_review.vectors[b"fixture"] = b"old model vector"
    rig.engine.apply_config(rig.engine.cfg)
    assert not rig.engine.live_review.vectors


def test_confirmed_line_and_receipt_share_one_commit(rig):
    trace = []
    rig.store.conn.set_trace_callback(trace.append)
    try:
        sent, _ = speak(rig, turn())
    finally:
        rig.store.conn.set_trace_callback(None)
    assert sent
    at = next(i for i, sql in enumerate(trace) if sql.startswith("INSERT INTO chat_line"))
    receipt = next(i for i, sql in enumerate(trace) if sql.startswith("INSERT INTO meta(key,value)") and "guards:sent:" in sql)
    assert receipt > at and not any(sql == "COMMIT" for sql in trace[at:receipt])


def test_deleted_discord_parent_during_embedding_cannot_send(rig, monkeypatch):
    adapter = rig.engine.live_review
    adapter.config = replace(adapter.config, semantic_enabled=True)
    parent = dstore.post(rig.store, channel="general", author=1, origin="human", text="ferry?", now=rig.clock.now())
    async def embed(texts, priority):
        dstore.delete(rig.store, parent, author=1, now=rig.clock.now())
        return [pack_vec(unit(i)) for i, _ in enumerate(texts)]
    monkeypatch.setattr(rig.engine.recall._embedder, "embed", embed)
    sent, _ = speak(rig, turn("discord:general"), TurnMeta("deleted", discord=DiscordTarget(parent, None)))
    assert sent == [] and not rig.sent.log
    assert not rig.store.conn.execute("SELECT 1 FROM discord_message WHERE origin='mind'").fetchone()
