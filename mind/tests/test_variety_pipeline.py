"""Topic memory in the live pipeline (conversation variety, task A5): each turn's moves are
stored, the next prompt to the same person names them ("Tonight you've already..."), a repeated
move or a summons is asked again with a hint, and a whisper's moves stay in that whisper. The
test cast and invented lines only."""
import asyncio
import logging

import pytest

from conftest import HUMAN, make_mem, seed_test_cast
from engine_rig import RAID, engine_clock, make_rig, save_member
from leeroy_mind.arc.live import ARC_CAP_TOKENS, PARTNER_TEXT
from leeroy_mind.arc.state import Arc, put_arc
from leeroy_mind.protocol import Group
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.filters import Tell
from leeroy_mind.talk.moves import MOVE_PHRASES, MOVES
from leeroy_mind.talk.pipeline import MOVE_HINT, REGEN_HINT, REPEAT_HINT, SUMMONS_HINT
from leeroy_mind.talk.prompt import LIVE_BUDGET
from leeroy_mind.talk.romance import (COOLING_TEXT, NOTE_TEXT, OVERLAY_CAP_TOKENS, SCENE_HEAD,
                                      STAGE_PASSAGES, rival_text)
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import estimate_tokens

HE = dict(they="he", them="him", their="his")
# one invented line per move class, whose main move is that class
LINES = {
    "declare": "i want you",
    "compliment": "you look great",
    "tease": "bet you can't keep up",
    "question": "how was work?",
    "callback": "like you promised",
    "plan": "we should meet up someday",
    "complaint": "ugh my boss is the worst",
    "joke": "lol true",
    "later": "see you after raid",
    "petname": "ok handsome",
    "raid": "the boss fight was rough",
    "gear": "need better gear honestly",
    "gift": "mailed you something",
}


@pytest.fixture
def clock():
    return engine_clock()


def _cast(store, **over) -> None:
    """Belra (102) as a woman drawn to him, whose words arrive as written."""
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance=over.pop("stance", "drawn"),
                typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0, abbreviations={}, **over)


@pytest.fixture
def rig(store, clock, tmp_path):
    _cast(store)
    r = make_rig(store, clock, tmp_path)
    yield r
    r.close()


def _moves(store) -> list[tuple]:
    return [tuple(r) for r in store.conn.execute(
        "SELECT guid, counterpart, channel, to_guid, main FROM talk_move ORDER BY id")]


def test_every_move_class_has_a_line():
    assert set(LINES) == set(MOVES) - {"summon", "invite_game"}


@pytest.mark.parametrize("move", [m for m in MOVES if m not in ("summon", "invite_game")])
def test_each_move_class_puts_the_lately_line_in_the_next_prompt(rig, move):
    """A summons is not among them: it is asked again and never said (its own test)."""
    rig.script(LINES[move], "fair enough")
    rig.whisper_from_him(102, "hey")
    rig.run()
    assert _moves(rig.store) == [(102, HUMAN, "whisper", HUMAN, move)]
    rig.whisper_from_him(102, "what's up")
    rig.run()
    user = rig.last_user()
    assert f"Tonight you've already {MOVE_PHRASES[move].format(**HE)}" in user
    assert LINES[move] not in user.split("Tonight you've already")[1]  # moves, never her words


def test_a_repeated_summons_is_asked_again_then_dropped(rig):
    rig.script("get over here", "come over already")
    rig.whisper_from_him(102, "hi")
    rig.run()
    assert rig.utterances()[-1]["dropped"] == "tells"
    assert len(rig.chat_calls()) == 2
    assert SUMMONS_HINT.format(**HE) in rig.last_user()
    assert not rig.sent.says() and _moves(rig.store) == []


def test_a_summons_in_a_typed_guild_reply_to_him_is_asked_again_then_dropped(rig):
    """A guild line addressed to him is a line to him: the summons check applies there too."""
    rig.script("get over here", "come over already")
    to_him = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN)
    assert rig.speak_turn(to_him) == []
    assert rig.utterances()[-1]["dropped"] == "tells" and len(rig.chat_calls()) == 2
    assert SUMMONS_HINT.format(**HE) in rig.last_user()
    assert not rig.sent.says() and _moves(rig.store) == []


def test_a_refused_send_records_no_move(store, clock, tmp_path):
    """Only a line the server took is a move: a refused one leaves no row and no note."""
    _cast(store)
    r = make_rig(store, clock, tmp_path, ack=lambda cmd: (False, "refused"))
    try:
        r.script("bet you can't keep up")
        r.whisper_from_him(102, "how was your day?")
        r.run()
        assert r.sent.says() and r.utterances()[-1]["dropped"] == "refused"
        assert _moves(store) == []
        assert store.conn.execute("SELECT COUNT(*) FROM pair_note").fetchone()[0] == 0
    finally:
        r.close()


def test_a_whisper_to_another_member_stays_in_that_whisper(rig):
    """Her whisper to Arkon names itself neither in her guild line to him nor in her turns
    with Zan."""
    to_arkon = Turn(kind=TurnKind.WHISPER, speaker=102, channel="whisper", to_guid=101,
                    addressee=101)
    rig.script("bet you can't keep up", "fair enough")
    assert rig.speak_turn(to_arkon)
    assert _moves(rig.store) == [(102, 101, "whisper", 101, "tease")]
    rig.run(60)
    assert rig.speak_turn(to_arkon)  # the same whisper sees it
    assert "Tonight you've already teased them" in rig.last_user()
    rig.run(60)
    assert rig.speak_turn(Turn(kind=TurnKind.CONTINUE, speaker=102, channel="guild",
                               addressee=101))
    assert "you've already" not in rig.last_user()
    rig.whisper_from_him(102, "what's up")
    rig.run()
    assert "teased" not in rig.last_user()


def test_a_turn_reads_her_moves_once(rig, monkeypatch):
    """The prompt's Lately line and the repeat check use the same rows (Composed.moves)."""
    calls = []
    recent = rig.engine.moves.recent

    def counted(*a, **kw):
        calls.append(a)
        return recent(*a, **kw)
    monkeypatch.setattr(rig.engine.moves, "recent", counted)
    rig.script("bet you can't keep up", "fair enough")
    rig.whisper_from_him(102, "hey")
    rig.run()
    assert len(calls) == 1


def test_a_raid_leads_unaddressed_call_to_come_over_is_said(rig):
    """The summons check is only for her lines to him: a call to the raid is no summons."""
    rig.sync(rig.event(Group(ts=0, leader_guid=102, raid=True, members=RAID)))
    assert rig.raid.lead() == 102
    rig.script("get over here, stack on me")
    assert rig.speak_turn(Turn(kind=TurnKind.OPEN, speaker=102, channel="raid"))
    assert len(rig.chat_calls()) == 1 and rig.utterances()[-1]["dropped"] is None
    assert SUMMONS_HINT.split(":")[0] not in rig.last_user()


def test_a_guild_line_asking_another_member_over_is_said(rig):
    rig.script("come over here and look at this")
    to_arkon = Turn(kind=TurnKind.CONTINUE, speaker=102, channel="guild", addressee=101)
    assert rig.speak_turn(to_arkon) == ["come over here and look at this"]
    assert len(rig.chat_calls()) == 1


def test_a_summons_answered_in_game_is_said(rig):
    rig.script("get over here", "meet me in dalaran")
    rig.whisper_from_him(102, "hi")
    rig.run()
    assert [s.text for s in rig.sent.says()] == ["meet me in dalaran"]
    assert _moves(rig.store)[-1][-1] == "invite_game"


def test_a_move_repeated_in_new_words_is_asked_again(rig):
    rig.script("bet you can't keep up", "bet you won't even try", "fair enough")
    rig.whisper_from_him(102, "hey")
    rig.run()
    rig.whisper_from_him(102, "oh yeah?")
    rig.run()
    assert len(rig.chat_calls()) == 3
    user = rig.last_user()
    assert "Tonight you've already teased him" in user
    assert user.endswith(MOVE_HINT.format(move=MOVE_PHRASES["tease"].format(**HE)))
    assert user.endswith("You just teased him: say something new instead.")
    assert [s.text for s in rig.sent.says()][-1] == "fair enough"


def test_the_retry_hint_names_the_move_in_the_counterparts_pronouns(rig):
    p = rig.engine.personas.get(102)
    to_him = Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN)
    to_arkon = Turn(kind=TurnKind.CONTINUE, speaker=102, channel="guild", addressee=101)
    tells = [Tell("member flirt", True), Tell("move repeat: tease", False, True),
             Tell("summons", False, True)]
    hint = rig.engine._retry_hint(tells, p, to_him)
    assert "You just teased him: say something new instead." in hint
    assert SUMMONS_HINT.format(**HE) in hint
    assert REGEN_HINT.format(name=p.character_name) not in hint  # each tell has its own hint
    hint = rig.engine._retry_hint(tells, p, to_arkon)  # the flirt hint's "him" stays its own
    assert "You just teased them:" in hint and "don't ask them over" in hint
    assert rig.engine._retry_hint([Tell("move repeat: clause", False, True)], p,
                                  to_arkon) == REPEAT_HINT


def test_whisper_moves_stay_out_of_a_guild_turn(rig):
    """Whisper scope: her whispered move names itself only in a whisper to him."""
    told = MOVE_PHRASES["declare"].format(**HE)
    rig.script("i want you", "fair enough")
    rig.whisper_from_him(102, "hey")
    rig.run()
    rig.whisper_from_him(102, "what's up")
    rig.run()
    assert told in rig.last_user()
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild",
                               addressee=HUMAN))
    guild = "\n".join(m["content"] for m in rig.chat_calls()[-1]["messages"])
    assert told not in guild and "you've already" not in guild
    assert _moves(rig.store)[-1] == (102, HUMAN, "guild", 0, "")


def test_a_member_turn_names_her_moves_with_that_member(rig):
    rig.script("bet you can't keep up", "fair enough")
    to_arkon = Turn(kind=TurnKind.CONTINUE, speaker=102, channel="guild", addressee=101)
    assert rig.speak_turn(to_arkon)
    rig.run(60)
    assert rig.speak_turn(to_arkon)
    assert "Tonight you've already teased them" in rig.last_user()
    assert _moves(rig.store)[0] == (102, 101, "guild", 0, "tease")


def test_moves_survive_a_restart(store, clock, tmp_path):
    _cast(store)
    first = make_rig(store, clock, tmp_path)
    try:
        first.script("lol true")
        first.whisper_from_him(102, "hey")
        first.run()
    finally:
        first.close()
    again = make_rig(store, clock, tmp_path)
    try:
        again.script("fair enough")
        again.whisper_from_him(102, "you still there?")
        again.run()
        assert "Tonight you've already made a joke" in again.last_user()
    finally:
        again.close()


def test_a_failing_move_write_is_logged_without_the_text(rig, caplog, monkeypatch):
    def broken(**kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(rig.engine.moves, "record", broken)
    rig.script("lol true")
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.talk"):
        rig.whisper_from_him(102, "hey")
        rig.run()
    assert [s.text for s in rig.sent.says()] == ["lol true"]
    assert "move_record_failed" in caplog.text and "lol true" not in caplog.text


def _overlays_at_cap(data_dir) -> str:
    """Both of the owner's overlays a whisper to him at the explicit band reads, each at
    OVERLAY_CAP_TOKENS (after make_rig, which writes its own band overlay)."""
    text = " ".join(f"w{i:03d}" for i in range(OVERLAY_CAP_TOKENS * 4 // 5))
    assert estimate_tokens(text) == OVERLAY_CAP_TOKENS
    for name in ("romance_explicit", "romance_explicit_whisper"):
        (data_dir / "prompts" / f"{name}.txt").write_text(text + "\n", encoding="utf-8")
    return text


def _worst_case(store, clock, *, hot: bool = False, arc: bool = False) -> None:
    """A lust woman (the explicit band in a whisper to him) with 5 memories of him. `hot`: also
    at the stage with the longest whisper passage she can be stored at (banter) and at the top
    heat step. `arc`: at the top heat step at "planning" (its longer distance frame) with the
    longest plan lines (a set plan: the longest kind, two worries, the longest cover story), a
    partner at home, a rival at a noticed stage and her spark cooling: more than ARC_CAP_TOKENS
    of arc lines to place."""
    _cast(store, stance="lust", **({"household": "married, two kids at home"} if arc else {}))
    if hot or arc:
        store.put_relationship(Relationship(a=102, b=HUMAN, affinity=40.0, trust=30.0,
                                            attraction=95.0,
                                            romance_stage="planning" if arc else "banter"))
    if arc:
        now = clock.now()
        plan = {"kind": "neutral", "target": now + 12 * 86400, "cover": "a weekend away with "
                "friends", "worries": ["time off", "the home front"]}
        put_arc(store, Arc(102, HUMAN, plan=plan, plan_state="set", plan_at=now - 86400,
                           planning_since=now - 20 * 86400, updated=now))
        save_member(store, 104, player_gender="female", stance="drawn")
        store.put_relationship(Relationship(a=104, b=HUMAN, romance_stage="voice"))
        with store.transaction():
            store.conn.execute("INSERT INTO jealousy(holder, target, kind, level, updated, "
                               "evidence) VALUES (102, 104, 'rivalry', 80, 0, '[]')")
            store.conn.execute(
                "INSERT INTO attraction_log(night, guid, gained, decayed, neglected, hurt, "
                "half_life, before, after, stance_before, stance_after) "
                "VALUES ('2026-10-28', 102, 0, 5, 1, 0, 30, 95, 95, 'lust', 'lust')")
    t = int(clock.now()) - 3 * 86400
    for i, text in enumerate(("Zan carried me through the whole heroic run last week.",
                              "Zan stayed up until four talking about his new job with me.",
                              "Zan gave me the spare gems he had in the bank, all of them.",
                              "Zan laughed at my terrible joke about the murlocs again.",
                              "Zan promised to help me farm the drake mount on Sunday.")):
        store.insert_memory(make_mem(owner=102, text=text, actors=(HUMAN,), t_event=t + i,
                                     t_created=t + i, t_accessed=t + i, source="chat"))


def _worst_whisper(r, clock, caplog) -> tuple[str, list[str], int]:
    """8 lines of whisper chat shown, her own 5 last lines, 10 move rows and her 3 longest
    scene notes, then his whisper: her prompt's user message, the notes and the prompt's
    estimated tokens."""
    replies = ["bet you can't keep up", "how was work?", "ugh my boss is the worst",
               "lol true", "ok handsome"]
    for i, reply in enumerate(replies):  # 10 lines of whisper chat (8 shown), her own 5
        r.script(reply)
        r.whisper_from_him(102, f"so what happened next with the thing number {i}")
        r.run()
    assert len(r.sent.says()) == len(replies)
    for i, text in enumerate(("you look great", "see you after raid", "lol true",
                              "need better gear honestly", "how was work?")):  # 10 rows
        r.engine.moves.record(guid=102, counterpart=HUMAN, channel="whisper",
                              to_guid=HUMAN, text=text, ts=clock.now() - 100 + i)
    longest = sorted((t.format(**HE) for t in NOTE_TEXT.values()), key=len)[-3:]
    for i, note in enumerate(longest):  # the newest three
        r.engine.notes.add(102, HUMAN, f"note{i}", note, channel="whisper", to_guid=HUMAN,
                           ts=clock.now() + i / 1000)
    r.script("fair enough")
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.talk"):
        r.whisper_from_him(102, "so how was the rest of your evening")
        r.run()
    tokens = sum(estimate_tokens(m["content"]) for m in r.chat_calls()[-1]["messages"])
    return r.last_user(), longest, tokens


def test_worst_case_whisper_keeps_the_lately_line_without_a_last_resort_trim(
        store, clock, tmp_path, caplog):
    """Both overlays at their cap, 8 lines of whisper chat, 5 memories, her own 5 last lines,
    10 move rows and 3 scene notes (the longest): the Lately line and the scene are there, the
    distance frame is said once, and nothing is cut."""
    _worst_case(store, clock)
    r = make_rig(store, clock, tmp_path)
    overlay = _overlays_at_cap(tmp_path)
    try:
        user, longest, tokens = _worst_whisper(r, clock, caplog)
        assert "Tonight you've already" in user
        assert f"{SCENE_HEAD} {'; '.join(longest)}." in user
        assert user.count("live far apart") == 1
        assert tokens <= LIVE_BUDGET
        assert user.count(overlay) == 2  # both overlays, whole
        assert user.count("three days ago: Zan") == 5  # every memory
        assert user.split("Chat:\n")[1].split("\n\n")[0].count("\n") == 7  # 8 chat lines
        assert user.split("(don't make the same move again):\n")[1].count("\n") >= 5
        assert "last-resort trim" not in caplog.text
    finally:
        r.close()


def test_a_hot_worst_case_whisper_keeps_the_lately_line_without_a_last_resort_trim(
        store, clock, tmp_path, caplog):
    """The same with her stage passage (banter) and her heat at "aching": the Lately line is
    kept with no last-resort trim. The usual trims may take memories, chat and her own lines
    down to their floors."""
    _worst_case(store, clock, hot=True)
    r = make_rig(store, clock, tmp_path)
    _overlays_at_cap(tmp_path)
    try:
        user, _, _ = _worst_whisper(r, clock, caplog)
        assert "Tonight you've already" in user
        assert any(v.format(name="Zan", **HE) in user for v in STAGE_PASSAGES["banter"])
        assert "last-resort trim" not in caplog.text
    finally:
        r.close()


@pytest.mark.parametrize("seed", [1, 7, 10])  # 7 and 10 draw the longest arc lines
def test_an_arc_worst_case_whisper_keeps_the_lately_line_without_a_last_resort_trim(
        store, clock, tmp_path, caplog, seed):
    """The hot worst case with everything the arc has for her whisper (S8): her set plan's two
    lines, the partner line, the rival line and cooling, more than ARC_CAP_TOKENS. Where her
    plan stands and the Lately line are kept, the arc's lines stay within their cap, and there
    is no last-resort trim; the usual trims may take memories, chat and her own lines down to
    their floors."""
    _worst_case(store, clock, arc=True)
    r = make_rig(store, clock, tmp_path, seed=seed)
    _overlays_at_cap(tmp_path)
    try:
        user, _, tokens = _worst_whisper(r, clock, caplog)
        fill = dict(name="Zan", **HE)
        assert "Tonight you've already" in user
        assert "It's happening: a quiet weekend somewhere neutral, about 12 days off." in user
        second = ("On your mind: time off, the home front; the cover story is a weekend away "
                  "with friends.")
        cooling = [v.format(**fill) for v in COOLING_TEXT if v.format(**fill) in user]
        shown = [x for x in (second, PARTNER_TEXT["together"].format(**fill),
                             rival_text("Dunna", ("he", "him", "his")).format(**fill), *cooling)
                 if x in user]
        assert shown  # beside her plan's first line, what fit this turn
        assert sum(estimate_tokens(x) for x in shown) + 18 <= ARC_CAP_TOKENS
        assert user.count("live far apart") == 1
        assert tokens <= LIVE_BUDGET
        assert "last-resort trim" not in caplog.text
    finally:
        r.close()


def test_his_own_lines_never_become_her_moves(rig):
    """Only what she said is recorded: his whispers make no rows."""
    rig.script("fair enough")
    rig.whisper_from_him(102, "i want you, get over here")
    rig.run()
    assert [m[0] for m in _moves(rig.store)] == [102]


def test_a_template_line_records_no_move(rig):
    """A template (say_as) is no move of hers, as it is none of her own last lines."""
    async def go():
        task = asyncio.create_task(rig.engine.say_as(101, "guild", "lol nice",
                                                     kind=TurnKind.OPEN))
        await rig.advance(40)
        return task.result()
    assert rig.sync(go())
    assert _moves(rig.store) == []
