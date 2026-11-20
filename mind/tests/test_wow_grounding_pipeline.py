"""WoW grounding in the live prompt ([talk] wow_grounding): a raid or guild turn carries the card,
her example lines and the glossary after the awareness card; a whisper's prompt is byte for byte
what it was; the example lines shown reach the filters and the guards; the awareness card leaves
out what the card says; a line of his that names a boss or a raid brings its facts; the trim drops
the grounding before memories and chat. Invented cast and rows only."""
import asyncio
import json
from dataclasses import replace

import pytest

from conftest import HUMAN, run
from engine_rig import FakeLibrary, chunk, engine_clock, make_rig, numbered
from leeroy_mind.config import load_config
from leeroy_mind.talk import grounding as g
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.prompt import PromptParts, render_messages, trim_to_budget
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import estimate_tokens


@pytest.fixture
def clock():
    return engine_clock()


def turn(guid=102, channel="guild", kind=TurnKind.OPEN):
    return Turn(kind=kind, speaker=guid, channel=channel,
                to_guid=HUMAN if channel == "whisper" else 0,
                addressee=HUMAN if channel == "whisper" or kind is TurnKind.REPLY_HUMAN else None)


def prompt(r, t, meta=None):
    c = run(r.engine._compose(t, meta or TurnMeta("t"), live=False))
    return "\n\n".join(m["content"] for m in c.messages), c


def switch(r, **talk):
    r.engine.cfg = replace(r.engine.cfg, talk=replace(r.engine.cfg.talk, **talk))


def example(store, clock, text, *, guid=102, channel="guild", days=2):
    with store.transaction():
        store.conn.execute(
            "INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin) "
            "VALUES (?,?,?,?,0,?,'mind')", (clock.now() - days * 86400, channel, guid, "Belra", text))


def raid_rows(store, clock, *, ended=None):
    now = clock.now()
    with store.transaction():
        store.conn.execute(
            "INSERT INTO guild_raid(started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,"
            "loot_guid,kills,wipes) VALUES (?,?,'naxx',25,0,0,104,101,104,4,1)", (now - 3600, ended))
        store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason) VALUES (?,102,30,'kill')",
                           (now - 60,))


def without(text: str, sections) -> str:
    for s in sections:
        text = text.replace("\n\n" + s, "", 1)
    return text


def test_a_guild_turn_carries_the_card_her_examples_and_the_glossary(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path, script=numbered(), wow_grounding=True)
    example(store, clock, "pulling the next pack stay behind me")
    text, c = prompt(r, turn())
    assert g.CARD_HEAD + "\n- You: Druid, tank." in text
    assert g.EXEMPLAR_HEAD + "\n- pulling the next pack stay behind me" in text
    assert g.GLOSSARY in text
    assert text.index(g.CARD_HEAD) < text.index(g.EXEMPLAR_HEAD) < text.index(g.GLOSSARY)
    assert "pulling the next pack stay behind me" in c.samples  # never said back word for word
    switch(r, wow_grounding=False)
    off, _ = prompt(r, turn())
    assert g.CARD_HEAD not in off and g.GLOSSARY not in off
    on_sections = [g.CARD_HEAD + "\n- You: Druid, tank.",
                   g.EXEMPLAR_HEAD + "\n- pulling the next pack stay behind me", g.GLOSSARY]
    assert without(text, on_sections) == off


@pytest.mark.parametrize("channel", ["whisper", "say"])
def test_a_whisper_or_say_prompt_is_byte_identical(store, clock, tmp_path, channel):
    r = make_rig(store, clock, tmp_path, script=numbered(), wow_grounding=True)
    example(store, clock, "pulling the next pack stay behind me")
    raid_rows(store, clock)
    kind = TurnKind.REPLY_HUMAN if channel == "say" else TurnKind.OPEN
    on, c = prompt(r, turn(channel=channel, kind=kind))
    switch(r, wow_grounding=False)
    off, _ = prompt(r, turn(channel=channel, kind=kind))
    assert on == off and g.CARD_HEAD not in on
    assert "pulling the next pack stay behind me" not in c.samples


def test_the_examples_shown_reach_the_guards(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path, script=numbered(), wow_grounding=True)
    example(store, clock, "pulling the next pack stay behind me")
    meta = TurnMeta("t")
    t = turn()
    prompt(r, t, meta)
    assert meta.wow_exemplars == ("pulling the next pack stay behind me",)
    assert meta.wow_grounded is True
    assert r.engine.live_review.draft(t, meta, "fine").exemplars == meta.wow_exemplars


def test_the_raid_on_the_card_and_out_of_the_awareness_card(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path, script=numbered(), wow_grounding=True)
    raid_rows(store, clock)
    switch(r, aware=True)
    text, c = prompt(r, turn())
    assert "Raid now: Naxxramas 25, 4 kills, 1 wipe so far." in text
    assert "Your DKP: 30, 1st of 1." in text
    assert "Guild raid:" not in text and "DKP: 30; rank" not in text
    assert "Raid now: Naxxramas 25, 4 kills, 1 wipe so far." in c.support  # it may back her words
    switch(r, wow_grounding=False)
    off, _ = prompt(r, turn())
    assert "Guild raid:" in off and "DKP: 30; rank 1" in off and g.CARD_HEAD not in off


def test_his_line_naming_a_raid_brings_its_facts(store, clock, tmp_path):
    fact = ("Icecrown Citadel is the raid at the top of the glacier, for ten or twenty-five "
            "players, with twelve bosses in four wings.")
    lib = FakeLibrary(named=[chunk("Icecrown Citadel", fact, kind="zone")])

    async def go(on):
        r = make_rig(store, clock, tmp_path, script=numbered(), library=lib, wow_grounding=on)
        await r.session()
        await r.human("icc tonight, bring flasks")
        await r.advance(60, step=2)
        return r.prompts()
    on = run(go(True))
    assert any("Facts you know" in p and "top of the glacier" in p for p in on), [
        p[p.find("Facts"):][:400] for p in on]
    assert not lib.searches  # titles only, never the semantic search or a model


def test_his_line_naming_nothing_brings_no_facts(store, clock, tmp_path):
    lib = FakeLibrary(named=[chunk("Icecrown Citadel", "A raid at the top of the glacier, "
                                   "for ten or twenty-five players, with twelve bosses.",
                                   kind="zone")])

    async def go():
        r = make_rig(store, clock, tmp_path, script=numbered(), library=lib, wow_grounding=True)
        await r.session()
        await r.human("lol that was fun")
        await r.advance(60, step=2)
        return r.prompts()
    assert not any("glacier" in p for p in run(go())) and not lib.titled_calls


def test_a_failed_read_leaves_the_turn_without_grounding(store, clock, tmp_path, caplog,
                                                        monkeypatch):
    r = make_rig(store, clock, tmp_path, script=numbered(), wow_grounding=True)

    def broken(*args, **kw):
        raise OSError("disk")
    monkeypatch.setattr(g, "build", broken)
    text, _ = prompt(r, turn())
    assert g.CARD_HEAD not in text
    assert "wow_grounding_failed guid=102" in caplog.text


def test_the_pool_is_kept_a_while(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path, script=numbered(), wow_grounding=True)
    example(store, clock, "pulling the next pack stay behind me")
    prompt(r, turn())
    example(store, clock, "a newer example that waits for the next read", days=1)
    text, _ = prompt(r, turn())
    assert "a newer example" not in text
    r.engine._wow_pools.clear()
    text, _ = prompt(r, turn())
    assert "a newer example that waits for the next read" in text


# -- the trim ------------------------------------------------------------------------------------

def parts(**kw):
    base = dict(card="card", samples=[], rules="rules", rel_cards=[], memories=[], mood="",
                chat=[], own_recent=[], instruction="say it")
    base.update(kw)
    return PromptParts(**base)


def test_the_trim_drops_the_grounding_from_its_end_before_memories():
    wow = ["CARD " + "c" * 200, "EXAMPLES " + "e" * 200, "GLOSSARY " + "g" * 200]
    p = parts(memories=[f"- memory {i} " + "m" * 100 for i in range(5)], wow=wow)
    full = sum(estimate_tokens(m["content"]) for m in render_messages(p))
    t = trim_to_budget(p, budget=full - 30)
    assert t.wow == wow[:2] and len(t.memories) == 5
    t = trim_to_budget(p, budget=full - 80)
    assert t.wow == wow[:1] and len(t.memories) == 5
    t = trim_to_budget(p, budget=full - 130)
    assert t.wow == [] and len(t.memories) == 5


def test_render_puts_the_grounding_after_awareness_before_the_instruction():
    text = render_messages(parts(awareness="AWARE", wow=["CARD", "GLOSS"], voice="VOICE"))[1]["content"]
    assert text.index("AWARE") < text.index("CARD") < text.index("GLOSS") < text.index("VOICE") \
        < text.index("say it")


# -- the switch ----------------------------------------------------------------------------------

def test_the_switch_is_on_by_default_and_anything_but_true_is_off(tmp_path):
    from leeroy_mind.config import EXEMPLARS_BEFORE, exemplars_time
    assert load_config(tmp_path / "absent.toml").talk.wow_grounding is True
    for value, want in (("false", False), ("true", True), ('"yes"', False), ("1", False)):
        path = tmp_path / f"m{value.strip(chr(34))}.toml"
        path.write_text(f"[talk]\nwow_grounding = {value}\n", encoding="utf-8")
        assert load_config(path).talk.wow_grounding is want
    path = tmp_path / "before.toml"
    path.write_text('[talk]\nwow_exemplars_before = "yesterday"\n', encoding="utf-8")
    assert load_config(path).talk.wow_exemplars_before == EXEMPLARS_BEFORE
    path.write_text('[talk]\nwow_exemplars_before = "2026-11-01T00:00:00+00:00"\n',
                    encoding="utf-8")
    assert exemplars_time(load_config(path).talk.wow_exemplars_before) == 1793491200.0
    assert exemplars_time("2026-11-01T00:00:00") is None  # no offset: refused
