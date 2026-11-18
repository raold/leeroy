"""[talk] coherence, coalescing and brb: a waiting or late answer covers every unanswered line
of his (his question first), in a whisper (talk.engine._life_waiting, TurnMeta.coalesced) and
in his DM (his posts since her last message); his leaving words cut her away at once; her
brb's reason is in her next prompt in that conversation. Fakes only; invented lines."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from conftest import HUMAN
from engine_rig import engine_clock, numbered
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.life.interrupt import Away
from leeroy_mind.life.presence import AWAY_NOTE_S, away_note, game_cap_s
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.prompt import unanswered_text
from leeroy_mind.talk.select import classify_human_line, is_leaving
from leeroy_mind.talk.turns import Turn, TurnKind
from life.test_interrupt import force, line_calls, rig, said, ticking, whispers

NIGHT_S = 7200.0  # 22:00 UK: the test cast (early shifts) is asleep, so answers wait


@pytest.fixture
def clock():
    return engine_clock()


@pytest.fixture(autouse=True)
def _brb_any_time(monkeypatch):
    """As tests/life/test_interrupt.py: her brb may follow his line at once here."""
    from leeroy_mind.life import interrupt
    monkeypatch.setattr(interrupt, "MIN_AFTER_HIS_S", 0.0)


def last_user(r):
    return line_calls(r)[-1]["messages"][-1]["content"]


def test_leaving_words_are_read_beyond_a_goodbye():
    for line in ("ok im out, kid needs picking up", "alright gotta run", "im off, cya",
                 "heading to bed", "gn", "ttyl"):
        assert is_leaving(line), line
    for line in ("brb", "are you heading out?", "the tank is out of mana", "nice one", ""):
        assert not is_leaving(line), line
    assert classify_human_line("ok im out, kid needs picking up") == "remark"  # no goodnights


def test_the_instruction_quotes_every_waiting_line(store, clock, tmp_path):
    r = rig(store, clock, tmp_path)
    try:
        human = r.engine.human
        text = unanswered_text(["what trinkets do you run for tanking", "?"], human)
        assert text == ('He wrote 2 lines you haven\'t answered yet: "what trinkets do you run '
                        'for tanking" / "?". Answer every one of them in this reply, his '
                        "question first.")
        assert unanswered_text(["nice", "so tired"], human).endswith("the oldest first.")
        assert unanswered_text(["only one"], human) == "" and unanswered_text([], human) == ""
    finally:
        r.close()


def test_a_coalesced_whisper_answers_the_question_not_only_the_newest(store, clock, tmp_path,
                                                                      caplog):
    caplog.set_level(logging.INFO)

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("the stone and the badge, {n}"),
                intent=True, shape=True)
        await r.session()
        await r.advance(NIGHT_S, step=60)
        t0 = clock.now()
        first = await r.human("what trinkets do you run for tanking these days",
                              channel="whisper", to_guid=102)
        await r.advance(20, step=1)
        second = await r.human("?", channel="whisper", to_guid=102)
        assert f"reply_coalesced: line={second.id} guid=102" in caplog.text
        await ticking(r, game_cap_s(6) + 60)
        assert len(whispers(r, 102, t0)) == 1  # one answer for both lines
        user = last_user(r)
        assert ('He wrote 2 lines you haven\'t answered yet: "what trinkets do you run for '
                'tanking these days" / "?". Answer every one of them in this reply, his question '
                "first.") in user
        # the turn is weighed as an answer to his question: answer-first, with room for it
        assert "Answer his question first, plainly" in user
        assert "about 12 words" in user or "only now seeing these" in user
        assert first.id != second.id and "trinkets" not in caplog.text
        r.close()
    asyncio.run(main())


def test_coherence_off_answers_the_newest_as_before(store, clock, tmp_path):
    async def main():
        r = rig(store, clock, tmp_path, script=numbered("the stone and the badge, {n}"),
                intent=True, shape=True, coherence=False)
        await r.session()
        await r.advance(NIGHT_S, step=60)
        await r.human("what trinkets do you run for tanking these days", channel="whisper",
                      to_guid=102)
        await r.advance(20, step=1)
        await r.human("?", channel="whisper", to_guid=102)
        await ticking(r, game_cap_s(6) + 60)
        assert "you haven't answered yet" not in last_user(r)
        r.close()
    asyncio.run(main())


@pytest.mark.parametrize("coherence", [True, False])
def test_his_leaving_words_cut_her_away(store, clock, tmp_path, monkeypatch, coherence):
    async def main():
        force(monkeypatch, away_s=300.0)
        r = rig(store, clock, tmp_path, script=numbered("yeah {n}, the vendor was shut"),
                shape=True, coherence=coherence)
        await r.session()
        await r.human("you around tonight?", channel="whisper", to_guid=102)
        await ticking(r, 60)
        until = r.engine.life.away_until(102)
        assert until > clock.now() + 200
        t1 = clock.now()
        await r.human("so what happened with the vendor", channel="whisper", to_guid=102)
        await ticking(r, 20)
        assert whispers(r, 102, t1) == []
        await r.human("ok im out, kid needs picking up", channel="whisper", to_guid=102)
        await ticking(r, 25)
        said = whispers(r, 102, t1)
        r.close()
        return said, until
    said, until = asyncio.run(main())
    if coherence:
        assert len(said) == 1 and said[0][0] < until  # at once, not when her brb ends
    else:
        assert said == []  # as before: it waits out her away


def whisper_turn(r, text="who was at the door"):
    row = said(r, text)
    turn = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", addressee=HUMAN, to_guid=HUMAN,
                trigger_line=row.id)
    meta = TurnMeta(r.engine._new_id(), answers_human=True)
    r.engine._draw_situation(turn, meta, r.engine.personas.get(102), r.clock.now())
    return r.sync(r.engine._compose(turn, meta, live=True)).messages[-1]["content"]


def test_her_brb_reason_is_in_her_next_prompt(store, clock, tmp_path):
    r = rig(store, clock, tmp_path, situation=True)
    try:
        now = r.clock.now()
        note = away_note("door", "him")
        assert note == ("A few minutes ago you told him you had to step away: someone was at "
                        "the door. You're back now.")
        assert note not in whisper_turn(r)
        r.engine.life._away[102] = Away(since=now - 200, until=now - 10, channel="whisper",
                                        reason="door", night="n", minute=0, done=True)
        assert note in whisper_turn(r)
        # another conversation, or long after, says nothing of it
        r.engine.life._away[102] = Away(since=now - 200, until=now - 10, channel="dm:102",
                                        reason="door", night="n", minute=0, done=True)
        assert note not in whisper_turn(r)
        r.engine.life._away[102] = Away(since=now - AWAY_NOTE_S - 400,
                                        until=now - AWAY_NOTE_S - 100, channel="whisper",
                                        reason="door", night="n", minute=0, done=True)
        assert note not in whisper_turn(r)
        assert away_note("nonsense", "him") == ""
    finally:
        r.close()


def test_a_dm_is_weighed_against_his_unanswered_question_and_lists_nothing_here(store, clock,
                                                                                  tmp_path):
    """DMs are discord.talk's (the DM memory work): this side adds no list of its own, and only
    reads DiscordTalk.unanswered, when it is there, to weigh the turn against his question."""
    r = rig(store, clock, tmp_path, situation=True, intent=True, shape=True)
    try:
        now = r.clock.now()
        dstore.post(r.store, channel="dm:102", author=HUMAN, origin="human",
                    text="what time does the depot shut on sundays", now=now - 60)
        mid = dstore.post(r.store, channel="dm:102", author=HUMAN, origin="human", text="?",
                          now=now)
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN)

        def meta_():
            return TurnMeta(r.engine._new_id(), answers_human=True, why="discord:reply",
                            discord=DiscordTarget(reply_to=mid, thread=None), discord_chat=())
        assert r.engine._his_line(turn, meta_()) == "?"  # no reader yet: his newest post
        posts = [(mid - 1, "what time does the depot shut on sundays"), (mid, "?")]
        talk = SimpleNamespace(unanswered=lambda conn, channel, thread, message: (
            posts if (channel, thread, message) == ("dm:102", None, mid) else []))
        r.engine.owed = SimpleNamespace(app=SimpleNamespace(discord_talk=talk))
        meta = meta_()
        assert r.engine._his_line(turn, meta) == posts[0][1]  # his question leads the weighing
        assert r.engine._his_waiting(turn, meta) == []
        r.engine._draw_situation(turn, meta, r.engine.personas.get(102), r.clock.now())
        user = r.sync(r.engine._compose(turn, meta, live=True)).messages[-1]["content"]
        assert "you haven't answered yet" not in user  # never a second list
    finally:
        r.close()
