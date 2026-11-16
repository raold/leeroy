"""Every message of his gets a member's answer (talk/owed.py, owner 2026-11-15): the plain third
ask and the fallback line inside an owed turn, the debt passing down a job, the watchdog that
rescues a message nobody answered (and holds through his raid's fight, an AFK member, a Free GPU
pause or his absence), the count health reads, and the fallback line itself. Fakes only:
FakeLLM, FakeClock, a scratch mind.db. Invented text."""
import logging
import re
import time
from types import SimpleNamespace

import pytest

from conftest import HUMAN, TEST_CAST, run, seed_test_cast
from engine_rig import engine_clock, make_rig, numbered
from leeroy_mind import health
from leeroy_mind.discord import store as dstore
from leeroy_mind.llm.client import LLMError
from leeroy_mind.protocol import Boss, Chat, Group
from leeroy_mind.talk import owed
from leeroy_mind.talk.owed import OwedWatch, count_unanswered, fallback_line, topic_word
from leeroy_mind.talk.turns import TurnKind

HINT = "Answer what Zan just said"


@pytest.fixture
def clock():
    return engine_clock()


def _words(text):
    return re.findall(r"[a-z']+", text.casefold())


def _shares_run(a, b, n=4):
    """Whether `a` holds a run of `n` words of `b`."""
    wa, wb = _words(a), _words(b)
    runs = {tuple(wb[i:i + n]) for i in range(len(wb) - n + 1)}
    return any(tuple(wa[i:i + n]) in runs for i in range(len(wa) - n + 1))


# -- the fallback line -------------------------------------------------------------------------

HIS = ["hey fam", "gn everyone, logging off", "come to brazen please", "thanks for the heals",
       "what bg should we queue next?", "ate a bunch of ground beef and rice in a curry broth",
       "thats it? oh :p?", "i love yall so much thank you for all you do",
       "do you want to come over tonight?", "...", "", "<3"]


@pytest.mark.parametrize("his", HIS)
def test_the_fallback_line_is_never_empty_and_never_says_his_words_back(his):
    for seed in range(20):
        line = fallback_line(his, called="Zan", seed=seed)
        assert line.strip() and any(c.isalpha() for c in line)
        assert not _shares_run(line, his, 3), (his, line)


def test_the_fallback_line_answers_by_the_kind_of_message_and_asks_back():
    assert "omw" in fallback_line("come to brazen please", called="Zan", seed=1) or \
        "way" in fallback_line("come to brazen please", called="Zan", seed=1) or \
        "coming" in fallback_line("come to brazen please", called="Zan", seed=1)
    bye = {fallback_line("gn everyone", called="Zan", seed=s) for s in range(30)}
    assert all(not line.endswith("?") or "tomorrow" in line for line in bye)
    for his in ("hey fam", "what bg should we queue next?",
                "ate a bunch of ground beef and rice in a curry broth"):
        assert all("?" in fallback_line(his, called="Zan", seed=s) for s in range(30)), his
    assert "ground" in fallback_line("ate a bunch of ground beef and rice in a curry broth",
                                     called="Zan", seed=3)
    assert fallback_line("hey", called="Zan", seed=7) == fallback_line("hey", called="Zan", seed=7)


def test_the_topic_word_skips_filler_names_and_stretched_words():
    assert topic_word("ate a bunch of ground beef and rice in a curry broth") == "ground"
    assert topic_word("belra you there?", avoid=["Belra"]) == ""
    assert topic_word("so fulllllll right now") == ""
    assert topic_word("honestly literally whatever") == "whatever"


# -- inside the turn: Pipeline.owed_fallback, the one fallback (#325) ----------------------------

def test_an_owed_whisper_answer_whose_drafts_both_echo_him_gets_the_fallback(store, clock,
                                                                            tmp_path):
    """Live 2026-11-12: his whisper's answer was dropped after two drafts kept a tell. The owed
    fallback (OWED_FALLBACK_HINT) is asked and its clean draft goes out."""
    his = "come find me by the docks after the raid tonight okay"

    def script(model, messages):
        return "sounds good, which docks?" if HINT in messages[-1]["content"] else his

    async def main():
        r = make_rig(store, clock, tmp_path, script=script)
        await r.human(his, channel="whisper", to_guid=101)
        await r.advance(40)
        said = [s for s in r.sent.says() if s.channel == "whisper"]
        assert [s.guid for s in said] == [101]
        assert "docks" in said[0].text.casefold()
        assert HINT in r.last_user()
        assert [u["dropped"] for u in r.utterances()] == [None]
    run(main())


def test_a_refused_fallback_is_asked_once_more_with_what_was_wrong(store, clock, tmp_path):
    """review-325-r2 P3-C: the fallback that still asks him over is asked once more, told in
    words what was wrong, and its second draft goes out."""
    def script(model, messages):
        last = messages[-1]["content"]
        if "Not like that" in last:
            return "missed you today. how was work?"
        return "come over to my place tonight"

    async def main():
        r = make_rig(store, clock, tmp_path, script=script)
        await r.human("what are you up to?", channel="whisper", to_guid=102)
        await r.advance(40)
        said = [s.text for s in r.sent.says() if s.guid == 102]
        assert said and "work" in " ".join(said)
        assert "don't ask him over" in r.last_user() and HINT in r.last_user()
        assert len(r.chat_calls()) == 4
    run(main())


@pytest.mark.parametrize("bad", ["- one thing\n- another thing", "*sighs deeply and looks away*",
                                 "hm\nBelra: what about me"])
def test_the_fallback_never_sends_a_list_an_action_or_dialogue(store, clock, tmp_path, bad):
    """review-325-r2 P3-A: OWED_HARD_TELLS holds dialogue, list, action and sample too."""
    his = "come find me by the docks after the raid tonight okay"

    def script(model, messages):
        last = messages[-1]["content"]
        if "Not like that" in last:
            return "sure, which docks?"
        return bad if HINT in last else his

    async def main():
        r = make_rig(store, clock, tmp_path, script=script)
        await r.human(his, channel="whisper", to_guid=101)
        await r.advance(40)
        said = [s.text for s in r.sent.says() if s.guid == 101]
        assert said == [said[0]] and "docks" in said[0].casefold()
    run(main())


def _owed_rig(store, clock, tmp_path, script):
    r = make_rig(store, clock, tmp_path, script=script)
    return r, _watch(r)


def test_a_model_that_keeps_failing_still_leaves_him_an_answer(store, clock, tmp_path):
    """Every request fails (a timeout): the turn drops (llm_error) after its owed fallback also
    failed; the watchdog's rescue turn ends in the line built from his message."""
    def script(model, messages):
        raise LLMError("/api/chat: timed out")

    async def main():
        r, w = _owed_rig(store, clock, tmp_path, script)
        await _tick(r, w, owed.START_GRACE_S)
        await r.human("what bg should we queue next?", channel="whisper", to_guid=103)
        await _tick(r, w, 30)
        assert r.sent.says() == []
        assert [u["dropped"] for u in r.utterances()] == ["llm_error"]
        await _tick(r, w, 60)
        said = [s for s in r.sent.says() if s.guid == 103]
        assert len(said) == 1 and "?" in said[0].text
    run(main())


def test_every_draft_echoing_him_still_ends_in_an_answer(store, clock, tmp_path, caplog):
    """The model only ever says his words back: the turn drops, the rescue turn's own drafts and
    fallbacks echo too, and its last resort is the line built from his message, never his
    words."""
    his = "i need you to tell me about your day at the office"
    caplog.set_level(logging.INFO)

    async def main():
        r, w = _owed_rig(store, clock, tmp_path, lambda m, msgs: his)
        await _tick(r, w, owed.START_GRACE_S)
        await r.human(his, channel="whisper", to_guid=104)
        await _tick(r, w, 120)
        said = [s.text for s in r.sent.says() if s.guid == 104]
        assert len(said) == 1 and not _shares_run(said[0], his, 4)
        assert any("say_owed_last_resort" in m for m in caplog.messages)
    run(main())


def test_a_silent_guild_ends_in_one_answer(store, clock, tmp_path):
    """The model only says (nothing): every answer drops; the watchdog's rescue turn answers
    once, and only once."""
    async def main():
        r, w = _owed_rig(store, clock, tmp_path, lambda m, msgs: "(nothing)")
        await _tick(r, w, owed.START_GRACE_S)
        await r.human("evening all, how is everyone?")
        await _tick(r, w, 300)
        said = r.sent.says()
        assert len(said) == 1
        sent = r.store.conn.execute("SELECT why FROM utterance WHERE dropped IS NULL").fetchall()
        assert [x[0] for x in sent] == ["reply:rescue"]
    run(main())


def test_the_debt_passes_to_the_next_answer_when_the_first_cannot_speak(store, clock,
                                                                        tmp_path):
    """The first planned answer's member goes AFK before her turn: the next answer to his line in
    that job carries the debt, so the guild's pace can't hold it back."""
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("yeah {n}"))
        owed_at_start = []
        speak = r.engine._speak

        async def spy(turn, meta):
            owed_at_start.append((turn.speaker, meta.owed_answer))
            return await speak(turn, meta)
        r.engine._speak = spy
        await r.human("evening all, anyone around?")
        first = r.engine.planned_turns()[0].speaker
        r.world.afk.add(first)
        for g in TEST_CAST:  # the guild's pace is full for everyone
            r.engine.guards.record(f"busy-{g}", g, r.clock.now(), TurnKind.OPEN)
        await r.advance(40)
        assert owed_at_start[0] == (first, True) and owed_at_start[1][1] is True
        said = [s.guid for s in r.sent.says()]
        assert said and first not in said and said[0] == owed_at_start[1][0]
    run(main())


# -- the watchdog ------------------------------------------------------------------------------

def _watch(r, **over):
    app = SimpleNamespace(store=r.store, clock=r.clock, engine=r.engine, world=r.world,
                          scheduler=r.engine.scheduler, discord_talk=None)
    for k, v in over.items():
        setattr(app, k, v)
    w = OwedWatch(app)
    r.engine.owed = w
    return w


async def _tick(r, w, seconds):
    for _ in range(int(seconds)):
        await r.advance(1)
        w.tick(r.clock.now())


def _line(r, text, channel="guild", to_guid=0):
    """His line stored and in the world, as the app does, without the engine's planning (an
    answer lost before it was even planned)."""
    ev = Chat(ts=0, channel=channel, from_guid=HUMAN, from_name="Testmage", to_guid=to_guid,
              text=text, map=0, zone=10)
    lid = r.store.insert_chat_line(ev, session_id=None, origin="human")
    row = r.store.chat_lines([lid])[0]
    r.world.add_line(row, r.clock.now())
    return row


def test_an_unanswered_guild_line_is_rescued_after_the_grace(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("oh nice {n}, what happened?"))
        w = _watch(r)
        await _tick(r, w, owed.START_GRACE_S)
        row = _line(r, "finally finished the big quest chain")
        w.note_line(row, "guild")
        await _tick(r, w, owed.GRACE_S["game"] - 10)
        assert r.sent.says() == []  # inside the grace: nothing yet
        await _tick(r, w, 40)
        said = r.sent.says()
        assert len(said) == 1 and said[0].channel == "guild"
        why = r.store.conn.execute("SELECT why, trigger_line FROM utterance").fetchall()
        assert [tuple(x) for x in why] == [("reply:rescue", row.id)]
        assert any("owed_rescue:" in m and "reason=not_planned" in m for m in caplog.messages)
        await _tick(r, w, 200)
        assert len(r.sent.says()) == 1 and not w._game  # answered: settled, never twice
    run(main())


def test_an_answered_line_is_left_alone(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("ha {n}"))
        w = _watch(r)
        await _tick(r, w, owed.START_GRACE_S)
        await r.human("finally finished the big quest chain")
        await _tick(r, w, 200)
        assert len({s.guid for s in r.sent.says()}) == len(r.sent.says()) >= 1
        assert not w._game
        assert "reply:rescue" not in {u[0] for u in r.store.conn.execute(
            "SELECT why FROM utterance").fetchall()}
    run(main())


def test_a_whisper_to_an_afk_member_waits_for_her(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("back now {n}, sorry"))
        w = _watch(r)
        await _tick(r, w, owed.START_GRACE_S)
        r.world.afk.add(103)
        await r.human("you around?", channel="whisper", to_guid=103)
        await _tick(r, w, 120)
        assert r.sent.says() == []
        assert any("owed_held" in m and "reason=afk" in m for m in caplog.messages)
        r.world.afk.discard(103)
        await _tick(r, w, 60)
        said = r.sent.says()
        assert [(s.guid, s.channel) for s in said] == [(103, "whisper")]
    run(main())


def test_his_raid_fight_holds_the_answer_until_it_ends(store, clock, tmp_path, caplog):
    """A boss fight: nobody in his raid may answer (no officer or raid lead in it), so the
    watchdog holds his line and answers it once the wipe's debrief is over."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("we got this {n}"))
        w = _watch(r)
        await _tick(r, w, owed.START_GRACE_S)
        group = (HUMAN, 101, 103, 105)
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=True, members=group))

        def boss(state):
            return Boss(ts=0, map=631, instance_id=5, boss_id=0, boss_name="", state=state,
                        members=group)
        await r.event(boss("engaged"))
        await r.human("nice work so far, keep an eye on the adds", channel="raid")
        await _tick(r, w, 120)
        assert r.sent.says() == []
        assert any("owed_held" in m and "reason=raid" in m for m in caplog.messages)
        await r.event(boss("wiped"))
        await _tick(r, w, 150)
        rescued = r.store.conn.execute("SELECT guid, channel FROM utterance WHERE "
                                       "why = 'reply:rescue' AND dropped IS NULL").fetchall()
        assert len(rescued) == 1 and tuple(rescued[0])[1] == "raid"
        assert tuple(rescued[0])[0] in group
    run(main())


def test_nothing_is_rescued_while_he_is_offline_or_the_gpu_is_paused(store, clock, tmp_path,
                                                                      caplog):
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("hey {n}"))
        gate = SimpleNamespace(paused=True)
        r.engine.scheduler.gpu_gate = None
        w = _watch(r, scheduler=SimpleNamespace(gpu_gate=gate))
        await _tick(r, w, owed.START_GRACE_S)
        w.note_line(_line(r, "anyone up for a dungeon"), "guild")
        await _tick(r, w, 120)
        assert r.sent.says() == []
        assert any("reason=gpu_paused" in m for m in caplog.messages)
        gate.paused = False
        r.world.human_online = False
        await _tick(r, w, 30)
        assert r.sent.says() == []
        assert any("reason=human_offline" in m for m in caplog.messages)
        r.world.human_online = True
        await _tick(r, w, 30)
        assert [s.channel for s in r.sent.says()] == ["guild"]
    run(main())


def test_a_line_nobody_can_hear_is_let_go_and_an_old_one_too(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("hm {n}"))
        w = _watch(r)
        await _tick(r, w, owed.START_GRACE_S)
        r.world.online.discard(105)
        w.note_line(_line(r, "you there?", channel="whisper", to_guid=105), "whisper")
        await _tick(r, w, 60)
        assert any("owed_given_up" in m and "reason=target_offline" in m
                   for m in caplog.messages)
        assert r.sent.says() == [] and not w._game
        # held the whole time (paused): let go once too old
        gate = SimpleNamespace(paused=True)
        w.app.scheduler = SimpleNamespace(gpu_gate=gate)
        w.note_line(_line(r, "anyone up for a dungeon"), "guild")
        await _tick(r, w, owed.MAX_AGE_S["game"] + 10)
        assert any("owed_given_up" in m and "reason=too_old" in m for m in caplog.messages)
        assert r.sent.says() == []
    run(main())


def test_addon_lines_are_never_owed(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        w = _watch(r)
        for text in ("<DBM> Testmage has wiped on Valithria Dreamwalker",
                     "Recount - Damage Done for Overall Data", "[BigWigs] Grobbulus engaged"):
            w.note_line(_line(r, text, channel="whisper", to_guid=102), "whisper")
        assert not w._game
    run(main())


# -- the count health reads ---------------------------------------------------------------------

def _his(store, text, ts, channel="guild", to_guid=0):
    ev = Chat(ts=0, channel=channel, from_guid=HUMAN, from_name="Testmage", to_guid=to_guid,
              text=text, map=0, zone=10)
    return store.insert_chat_line(ev, session_id=None, origin="human", ts=ts)


def _theirs(store, guid, text, ts, channel="guild", to_guid=0, trigger=None):
    ev = Chat(ts=0, channel=channel, from_guid=guid, from_name="M", to_guid=to_guid, text=text,
              map=0, zone=10)
    store.insert_chat_line(ev, session_id=None, origin="mind", ts=ts)
    store.log_utterance(ts, guid, channel, "reply_human", text, [], None, None,
                        trigger_line=trigger)


def test_the_count_groups_his_bursts_and_reads_every_medium(store):
    seed_test_cast(store)
    t0 = 1_800_000_000.0
    a = _his(store, "hey fam", t0)
    b = _his(store, "hows everyone", t0 + 20)  # one message with a
    _theirs(store, 101, "good, you?", t0 + 30, trigger=b)
    _his(store, "anyone up for a dungeon", t0 + 600)  # never answered
    _his(store, "Recount - Damage Done for Overall Data", t0 + 700)  # a paste: no message
    _his(store, "follow", t0 + 710, channel="party")  # a bot command: no message
    w = _his(store, "you around?", t0 + 800, channel="whisper", to_guid=102)
    _theirs(store, 102, "yep", t0 + 810, channel="whisper", to_guid=HUMAN)
    _his(store, "<DBM> Testmage has wiped", t0 + 900, channel="whisper", to_guid=103)
    _his(store, "still there?", t0 + 1000, channel="whisper", to_guid=103)  # unanswered
    dm = dstore.post(store, channel="dm:104", author=HUMAN, origin="human", text="hi", now=t0)
    dstore.post(store, channel="dm:104", author=104, origin="mind", text="hey!", now=t0 + 60,
                reply_to=dm)
    dstore.post(store, channel="dm:105", author=HUMAN, origin="human", text="hi", now=t0)
    ch = dstore.post(store, channel="general", author=HUMAN, origin="human", text="morning",
                     now=t0)
    dstore.post(store, channel="general", author=101, origin="mind", text="unrelated",
                now=t0 + 10)
    got = count_unanswered(store.conn, HUMAN, t0 - 1, t0 + 5000)
    assert got.messages == {"guild": 2, "whisper": 2, "discord_dm": 2, "discord_channel": 1}
    assert got.unanswered == {"guild": 1, "whisper": 1, "discord_dm": 1, "discord_channel": 1}
    assert (got.total, got.missed) == (7, 4)
    assert a and w and ch
    # a message younger than the window is not yet due
    assert count_unanswered(store.conn, HUMAN, t0 + 990, t0 + 1100).total == 0


def test_health_names_the_unanswered_count_by_medium(store):
    seed_test_cast(store)
    now = time.time()
    store.set_meta("human_guid", str(HUMAN))
    _his(store, "anyone up for a dungeon", now - 3600)
    check = health._owner_replies(store.conn, now)
    assert (check.name, check.status) == ("owner-replies", health.NOTE)
    assert "1 of 1 unanswered" in check.text and "guild 1" in check.text
    assert "dungeon" not in check.text  # counts only, never a text
    for i in range(health.OWNER_UNANSWERED_FAIL):
        _his(store, f"line {i}", now - 3000 + i * 200, channel="whisper", to_guid=101)
    assert health._owner_replies(store.conn, now).status == health.FAIL


def test_the_model_away_holds_the_rescue_until_it_is_back(store, clock, tmp_path, caplog):
    """While the app's silent latch is off every line is refused (MindApp._send): the watchdog
    waits, then answers once the model is back."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("back {n}, what did i miss?"))
        w = _watch(r, model_ready=False)
        await _tick(r, w, owed.START_GRACE_S)
        w.note_line(_line(r, "anyone up for a dungeon"), "guild")
        await _tick(r, w, 120)
        assert r.sent.says() == []
        assert any("owed_held" in m and "reason=model_away" in m for m in caplog.messages)
        w.app.model_ready = True
        await _tick(r, w, 40)
        assert [s.channel for s in r.sent.says()] == ["guild"]
    run(main())


# -- several answers to him at once (owner, 2026-11-16) -----------------------------------------

def test_the_owed_deadline_scales_with_the_answers_in_flight_up_to_a_ceiling():
    assert owed.owed_deadline("whisper") == 90.0
    assert owed.owed_deadline("guild", 3) == 270.0
    assert owed.owed_deadline("guild", 20) == 600.0  # the hard ceiling
    assert owed.owed_deadline("dm:102", 2) == 600.0
    assert owed.owed_deadline("discord:general", 9) == 900.0


def test_four_whispers_at_once_are_all_answered_on_a_slow_model(store, clock, tmp_path, caplog):
    """Three model slots, four of his whispers, 50 s a request: the fourth waits 100 s, past the
    90 s an owed answer gets alone. With four in flight its deadline scales (360 s), so it is
    answered, never expired."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("yep {n}, right here"))
        chat = r.llm.chat

        async def slow(*args, **kwargs):
            await r.clock.sleep(50)
            return await chat(*args, **kwargs)
        r.llm.chat = slow
        for g in (101, 102, 103, 104):
            await r.human("you there?", channel="whisper", to_guid=g)
        await r.advance(400, 2)
        assert {s.guid for s in r.sent.says() if s.channel == "whisper"} == {101, 102, 103, 104}
        assert not [u for u in r.utterances() if u["dropped"]]
        assert any("owed_deadline_extended" in m and "in_flight=4" in m for m in caplog.messages)
    run(main())
