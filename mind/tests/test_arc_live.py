"""The arc in live talk (Task S8): her plan, the trouble at home, the arc's recent events, a
rival who has got closer to him and a cooling spark reach only her whispers to him; a summons
inside a plan he has talked about stands; and a woman away is never voiced. The test cast and
invented lines only."""
import random
import re

import pytest

from conftest import HUMAN, make_mem, make_persona, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.arc import live
from leeroy_mind.arc.live import (ARC_CAP_TOKENS, AWAY_CACHE_S, EVENT_RECENT_S, EVENT_TEXT,
                                  PARTNER_TEXT, RIVAL_LINE_LEVEL, ArcView, arc_view, away_now)
from leeroy_mind.arc.state import Arc, put_arc
from leeroy_mind.human import _FORMS
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.pipeline import SUMMONS_HINT
from leeroy_mind.talk.romance import COOLING_TEXT, REPROACH_RE, STAGE_PASSAGES, rival_text
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import estimate_tokens

HE = _FORMS["he"]
FILL = dict(name="Zan", they="he", them="him", their="his")
DAY = 86400.0
MARRIED = "married, two kids at home"
PLAN = {"kind": "neutral", "worries": ["money"], "cover": ""}


@pytest.fixture
def clock():
    return engine_clock()


def _woman(store, guid: int, stance: str = "drawn", **over) -> None:
    save_member(store, guid, player_gender="female", stance=stance, typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={}, **over)


def _cast(store, **over) -> None:
    """Belra (102) and Dunna (104) as women drawn to him, whose words arrive as written."""
    seed_test_cast(store)
    _woman(store, 102, **over)
    _woman(store, 104)


@pytest.fixture
def rig(store, clock, tmp_path):
    _cast(store)
    r = make_rig(store, clock, tmp_path)
    yield r
    r.close()


def _stage(store, guid: int, stage: str, attraction: float = 80.0) -> None:
    store.put_relationship(Relationship(a=guid, b=HUMAN, affinity=50.0, trust=50.0,
                                        attraction=attraction, romance_stage=stage))


def _plan(store, guid: int, state: str, *, now: float, stage: str = "planning",
          plan: dict | None = None, at: float = 0.0, **arc) -> None:
    """Her arc at `stage` with a plan in `state`, its target 12 days out."""
    _stage(store, guid, stage)
    plan = {**PLAN, **(plan or {}), "target": now + 12 * DAY}
    put_arc(store, Arc(guid, HUMAN, plan=plan, plan_state=state, plan_at=at or None,
                       planning_since=now - 20 * DAY, updated=now, **arc))


def _away(store, guid: int, *, left: float, back: float | None, returned=None) -> int:
    with store.transaction():
        return store.conn.execute(
            "INSERT INTO away(guid, reason, left_at, returns_at, returned_at) "
            "VALUES (?, 'weekend', ?, ?, ?)", (guid, left, back, returned)).lastrowid


def _rivalry(store, holder: int, target: int, level: float) -> None:
    with store.transaction():
        store.conn.execute("INSERT INTO jealousy(holder, target, kind, level, updated, evidence) "
                           "VALUES (?, ?, 'rivalry', ?, 0, '[]')", (holder, target, level))


def _night(store, guid: int, night: str, *, gained: float, decayed: float,
           neglected: int = 0) -> None:
    with store.transaction():
        store.conn.execute(
            "INSERT INTO attraction_log(night, guid, gained, decayed, neglected, hurt, "
            "half_life, before, after, stance_before, stance_after) "
            "VALUES (?, ?, ?, ?, ?, 0, 30, 70, 70, 'drawn', 'drawn')",
            (night, guid, gained, decayed, neglected))


def _view(store, p, *, now, channel="whisper", to_guid=HUMAN, rng=None) -> ArcView:
    return arc_view(store, p, HUMAN, now=now, channel=channel, to_guid=to_guid, forms=HE,
                    names={101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna",
                           105: "Ebbo", HUMAN: "Zan"}, rng=rng)


def _belra(**over):
    return make_persona(102, "Belra", player_gender="female", stance=over.pop("stance", "drawn"),
                        **over)


def _her_prompts(rig, guid: int) -> list[str]:
    name = {102: "Belra", 104: "Dunna"}[guid]
    return [t for t in rig.prompts() if f"You play {name}," in t]


def _whisper_prompt(rig, guid: int, text: str, reply: str = "fair enough") -> str:
    rig.script(reply)
    rig.whisper_from_him(guid, text)
    rig.run()
    return rig.last_user()


# -- away ---------------------------------------------------------------------------------------

def test_away_now_is_every_open_away_she_has_not_come_back_from(store, clock):
    now = clock.now()
    _away(store, 101, left=now - 9 * DAY, back=now - 7 * DAY, returned=now - 7 * DAY)
    _away(store, 103, left=now - DAY, back=now + DAY)                 # open, any owner
    assert away_now(store, now) == frozenset({103})
    with store.transaction():
        store.conn.execute("UPDATE away SET returns_at = ? WHERE guid = 103", (now - 60,))
    assert away_now(store, now) == frozenset()                        # past her return
    with store.transaction():
        store.conn.execute("UPDATE away SET returns_at = NULL WHERE guid = 103")
    assert away_now(store, now) == frozenset({103})                   # no return set yet


def test_an_away_woman_is_never_voiced(rig):
    now = rig.clock.now()
    _plan(rig.store, 102, "set", now=now)
    _away(rig.store, 102, left=now - 3600, back=now + DAY)
    assert 102 not in rig.engine._candidates()
    rig.whisper_from_him(102, "hey, you there?")
    rig.run()
    rig.line_from_him("anyone around tonight?")
    rig.run()
    assert all(u["guid"] != 102 for u in rig.utterances())
    assert _her_prompts(rig, 102) == []
    whisper = Turn(kind=TurnKind.WHISPER, speaker=102, channel="whisper", to_guid=HUMAN,
                   addressee=HUMAN)
    assert rig.speak_turn(whisper) == []                              # a turn made elsewhere
    assert rig.utterances()[-1]["dropped"] == "away" and _her_prompts(rig, 102) == []


def test_she_talks_again_once_she_is_back(rig):
    now = rig.clock.now()
    _away(rig.store, 102, left=now - 3600, back=now + 120)
    assert 102 not in rig.engine._candidates()
    rig.run(AWAY_CACHE_S + 120)                                       # her return, the cache
    assert 102 in rig.engine._candidates()


def test_the_away_list_is_read_at_most_once_a_minute(rig):
    assert 102 in rig.engine._candidates()
    now = rig.clock.now()
    _away(rig.store, 102, left=now - 3600, back=now + DAY)
    assert 102 in rig.engine._candidates()                            # the cached list
    rig.run(AWAY_CACHE_S)
    assert 102 not in rig.engine._candidates()


def test_two_women_planning_only_one_is_away(rig):
    """Two set plans, one gap of his: the meetup took the earlier one (S5), so she is away and
    the other answers his whisper with her own plan still ahead."""
    now = rig.clock.now()
    _plan(rig.store, 102, "set", now=now, at=now - 5 * DAY)
    _plan(rig.store, 104, "set", now=now, at=now - 2 * DAY, plan={"kind": "event"})
    _away(rig.store, 102, left=now - 3600, back=now + DAY)
    assert away_now(rig.store, now) == frozenset({102})
    rig.whisper_from_him(102, "safe travels")
    rig.run()
    user = _whisper_prompt(rig, 104, "you still up for it?", "can't wait")
    assert [s.guid for s in rig.sent.says()] == [104]
    assert "It's happening: BlizzCon, about 12 days off." in user
    assert _her_prompts(rig, 102) == []


# -- what reaches which turn --------------------------------------------------------------------

def test_plan_details_only_in_a_whisper_to_him(rig):
    now = rig.clock.now()
    _plan(rig.store, 102, "set", now=now, plan={"cover": "a work trip"})
    whisper = _whisper_prompt(rig, 102, "so are we still on?")
    assert "It's happening: a quiet weekend somewhere neutral, about 12 days off." in whisper
    assert "the cover story is a work trip" in whisper
    rig.run(300)                                                      # past her loop guards
    assert rig.speak_turn(Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild",
                               addressee=HUMAN))
    guild = rig.prompts()[-1]
    assert any(v.format(**FILL) in guild for v in STAGE_PASSAGES["planning"])  # the passage
    rig.run(300)
    assert rig.speak_turn(Turn(kind=TurnKind.WHISPER, speaker=102, channel="whisper",
                               to_guid=101, addressee=101))
    to_arkon = rig.prompts()[-1]
    for prompt in (guild, to_arkon):
        for words in ("It's happening", "somewhere neutral", "days off", "cover story",
                      "work trip"):
            assert words not in prompt, words


@pytest.mark.parametrize("part", ["plan", "home", "rival", "cooling", "events", "summons"])
def test_a_view_off_his_whisper_holds_nothing_private(store, clock, part):
    seed_test_cast(store)
    now = clock.now()
    _plan(store, 102, "talked" if part in ("plan", "summons") else "none", now=now,
          events=(("gift", now - DAY),) if part == "events" else ())
    if part == "rival":
        _rivalry(store, 102, 104, 80.0)
        _stage(store, 104, "voice")
    if part == "cooling":
        _night(store, 102, "2026-10-28", gained=0.0, decayed=4.0, neglected=1)
    p = _belra(household=MARRIED if part == "home" else "lives alone")
    private = _view(store, p, now=now)
    assert private != ArcView(stage="planning", plan_state=private.plan_state)
    for channel, to in (("guild", 0), ("whisper", 101), ("voice:raid", 0), ("officer", 0),
                        ("discord:general", 0), ("discord:officer-chat", 0)):
        assert _view(store, p, now=now, channel=channel, to_guid=to) == ArcView(
            stage="planning", plan_state=private.plan_state)
    assert _view(store, p, now=now, channel="dm:102") == private  # a DM to him: a whisper


def test_her_arc_lines_stay_within_the_cap_and_take_turns(store, clock):
    """Everything at once (a set plan with its cover story, her partner, a rival, cooling):
    each whisper holds where her plan stands and what fits beside it in ARC_CAP_TOKENS, and
    across whispers every line gets its turn."""
    seed_test_cast(store)
    now = clock.now()
    _plan(store, 102, "set", now=now, plan={"cover": "a weekend away with friends",
                                            "worries": ["time off", "the home front"]})
    _rivalry(store, 102, 104, 80.0)
    _stage(store, 104, "voice")
    _night(store, 102, "2026-10-28", gained=0.0, decayed=4.0, neglected=1)
    p = _belra(household=MARRIED)
    first = "It's happening: a quiet weekend somewhere neutral, about 12 days off."
    longest_cooling = max(estimate_tokens(v.format(**FILL)) for v in COOLING_TEXT)
    seen = set()
    for seed in range(40):
        view = _view(store, p, now=now, rng=random.Random(seed))
        assert view.arc_lines[0] == first
        used = (sum(estimate_tokens(x.format(**FILL)) for x in view.arc_lines)
                + (estimate_tokens(rival_text(view.rival, HE).format(**FILL)) if view.rival
                   else 0) + (longest_cooling if view.cooling else 0))
        assert used <= ARC_CAP_TOKENS
        seen |= {*view.arc_lines[1:], *(["rival"] if view.rival else []),
                 *(["cooling"] if view.cooling else [])}
    assert seen == {"On your mind: time off, the home front; the cover story is a weekend away "
                    "with friends.", PARTNER_TEXT["together"], "rival", "cooling"}
    plain = _view(store, p, now=now)            # no rng: the plan's lines, then home
    assert plain.arc_lines[0] == first and not plain.rival


def test_a_view_draws_nothing_with_one_line_to_place(store, clock):
    """The engine's shared generator is drawn from only when there is an order to draw."""
    seed_test_cast(store)
    now = clock.now()
    _plan(store, 102, "talked", now=now)
    rng = random.Random(3)
    state = rng.getstate()
    assert _view(store, _belra(), now=now, rng=rng).arc_lines
    assert rng.getstate() == state


def test_his_weekend_memory_and_hers_both_reach_her_whisper_only(rig):
    """Her private weekend moment (S5) and what he told her about it in a whisper (a memory
    extracted from his line): both in her whisper to him, neither in her guild reply."""
    now = rig.clock.now()
    _plan(rig.store, 102, "done", now=now, stage="after", weekends=1, last_meetup=now - 2 * DAY)
    away = _away(rig.store, 102, left=now - 5 * DAY, back=now - 3 * DAY, returned=now - 3 * DAY)
    his = rig.store.insert_chat_line(
        Chat(ts=0, channel="whisper", from_guid=HUMAN, from_name="Testmage", to_guid=102,
             text="the train home felt far too short", map=0, zone=10),
        session_id=None, origin="human")
    t = int(now - 3 * DAY)
    rig.store.insert_memory(make_mem(
        owner=102, kind="event", source="offscreen", importance=8, actors=(HUMAN,),
        text="We got lost looking for the hotel and laughed about it all evening.",
        t_event=t, t_created=t, t_accessed=t, evidence=(f"away:{away}",), scope="private",
        scope_with=HUMAN))
    rig.store.insert_memory(make_mem(
        owner=102, kind="observation", source="chat", importance=6, actors=(HUMAN,),
        text="Zan said the train home after our weekend felt far too short.",
        t_event=t + 60, t_created=t + 60, t_accessed=t + 60, evidence=(f"chat:{his}",),
        scope="private", scope_with=HUMAN))
    whisper = _whisper_prompt(rig, 102, "still thinking about the weekend")
    assert "got lost looking for the hotel" in whisper
    assert "train home after our weekend" in whisper
    rig.run(300)
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild",
                               addressee=HUMAN))
    guild = rig.prompts()[-1]
    assert "got lost looking for the hotel" not in guild and "train home" not in guild


def test_the_rival_line_names_the_rival_never_him(rig):
    now = rig.clock.now()
    _plan(rig.store, 102, "talked", now=now)
    _stage(rig.store, 104, "voice")                                   # noticed by the guild
    _rivalry(rig.store, 102, 104, 55.0)
    user = _whisper_prompt(rig, 102, "hey you")
    line = rival_text("Dunna", HE).format(**FILL)
    assert line in user
    assert "Dunna" in line and not REPROACH_RE.search(line)
    assert line.endswith("never him.")
    rig.run(300)
    assert rig.speak_turn(Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild",
                               addressee=HUMAN))
    assert "Dunna has been getting closer" not in rig.prompts()[-1]


@pytest.mark.parametrize("level, stage, named", [
    (RIVAL_LINE_LEVEL, "voice", "Dunna"),
    (RIVAL_LINE_LEVEL - 1, "voice", ""),           # not jealous enough
    (90.0, "whispers", ""),                         # a quiet stage nobody can tell
    (90.0, "after", "Dunna"),
    (90.0, "ex", ""),
])
def test_the_rival_is_jealous_enough_of_a_noticed_stage(store, clock, level, stage, named):
    seed_test_cast(store)
    _stage(store, 104, stage)
    _rivalry(store, 102, 104, level)
    assert _view(store, _belra(), now=clock.now()).rival == named


def test_the_rival_is_the_one_she_minds_most(store, clock):
    seed_test_cast(store)
    _stage(store, 103, "planning")
    _stage(store, 104, "voice")
    _stage(store, 105, "banter")
    _rivalry(store, 102, 103, 50.0)
    _rivalry(store, 102, 104, 70.0)
    _rivalry(store, 102, 105, 95.0)                 # the strongest, at a quiet stage
    with store.transaction():
        store.conn.execute("INSERT INTO jealousy(holder, target, kind, level, updated, evidence) "
                           "VALUES (102, 1, 'envy', 99, 0, '[]')")
    assert _view(store, _belra(), now=clock.now()).rival == "Dunna"


# -- her plan, home and cooling -----------------------------------------------------------------

@pytest.mark.parametrize("stage, state, lines", [
    ("planning", "hoped", ["A quiet weekend somewhere neutral is a hope you've floated; you "
                           "leave it with him."]),
    ("planning", "talked", ["You two have talked about a quiet weekend somewhere neutral, but "
                            "nothing is settled."]),
    ("planning", "set", ["It's happening: a quiet weekend somewhere neutral, about 12 days off.",
                         "On your mind: money."]),
    ("after", "set", ["It's happening: a quiet weekend somewhere neutral, about 12 days off.",
                      "On your mind: money."]),   # a visit
    ("planning", "none", []),
    ("planning", "done", []),
    ("weekend", "set", []),
    ("ex", "set", []),
])
def test_her_plan_lines_follow_its_state(store, clock, stage, state, lines):
    seed_test_cast(store)
    now = clock.now()
    _plan(store, 102, state, now=now, stage=stage)
    assert list(_view(store, _belra(), now=now).arc_lines) == lines


def test_a_fond_woman_has_no_plan_lines(store, clock):
    seed_test_cast(store)
    now = clock.now()
    _plan(store, 102, "set", now=now)
    view = _view(store, _belra(stance="fond"), now=now)
    assert view.arc_lines == () and not view.summons_ok


@pytest.mark.parametrize("stage, household, partner, line", [
    ("late_nights", MARRIED, "none", PARTNER_TEXT["together"]),
    ("after", "lives with her boyfriend", "none", PARTNER_TEXT["together"]),
    ("confession", MARRIED, "separated", PARTNER_TEXT["separated"]),
    ("whispers", MARRIED, "none", ""),              # too early for the trouble at home
    ("planning", "lives alone", "none", ""),
    ("ex", MARRIED, "none", ""),
])
def test_the_partner_line(store, clock, stage, household, partner, line):
    seed_test_cast(store)
    now = clock.now()
    _plan(store, 102, "none", now=now, stage=stage, partner=partner)
    lines = _view(store, _belra(household=household), now=now).arc_lines
    assert list(lines) == ([line] if line else [])


def test_recent_arc_events_are_scene_items_newest_first(store, clock):
    seed_test_cast(store)
    now = clock.now()
    events = (("missing", now - EVENT_RECENT_S - 60), ("inside_joke", now - 4 * DAY),
              ("separated", now - 3 * DAY), ("gift", now - DAY))
    _plan(store, 102, "none", now=now, stage="after", events=events)
    view = _view(store, _belra(), now=now)
    assert view.scene == (EVENT_TEXT["gift"], EVENT_TEXT["inside_joke"])
    assert "separated" not in EVENT_TEXT and "found_out" not in EVENT_TEXT  # the partner line's


def test_events_join_the_scene_after_the_notes(rig):
    now = rig.clock.now()
    _plan(rig.store, 102, "none", now=now, stage="after",
          events=(("inside_joke", now - DAY), ("gift", now - 2 * DAY)))
    rig.engine.notes.add(102, HUMAN, "her_tease", "you teased him", channel="whisper",
                         to_guid=HUMAN, ts=now)
    user = _whisper_prompt(rig, 102, "hey")
    joke, gift = (EVENT_TEXT[k].format(**FILL) for k in ("inside_joke", "gift"))
    assert f"Between you two lately: you teased him; {joke}; {gift}." in user


@pytest.mark.parametrize("rows, cooling", [
    ([], False),
    ([("2026-10-28", 3.0, 1.0, 0)], False),
    ([("2026-10-28", 1.0, 3.0, 0)], True),
    ([("2026-10-28", 3.0, 1.0, 1)], True),          # neglected
    ([("2026-10-27", 1.0, 3.0, 1), ("2026-10-28", 3.0, 1.0, 0)], False),  # her last night counts
])
def test_cooling_reads_her_last_attraction_night(store, clock, rows, cooling):
    seed_test_cast(store)
    for night, gained, decayed, neglected in rows:
        _night(store, 102, night, gained=gained, decayed=decayed, neglected=neglected)
    _night(store, 104, "2026-10-28", gained=0.0, decayed=9.0, neglected=1)  # not hers
    assert _view(store, _belra(), now=clock.now()).cooling is cooling


def test_cooling_reaches_her_whisper(rig):
    _night(rig.store, 102, "2026-10-28", gained=0.0, decayed=4.0, neglected=1)
    user = _whisper_prompt(rig, 102, "hey")
    assert any(v.format(**FILL) in user for v in COOLING_TEXT)


def test_an_ex_has_no_arc_in_her_whispers(store, clock):
    seed_test_cast(store)
    now = clock.now()
    _plan(store, 102, "set", now=now, stage="ex", events=(("gift", now - DAY),))
    _rivalry(store, 102, 104, 90.0)
    _stage(store, 104, "voice")
    _night(store, 102, "2026-10-28", gained=0.0, decayed=4.0, neglected=1)
    assert _view(store, _belra(household=MARRIED), now=now) == ArcView(stage="ex",
                                                                       plan_state="set")


# -- the summons --------------------------------------------------------------------------------

def test_a_summons_inside_a_set_plan_is_kept(rig):
    _plan(rig.store, 102, "set", now=rig.clock.now())
    rig.script("come over already, i can't wait")
    rig.whisper_from_him(102, "counting the days")
    rig.run()
    assert [s.text for s in rig.sent.says()] == ["come over already, i can't wait"]
    assert len(rig.chat_calls()) == 1


def test_a_summons_inside_a_talked_plan_is_kept(rig):
    _plan(rig.store, 102, "talked", now=rig.clock.now())
    rig.script("get over here already")
    rig.whisper_from_him(102, "we should figure out dates")
    rig.run()
    assert [s.text for s in rig.sent.says()] == ["get over here already"]


@pytest.mark.parametrize("state, stage, stance", [
    ("hoped", "planning", "drawn"),                 # only a hope: he has not engaged
    ("none", "planning", "drawn"),
    ("set", "after", "drawn"),                      # a visit's plan: only the planning stage
    ("set", "planning", "fond"),                    # never past FULL_ARC (carry from S5)
])
def test_a_summons_outside_a_plan_he_shaped_is_asked_again_then_dropped(
        store, clock, tmp_path, state, stage, stance):
    _cast(store, stance=stance)
    r = make_rig(store, clock, tmp_path)
    try:
        _plan(store, 102, state, now=clock.now(), stage=stage)
        r.script("get over here", "come over already")
        r.whisper_from_him(102, "hi")
        r.run()
        assert r.utterances()[-1]["dropped"] == "tells" and len(r.chat_calls()) == 2
        assert SUMMONS_HINT.format(**FILL) in r.last_user()
        assert not r.sent.says()
    finally:
        r.close()


def test_a_summons_in_her_guild_line_inside_a_set_plan_is_still_asked_again(rig):
    _plan(rig.store, 102, "set", now=rig.clock.now())
    rig.script("get over here", "come over already")
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild",
                               addressee=HUMAN)) == []
    assert rig.utterances()[-1]["dropped"] == "tells"


class _Comms:
    """A voice side that speaks every voice-channel turn and never reaches the service, so a
    voiced line falls back to typed chat (Pipeline._speak_now)."""

    def place(self, turn, meta):
        return turn.channel, turn.to_guid, turn.addressee

    def voiced(self, turn, meta):
        return turn.channel.startswith("voice:")

    async def hold(self, turn, channel):
        return None

    def deliver(self, turn, meta, text):
        return False

    def available(self):
        return True

    def hearable(self, guid):
        return True

    def unheard(self, channel, meta):
        return False


@pytest.mark.parametrize("reply, sent", [("get over here", False), ("lol true", True)])
def test_a_voiced_summons_that_falls_back_to_typed_chat_with_him_is_dropped(rig, reply, sent):
    """In a voice room a summons may stand (the two are together); typed in guild it may not
    (A5's ruling, carried to S8)."""
    rig.engine.comms = _Comms()
    rig.script(reply)
    said = rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="voice:raid",
                               addressee=HUMAN))
    assert len(rig.chat_calls()) == 1
    if sent:
        assert said == [reply] and rig.sent.says()[-1].channel == "guild"
    else:
        assert said == [] and not rig.sent.says()
        assert rig.utterances()[-1]["dropped"] == "summons"


# -- the texts ----------------------------------------------------------------------------------

def test_live_arc_texts_stay_short_and_never_name_a_place():
    filled = [t.format(**FILL) for t in (*PARTNER_TEXT.values(), *EVENT_TEXT.values())]
    assert all(len(t) <= 80 for t in (e.format(**FILL) for e in EVENT_TEXT.values()))
    assert all(len(t) <= 100 for t in filled)
    capitals = {w for t in filled for w in re.findall(r"(?<![.:;] )(?<!^)\b[A-Z]\w+", t)}
    assert capitals <= {"Zan"}


def test_the_live_view_logs_nothing(store, clock, caplog):
    """A turn's view writes no log line: no plan, cover story or name reaches the log."""
    seed_test_cast(store)
    now = clock.now()
    _plan(store, 102, "set", now=now, plan={"cover": "a work trip"})
    with caplog.at_level("DEBUG", logger="leeroy_mind"):
        _view(store, _belra(household=MARRIED), now=now)
        away_now(store, now)
    assert caplog.records == []


def test_the_arc_imports_nothing_from_the_pipeline():
    assert "talk.pipeline" not in open(live.__file__, encoding="utf-8").read()


def test_his_emote_at_an_away_woman_gets_nothing_back(rig, caplog):
    """An away woman sends no emote back either (talk.emotes._emote_blocked)."""
    from leeroy_mind.protocol import Emote, TextEmote
    now = rig.clock.now()
    _away(rig.store, 102, left=now - 3600, back=now + DAY)

    async def main():
        for target in (102, 104):
            ev = TextEmote(ts=0, guid=HUMAN, emote="wave", target_guid=target, via_bridge=False)
            rig.world.apply(ev, rig.clock.now())
            await rig.engine.on_text_emote(ev, rig.clock.now())
        await rig.advance(12, 0.25)
    with caplog.at_level("INFO", logger="leeroy_mind"):
        rig.sync(main())
    assert [c.guid for _, c in rig.sent.log if isinstance(c, Emote)] == [104]
    assert "emote_skipped: emote=wave guid=102 reason=away" in caplog.text


def _dm(rig, kind: TurnKind = TurnKind.FLIRT) -> Turn:
    """Her line to him in her Discord DM (a FLIRT always carries her romance text)."""
    return Turn(kind=kind, speaker=102, channel="dm:102", to_guid=HUMAN, addressee=HUMAN)


def test_her_discord_dm_to_him_carries_her_arc_as_a_whisper_does(rig):
    """A Discord DM to him is private exactly as a whisper is: her plan's lines reach its prompt;
    a Discord channel's prompt gets the public stage passage and none of them."""
    now = rig.clock.now()
    _plan(rig.store, 102, "set", now=now, plan={"cover": "a work trip"})
    rig.script("cant wait")
    assert rig.speak_turn(_dm(rig))
    dm = rig.prompts()[-1]
    assert "It's happening: a quiet weekend somewhere neutral, about 12 days off." in dm
    assert "the cover story is a work trip" in dm
    rig.run(300)
    assert rig.speak_turn(Turn(kind=TurnKind.FLIRT, speaker=102, channel="discord:general",
                               addressee=HUMAN))
    general = rig.prompts()[-1]
    assert any(v.format(**FILL) in general for v in STAGE_PASSAGES["planning"])
    for words in ("It's happening", "somewhere neutral", "days off", "cover story", "work trip"):
        assert words not in general, words


def test_a_summons_in_her_dm_inside_a_set_plan_is_kept(rig):
    """As in her whisper: inside a plan he said yes to, asking him over stands in her DM."""
    _plan(rig.store, 102, "set", now=rig.clock.now())
    rig.script("come over already, i can't wait")
    assert rig.speak_turn(_dm(rig, TurnKind.REPLY_HUMAN)) == ["come over already, i can't wait"]
    assert len(rig.chat_calls()) == 1
