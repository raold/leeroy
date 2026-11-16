"""What she wants (talk/intent.py; human-likeness spec section 2, core plan Task 4): one want enum
shared with think-dm's beat (talk/beat.py), its seeded weighted draw, the code-built topics, and the
one block every channel renders through (beat.render). Nothing calls the draw on a live turn yet.
The test cast and invented lines only."""
import random
from dataclasses import replace
from enum import StrEnum
from typing import NamedTuple

import pytest

from conftest import seed_test_cast
from leeroy_mind.talk import beat
from leeroy_mind.talk.intent import (INTENT_KINDS, Intent, IntentContext, allowed, draw_intent,
                                     topic_for, want_words, weights)
from leeroy_mind.talk.turns import TurnKind

# Lane A's types (plan Tasks 1 and 3) may reach main after this lane: the real ones once they are
# there, else stand-ins with their values. talk/intent.py reads them by value, never imports them.
try:
    from leeroy_mind.life.day import Block, DayEvent
except ImportError:
    class DayEvent(StrEnum):
        NONE = "none"
        LATE_SHIFT = "late_shift"
        SICK_KID = "sick_kid"
        NIGHT_OUT = "night_out"
        BAD_DAY = "bad_day"

    class Block(StrEnum):
        ASLEEP = "asleep"
        WORK = "work"
        COMMUTE = "commute"
        HOME_DUTY = "home_duty"
        GYM = "gym"
        OUT = "out"
        FREE = "free"
        GAMING = "gaming"
try:
    from leeroy_mind.life.state import State
except ImportError:
    class State(NamedTuple):
        valence: float
        energy: float
        stress: float
        social: float
        toward_him: float

BASE = IntentContext(kind=TurnKind.REPLY_HUMAN, to_him=True, owed=True, his_line="how was work?",
                     family="whisper", female=True, stance="lust", style="bold", attachment="secure",
                     humour="dry", agreeableness=6, extraversion=6, tail=False, attention=None,
                     state=None, event=DayEvent.NONE, block=None, kids=False, last=None)

# think-dm's words for "You want to:" as merged in #329: the beat's lines must not move a byte
BEAT_WANTS_329 = {
    "vent": "get something off your chest",
    "share": "share something of your own",
    "ask": "get something from {human}",
    "tease": "tease {human}",
    "flirt": "flirt with {human}",
    "reassure_seek": "hear that things are good between you",
    "gossip": "talk about someone else",
    "deflect": "steer away from it",
    "small_talk": "keep it light",
    "react": "just react",
    "boundary": "draw a line",
    "logistics": "sort out the plan",
}


def test_one_enum_with_the_beat():
    assert [i.value for i in Intent] == list(beat.WANTS)
    assert all(beat.WANTS[i.value].format(human="Zan") == want_words(i, "Zan") for i in Intent)


def test_the_beats_words_and_choices_are_unchanged():
    assert beat.WANTS == BEAT_WANTS_329
    assert set(beat.MEANING) == set(beat.WANTS)
    assert beat.wants_for(True) == tuple(BEAT_WANTS_329)
    assert beat.wants_for(False) == tuple(w for w in BEAT_WANTS_329 if w != "flirt")
    assert all(type(w) is str for w in beat.wants_for(True))  # the schema's enum, as before


def test_render_generalises_the_beats_lines():
    b = beat.Beat("he's teasing", "warm", "tease", None)
    assert beat.lines(b, "Zan") == beat.render(read=b.read, lands=b.lands, want=Intent.TEASE, who="Zan")
    assert beat.render(lands="it stings", want=Intent.VENT, who="Zan", topic="about a rough day") == \
        "It lands: it stings\nYou want to: get something off your chest (about a rough day)"
    assert beat.render() == ""


def test_render_keeps_the_beats_order_and_bring_in():
    b = beat.Beat("he asks if you're on tonight", "a little thrill", "ask", "the raid on Thursday")
    assert beat.lines(b, "Zan") == (
        "You read it as: he asks if you're on tonight\nIt lands: a little thrill\n"
        "You want to: get something from Zan\nBring in: the raid on Thursday")
    assert beat.render(want=Intent.ASK, who="everyone") == \
        "You want to: get something from everyone"
    assert beat.render(read="r", use="") == "You read it as: r"


def test_owed_never_just_reacts():   # Review Focus 4
    assert weights(BASE)[Intent.REACT] == 0 and Intent.REACT not in allowed(BASE)


def test_owed_never_just_reacts_anywhere():   # Review Focus 4, every eligible kind and family
    for kind in INTENT_KINDS:
        for family in ("game", "whisper", "discord", "dm", "group"):
            for line in ("", "hey", "lol nice", "what time is raid?", "you look cute ;)"):
                ctx = replace(BASE, kind=kind, family=family, his_line=line,
                              state=State(-0.5, -0.8, 0.9, -0.9, -0.9))
                assert weights(ctx)[Intent.REACT] == 0, (kind, family, line)
                assert all(draw_intent(ctx, random.Random(i)) is not Intent.REACT
                           for i in range(40))


def test_overlay_tail_keeps_his_pace():
    w = weights(replace(BASE, tail=True))
    assert all(w[i] == 0 for i in (Intent.VENT, Intent.DEFLECT, Intent.BOUNDARY, Intent.GOSSIP,
                                   Intent.LOGISTICS, Intent.SMALL_TALK))
    assert set(allowed(replace(BASE, tail=True))) == {
        Intent.SHARE, Intent.ASK, Intent.TEASE, Intent.FLIRT, Intent.REASSURE_SEEK}


def test_no_flirt_with_members_or_from_men():
    assert weights(replace(BASE, to_him=False, kind=TurnKind.CONTINUE))[Intent.FLIRT] == 0
    assert Intent.FLIRT not in allowed(replace(BASE, female=False))
    assert weights(replace(BASE, family="group"))[Intent.REASSURE_SEEK] == 0


def test_flirt_and_reassurance_by_stance_and_attachment():
    line = replace(BASE, owed=False, his_line="you look cute tonight ;)")   # his flirt
    flirt = lambda stance: weights(replace(line, stance=stance))[Intent.FLIRT]  # noqa: E731
    assert flirt("lust") == 4 and flirt("drawn") == pytest.approx(4 * 0.7)
    assert flirt("fond") == pytest.approx(4 * 0.3) and flirt("none") == 0
    assert weights(replace(line, stance="none"))[Intent.REASSURE_SEEK] == 0
    assert weights(replace(line, female=False))[Intent.REASSURE_SEEK] == 0
    assert weights(replace(line, attachment="anxious"))[Intent.REASSURE_SEEK] == 2 * 1
    assert weights(replace(line, family="group"))[Intent.FLIRT] == 0


def test_rows_by_his_line():
    plain = replace(BASE, owed=False)
    row = lambda line: [weights(replace(plain, his_line=line))[i] for i in Intent]  # noqa: E731
    assert row("hey all") == [1, 2, 0.5, 1, 1, 0.3, 0.3, 0.2, 3, 2, 0, 0.5]
    assert row("that boss was rough") == [1, 2, 1, 2, 1, 0.3, 0.5, 0.5, 1, 3, 0.3, 0.5]
    assert row("how was work?") == [0.7, 3, 1, 1.5, 1, 0.3, 0.3, 0.7, 0.5, 1, 0.3, 0.5]
    assert row("miss you, cutie ;)") == [0.3, 1, 0.5, 2, 4, 1, 0, 1, 0.5, 1, 0.5, 0]
    hers = replace(plain, kind=TurnKind.WHISPER, his_line="")
    assert [weights(hers)[i] for i in Intent] == [1, 2, 1, 1.5, 4, 1, 0, 0, 1, 0, 0, 0]
    assert weights(hers) == weights(replace(hers, kind=TurnKind.FLIRT))


def test_owed_question_holds_back_the_put_offs():
    plain, owed = replace(BASE, owed=False), BASE
    for i, factor in ((Intent.DEFLECT, 0.3), (Intent.BOUNDARY, 0.3), (Intent.VENT, 0.5)):
        assert weights(owed)[i] == pytest.approx(weights(plain)[i] * factor)
    remark = replace(BASE, his_line="that boss was rough")
    assert weights(remark)[Intent.DEFLECT] == weights(replace(remark, owed=False))[Intent.DEFLECT]


def test_bad_day_and_stress_raise_venting():
    calm = weights(BASE)[Intent.VENT]
    hard = weights(replace(BASE, event=DayEvent.BAD_DAY, state=State(0, 0, 0.5, 0, 0)))[Intent.VENT]
    assert hard == calm * 2 * 2
    assert topic_for(Intent.VENT, replace(BASE, event=DayEvent.BAD_DAY)) == "about a rough day"


def test_her_day_her_mood_and_her_ways():
    plain = replace(BASE, owed=False, his_line="that boss was rough")
    w0 = weights(plain)

    def ratio(i, **kw):
        return weights(replace(plain, **kw))[i] / w0[i]
    assert ratio(Intent.VENT, block=Block.WORK) == pytest.approx(1.5)
    assert ratio(Intent.VENT, event=DayEvent.SICK_KID) == pytest.approx(2)
    assert ratio(Intent.SHARE, event=DayEvent.NIGHT_OUT) == pytest.approx(1.5)
    assert ratio(Intent.SHARE, state=State(0.5, 0, 0, 0, 0)) == pytest.approx(1.5)
    assert ratio(Intent.TEASE, humour="teasing") == pytest.approx(1.5)
    assert ratio(Intent.TEASE, agreeableness=4) == pytest.approx(1.5)
    assert ratio(Intent.TEASE, state=State(0, 0, 0, 0, -0.3)) == 0
    assert ratio(Intent.BOUNDARY, state=State(0, 0, 0.5, 0, 0)) == pytest.approx(1.5)
    assert ratio(Intent.BOUNDARY, state=State(0, 0, 0, 0, -0.1)) == pytest.approx(1.5)
    assert ratio(Intent.DEFLECT, state=State(0, 0, 0, 0, -0.5)) == pytest.approx(1.5)
    assert ratio(Intent.DEFLECT, style="shy") == pytest.approx(1.5)
    assert ratio(Intent.DEFLECT, style="guarded") == pytest.approx(1.5)
    tired = State(0, -0.3, 0, 0, 0)
    assert ratio(Intent.SMALL_TALK, state=tired) == ratio(Intent.REACT, state=tired) == 2
    shy = State(0, 0, 0, -0.3, 0)
    assert ratio(Intent.REACT, state=shy) == ratio(Intent.BOUNDARY, state=shy) == pytest.approx(1.5)
    assert ratio(Intent.SHARE, state=shy) == ratio(Intent.ASK, state=shy) == pytest.approx(0.6)
    assert ratio(Intent.TEASE, last=Intent.TEASE) == pytest.approx(0.5)


def test_his_plans_and_times_ask_for_logistics():
    plain = replace(BASE, owed=False, his_line="that boss was rough")
    base = weights(plain)[Intent.LOGISTICS]
    for line in ("raid at 8pm?", "icc at 20:30 tonight", "anyone up for icc later?"):
        assert weights(replace(plain, his_line=line))[Intent.LOGISTICS] == pytest.approx(base * 3)


def test_gossip_stays_out_of_private_talk_not_to_him():
    other = replace(BASE, kind=TurnKind.CONTINUE, to_him=False, owed=False, his_line="")
    assert weights(replace(other, family="dm"))[Intent.GOSSIP] == 0
    assert weights(replace(other, family="whisper"))[Intent.GOSSIP] == 0
    assert weights(replace(other, family="discord"))[Intent.GOSSIP] == 1
    assert weights(replace(BASE, family="dm"))[Intent.GOSSIP] > 0


def test_opener_wants_and_no_topic_of_its_own():
    opener = replace(BASE, kind=TurnKind.OPEN, to_him=False, owed=False, his_line="",
                     family="discord")
    assert allowed(opener) == (Intent.VENT, Intent.SHARE, Intent.ASK, Intent.GOSSIP,
                               Intent.SMALL_TALK)
    assert topic_for(Intent.VENT, replace(opener, event=DayEvent.BAD_DAY)) == ""
    assert topic_for(Intent.SHARE, opener) == ""


def test_topics_are_code_built():
    def t(i, **kw):
        return topic_for(i, replace(BASE, **kw))
    assert t(Intent.VENT, event=DayEvent.SICK_KID) == "about your kid being ill"
    assert t(Intent.VENT, event=DayEvent.LATE_SHIFT) == "about being stuck late at work"
    assert t(Intent.VENT, block=Block.WORK) == t(Intent.VENT, block=Block.COMMUTE) == "about work"
    assert t(Intent.VENT, kids=True) == "about the kids"
    assert t(Intent.VENT, block=Block.FREE) == "about your day"
    assert t(Intent.SHARE, event=DayEvent.NIGHT_OUT) == "your night out"
    assert t(Intent.SHARE) == "something from your day"
    assert t(Intent.GOSSIP) == "only from what you remember"
    assert t(Intent.LOGISTICS) == "raid times, gear or who's coming"
    assert all(t(i, event=DayEvent.BAD_DAY, kids=True) == "" for i in Intent
               if i not in (Intent.VENT, Intent.SHARE, Intent.GOSSIP, Intent.LOGISTICS))
    assert want_words(Intent.VENT, "Zan", t(Intent.VENT, kids=True)) == \
        "get something off your chest (about the kids)"


def test_only_eligible_kinds_want_anything():
    for kind in TurnKind:
        ctx = replace(BASE, kind=kind, owed=False)
        if kind in INTENT_KINDS:
            assert allowed(ctx)
        else:
            assert allowed(ctx) == () and draw_intent(ctx, random.Random(1)) is None


def test_draw_is_seeded():
    a = [draw_intent(BASE, random.Random(i)) for i in range(50)]
    assert a == [draw_intent(BASE, random.Random(i)) for i in range(50)]


def test_draw_follows_the_weights():
    w = weights(BASE)
    draws = [draw_intent(BASE, random.Random(i)) for i in range(4000)]
    assert set(draws) <= set(allowed(BASE))
    total = sum(w.values())
    for i in allowed(BASE):
        assert draws.count(i) / len(draws) == pytest.approx(w[i] / total, abs=0.03)


def test_life_intents_prints_codes_only(tmp_path, store, capsys):
    from leeroy_mind.__main__ import main
    from leeroy_mind.life.cli import life_main
    seed_test_cast(store)
    before = store.conn.execute("SELECT count(*) FROM meta").fetchone()[0]
    assert main(["life", "intents", "--data-dir", str(tmp_path), "--guid", "101",
                 "--line", "how was the warehouse today?"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "101 kind=reply_human family=whisper owed=yes samples=500"
    shares = dict(line.split() for line in lines[1:])
    assert list(shares) == [i.value for i in Intent]
    assert shares["flirt"] == "0.0%" and shares["react"] == "0.0%"   # a man; an owed answer
    assert sum(float(v.rstrip("%")) for v in shares.values()) == pytest.approx(100, abs=0.05)
    assert "Arkon" not in out and "warehouse" not in out
    assert store.conn.execute("SELECT count(*) FROM meta").fetchone()[0] == before
    assert life_main(["intents", "--data-dir", str(tmp_path), "--guid", "101",
                      "--kind", "greet"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "none 100.0%"
    assert life_main(["intents", "--data-dir", str(tmp_path), "--guid", "999"]) == 1
    assert "no persona for guid 999" in capsys.readouterr().err
    assert life_main(["intents", "--data-dir", str(tmp_path / "empty"), "--guid", "101"]) == 2
