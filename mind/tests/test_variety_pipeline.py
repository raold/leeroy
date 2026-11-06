"""Topic memory in the live pipeline (conversation variety, task A5): each turn's moves are
stored, the next prompt to the same person names them ("Tonight you've already..."), a repeated
move or a summons is asked again with a hint, and a whisper's moves stay in that whisper. The
test cast and invented lines only."""
import asyncio
import logging

import pytest

from conftest import HUMAN, make_mem, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.talk.filters import Tell
from leeroy_mind.talk.moves import MOVE_PHRASES, MOVES
from leeroy_mind.talk.pipeline import MOVE_HINT, REGEN_HINT, REPEAT_HINT, SUMMONS_HINT
from leeroy_mind.talk.romance import OVERLAY_CAP_TOKENS
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
    first.script("lol true")
    first.whisper_from_him(102, "hey")
    first.run()
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


def _worst_case(store, clock) -> None:
    """A lust woman (the explicit band in a whisper to him) with 5 memories of him."""
    _cast(store, stance="lust")
    t = int(clock.now()) - 3 * 86400
    for i, text in enumerate(("Zan carried me through the whole heroic run last week.",
                              "Zan stayed up until four talking about his new job with me.",
                              "Zan gave me the spare gems he had in the bank, all of them.",
                              "Zan laughed at my terrible joke about the murlocs again.",
                              "Zan promised to help me farm the drake mount on Sunday.")):
        store.insert_memory(make_mem(owner=102, text=text, actors=(HUMAN,), t_event=t + i,
                                     t_created=t + i, t_accessed=t + i, source="chat"))


def test_worst_case_whisper_keeps_the_lately_line_without_a_last_resort_trim(
        store, clock, tmp_path, caplog):
    """Both overlays at their cap, 8 lines of whisper chat, 5 memories, her own 5 last lines
    and 10 move rows: the Lately line is there and nothing is cut."""
    _worst_case(store, clock)
    r = make_rig(store, clock, tmp_path)
    overlay = _overlays_at_cap(tmp_path)
    try:
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
        r.script("fair enough")
        with caplog.at_level(logging.WARNING, logger="leeroy_mind.talk"):
            r.whisper_from_him(102, "so how was the rest of your evening")
            r.run()
        user = r.last_user()
        assert "Tonight you've already" in user
        assert user.count(overlay) == 2  # both overlays, whole
        assert user.count("three days ago: Zan") == 5  # every memory
        assert user.split("Chat:\n")[1].split("\n\n")[0].count("\n") == 7  # 8 chat lines
        assert user.split("(don't make the same move again):\n")[1].count("\n") >= 5
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
