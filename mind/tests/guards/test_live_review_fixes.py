"""Independent review regressions, using the invented cast and fake model."""
import asyncio
from dataclasses import replace
import threading

import pytest

from .test_live import rig,turn,speak,source
from leeroy_mind.config import load_config
from leeroy_mind.guards import Verdict
from leeroy_mind.protocol import TextEmote,Zone
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.talk.turns import Turn
from leeroy_mind.text import estimate_tokens
from leeroy_mind.talk.prompt import LIVE_BUDGET


def test_deterministic_announcements_never_enter_guard_or_rewrite(rig,monkeypatch):
    def forbidden(*args):
        pytest.fail("deterministic award entered model guards")
    monkeypatch.setattr(live,"review",forbidden)
    for _ in range(4):
        sent,meta = speak(rig,turn("raid",template="+10 dkp to 25 for invented boss"),
                          TurnMeta("award",before_transport=lambda:True))
        assert sent == ["+10 dkp to 25 for invented boss"]
    assert not rig.chat_calls()


def test_repeated_say_as_callouts_do_not_spend_guard_rations(rig,monkeypatch):
    def forbidden(*args):
        pytest.fail("template reviewed by model guards")
    monkeypatch.setattr(live,"review",forbidden)
    async def go():
        for text in ["drink up, pulling when everyone is full"]*2+["+10 dkp to 25 for invented boss"]*4:
            task = asyncio.create_task(rig.engine.say_as(101,"raid",text,kind=TurnKind.CALLOUT))
            await rig.advance(100)
            assert task.done() and task.result()
    rig.sync(go())
    assert not rig.chat_calls()
    assert not rig.store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'guards:sent:%'").fetchone()


@pytest.mark.parametrize("channel",["guild","whisper","dm:101","say"])
@pytest.mark.parametrize("action",["drop","react"])
def test_owed_answer_survives_advisory_verdict(rig,monkeypatch,channel,action):
    monkeypatch.setattr(live,"review",lambda *args:Verdict(action,("own_repeat_same_channel",)))
    rig.script("the ferry leaves at seven","bring a flask for the trip")
    sent,meta = speak(rig,turn(channel),TurnMeta("owed",why="reply:named",answers_human=True,
                                              whisper_reply=channel=="whisper"))
    assert sent
    assert not rig.sent.emotes()
    if action == "drop":
        assert len(rig.chat_calls()) == 2 and meta.guard_rewritten


@pytest.mark.parametrize("channel",["guild","officer","party","raid","raid_warning","say","yell","whisper","discord:general","dm:101"])
@pytest.mark.parametrize("emote",["wave","kiss"])
def test_text_emote_card_has_original_channel_privacy(rig,channel,emote):
    e = rig.engine
    e.cfg = replace(e.cfg,talk=replace(e.cfg.talk,aware=True))
    rig.store.insert_game_event(TextEmote(0,1,emote,101),session_id=None)
    block = e.live_review.block(turn(channel),TurnMeta("test"),close=False)
    allowed = channel=="whisper" or channel=="say" and emote=="wave"
    assert (f"Emote {emote}" in block) is allowed
    if channel.startswith(("discord:","dm:")):
        assert "In game:" not in block and "His raid:" not in block


def test_card_precedes_instruction_and_fits_prompt_budget(rig):
    e = rig.engine
    e.cfg = replace(e.cfg,talk=replace(e.cfg.talk,aware=True))
    messages,_ = rig.sync(e.compose(turn()))
    user = messages[-1]["content"]
    assert user.index(live.END) < user.index("Answer")
    assert sum(estimate_tokens(m["content"]) for m in messages) <= LIVE_BUDGET


def test_card_uses_people_and_place_names_and_relative_time(rig):
    e = rig.engine
    e.cfg = replace(e.cfg,talk=replace(e.cfg.talk,aware=True))
    rig.store.insert_game_event(Zone(0,101,10,"Invented Meadow"),session_id=None)
    rig.store.insert_game_event(TextEmote(0,1,"wave",101),session_id=None,ts=rig.clock.now()-180)
    block = e.live_review.block(turn("say"),TurnMeta("test"),close=False)
    assert "Channel: say" in block and "Invented Meadow" in block
    assert "#101" not in block and "#1" not in block and "1.793" not in block
    assert "3 min ago" in block and e.human.called in block


def test_card_failure_cannot_silence_turn_or_log_exception_text(rig,monkeypatch,caplog):
    e = rig.engine
    e.cfg = replace(e.cfg,talk=replace(e.cfg.talk,aware=True))
    def broken(*args,**kwargs):
        raise ValueError("PRIVATE_CARD_DETAILS")
    monkeypatch.setattr(e.live_review,"block",broken)
    sent,_ = speak(rig,turn())
    assert sent and "awareness_card_failed" in caplog.text
    assert "PRIVATE_CARD_DETAILS" not in caplog.text


def test_other_talk_settings_do_not_turn_awareness_off(tmp_path):
    path = tmp_path/"mind.toml"
    path.write_text("[talk]\naware=true\nrealism=true\n")
    assert load_config(path).talk.aware


def test_claims_only_embeds_draft_not_chat_context(rig,monkeypatch):
    from conftest import unit
    from leeroy_mind.storage.rows import pack_vec
    e = rig.engine
    e.live_review.claims_enabled = True
    source(rig,"earlier invented discussion")
    seen = []
    async def embed(texts,*args):
        seen.extend(texts)
        return [pack_vec(unit(0)) for _ in texts]
    monkeypatch.setattr(e.recall._embedder,"embed",embed)
    rig.sync(e.live_review.check(turn(),TurnMeta("test"),"new invented reply"))
    assert seen == ["new invented reply"]


def test_live_review_database_work_leaves_event_loop(rig,monkeypatch):
    owner = threading.get_ident()
    seen = []
    def review(*args):
        seen.append(threading.get_ident())
        return Verdict("send")
    monkeypatch.setattr(live,"review",review)
    rig.sync(rig.engine.live_review.check(turn(),TurnMeta("test"),"invented reply"))
    assert seen and all(ident!=owner for ident in seen)


@pytest.mark.parametrize("phase",["delay","hold","typing"])
def test_member_lock_is_free_during_waits(rig,monkeypatch,phase):
    e = rig.engine
    async def go():
        reached,release = asyncio.Event(),asyncio.Event()
        original_sleep = e.clock.sleep
        async def pause(seconds):
            if phase=="delay" and seconds==17 or phase=="typing" and seconds>0:
                reached.set()
                await release.wait()
            else:
                await original_sleep(seconds)
        async def hold(t):
            reached.set()
            await release.wait()
        monkeypatch.setattr(e.clock,"sleep",pause)
        if phase=="hold":
            monkeypatch.setattr(e,"_route",lambda *args:True)
            monkeypatch.setattr(e,"_hold",hold)
        task = asyncio.create_task(e._speak(turn(delay=17 if phase=="delay" else 0),TurnMeta("wait")))
        try:
            await asyncio.wait_for(reached.wait(),3)
            lock = e.live_review.lock(101)
            assert not lock.locked()
            await asyncio.wait_for(lock.acquire(),.2)
            lock.release()
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    rig.sync(go())


def test_guard_drop_receipt_has_codes_and_memory_ids_without_draft(rig,monkeypatch,caplog):
    from leeroy_mind.talk.style import Styled
    e = rig.engine
    monkeypatch.setattr(live,"review",lambda *args:Verdict("drop",("own_repeat_same_channel",)))
    t = Turn(TurnKind.OPEN,101,"guild",deadline=rig.clock.now()+60)
    meta = TurnMeta("private-drop",memory_ids=[71,83])
    with caplog.at_level("INFO",logger="leeroy_mind.talk"):
        assert rig.sync(e._guarded_lines(t,meta,e.personas.get(101),[Styled("PRIVATE_DRAFT","PRIVATE_DRAFT")],None,close=False)) == []
    row = rig.store.conn.execute("SELECT text,memory_ids,guard_verdict,guard_rewritten FROM utterance WHERE turn_id=?",(meta.turn_id,)).fetchone()
    assert tuple(row)==("","[71, 83]","drop",0)
    assert "own_repeat_same_channel" in caplog.text and "71" in caplog.text and "83" in caplog.text
    assert "PRIVATE_DRAFT" not in caplog.text


def test_multiline_drop_keeps_failed_verdict_when_last_line_passes(rig,monkeypatch):
    from leeroy_mind.talk.style import Styled
    def review(stores,draft):
        return Verdict("drop",("own_repeat_same_channel",)) if draft.text=="repeated draft" else Verdict("send")
    monkeypatch.setattr(live,"review",review)
    e = rig.engine
    t = Turn(TurnKind.OPEN,101,"guild",deadline=rig.clock.now()+60)
    meta = TurnMeta("multi-drop")
    lines = [Styled(text,text) for text in ("repeated draft","another thought")]
    assert rig.sync(e._guarded_lines(t,meta,e.personas.get(101),lines,None,close=False))==[]
    assert rig.store.conn.execute("SELECT guard_verdict FROM utterance WHERE turn_id=?",(meta.turn_id,)).fetchone()[0]=="drop"


def test_successful_rewrite_persists_verdict_and_flag(rig,monkeypatch):
    calls = []
    def review(*args):
        calls.append(1)
        return Verdict("rewrite",("gag_daily_cap",)) if len(calls)==1 else Verdict("send")
    monkeypatch.setattr(live,"review",review)
    rig.script("the ferry leaves at seven","bring a flask for the trip")
    sent,meta = speak(rig,turn())
    assert sent
    assert tuple(rig.store.conn.execute("SELECT guard_verdict,guard_rewritten FROM utterance WHERE turn_id=?",(meta.turn_id,)).fetchone()) == ("rewrite",1)


@pytest.mark.parametrize("channel",["discord:general","dm:101"])
def test_discord_guard_does_not_load_or_check_game_facts(rig,monkeypatch,channel):
    def forbidden(*args):
        pytest.fail("Discord consulted game world facts")
    monkeypatch.setattr(live,"load_facts",forbidden)
    from leeroy_mind.eval.talk import unawareness
    monkeypatch.setattr(unawareness,"check_line",forbidden)
    rig.engine.live_review.facts = object()
    result = rig.sync(rig.engine.live_review.check(turn(channel),TurnMeta("discord"),"a different invented thought"))
    assert result.action=="send"


@pytest.mark.parametrize("channel",["say","yell"])
def test_nearby_card_uses_channel_and_zone_audience(rig,monkeypatch,channel):
    e = rig.engine
    e.cfg = replace(e.cfg,talk=replace(e.cfg.talk,aware=True))
    original = e.world.audience
    calls = []
    def audience(*args,**kwargs):
        calls.append(kwargs)
        return original(*args,**kwargs)
    monkeypatch.setattr(e.world,"audience",audience)
    block = e.live_review.block(turn(channel),TurnMeta("zone"),close=False)
    assert f"Channel: {channel}" in block
    assert any(c.get("zone")==e.world.members[101].zone for c in calls)


def test_context_read_is_bounded_and_indexed(rig):
    old = source(rig,"OLD_CONTEXT")
    fresh = source(rig,"NEW_CONTEXT")
    with rig.store.transaction():
        rig.store.conn.execute("UPDATE chat_line SET ts=? WHERE id=?",(rig.clock.now()-86400,old))
    history,_ = rig.engine.live_review.history(turn(),TurnMeta("index"))
    assert [h.id for h in history]==[fresh]
    query = "SELECT * FROM chat_line WHERE channel=? AND ts BETWEEN ? AND ? ORDER BY ts DESC,id DESC LIMIT 256"
    plan = str([tuple(r) for r in rig.store.conn.execute("EXPLAIN QUERY PLAN "+query,("guild",0,rig.clock.now()))])
    assert "chat_line_channel_ts" in plan and "SCAN chat_line" not in plan


def test_invalid_awareness_logs_only_key_not_value(tmp_path,caplog):
    path = tmp_path/"mind.toml"
    path.write_text('[talk]\naware="PRIVATE_VALUE"\n')
    assert not load_config(path).talk.aware
    assert "talk_aware_invalid key=aware" in caplog.text and "PRIVATE_VALUE" not in caplog.text


def test_invalid_guard_setting_logs_only_key(caplog):
    from leeroy_mind.guards.config import parse_config
    assert not parse_config({"enabled":"PRIVATE_VALUE"}).enabled
    assert "guards_invalid keys=enabled" in caplog.text and "PRIVATE_VALUE" not in caplog.text


def test_over_budget_card_is_removed_before_conversation(rig,monkeypatch):
    e = rig.engine
    e.cfg = replace(e.cfg,talk=replace(e.cfg.talk,aware=True))
    monkeypatch.setattr(e.live_review,"block",lambda *args,**kwargs:"OVERSIZED_CARD "*5000)
    messages,_ = rig.sync(e.compose(turn()))
    assert "OVERSIZED_CARD" not in str(messages)
    assert "Answer" in messages[-1]["content"]
    assert sum(estimate_tokens(m["content"]) for m in messages) <= LIVE_BUDGET


def test_hard_screened_retry_does_not_displace_safe_owed_answer(rig,monkeypatch):
    monkeypatch.setattr(live,"review",lambda *args:Verdict("drop",("own_repeat_same_channel",)))
    rig.script("the ferry leaves at seven","/invite arkon")
    sent,meta = speak(rig,turn(),TurnMeta("owed",why="reply:named",answers_human=True))
    assert sent==["the ferry leaves at seven"] and meta.guard_rewritten


@pytest.mark.parametrize("question,emoji",[("funny story","😄"),("when is the ferry?","👍")])
def test_reaction_mapping_distinguishes_laugh_from_acknowledgement(rig,question,emoji):
    from leeroy_mind.guards import Draft,GuardStores,review
    parent = source(rig,question)
    result = review(GuardStores(rig.store.conn,rig.engine.live_review.config),
                    Draft(101,"guild","lol",parent_id=parent,now=rig.clock.now(),real_answer_due=True))
    assert result.action=="react" and result.reaction==emoji
