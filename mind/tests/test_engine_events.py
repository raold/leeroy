"""The talk engine's reactions to game events, its bursts, romance follow-ups and turn sources
(Task 16). The rig is in engine_rig.py."""
import asyncio
import json
from dataclasses import replace

import pytest

from conftest import HUMAN, make_mem, make_persona, run, seed_test_cast
from leeroy_mind.personas.io import save_persona
from engine_rig import (OVERLAY, RAID, FakeLibrary, chunk, engine_clock, make_rig, numbered,
                        save_member)
from leeroy_mind.protocol import (Ack, Achievement, Boss, Death, Group, Login, Logout, LootWon,
                                  Resurrect, Zone)
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.reactions import (DIED_FIRST, FACTS_RULE, JOKE_OK, KILL_RULE,
                                        LOGIN_QUIET_S, MECHANICS_RULE, NO_JOKE, WIPE_KNOWN)
from leeroy_mind.talk.romance import INVARIANTS
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_llm import script_from

ROMANCE = INVARIANTS.split(",")[1].strip()  # "consent always": in every romance instruction


@pytest.fixture
def clock():
    return engine_clock()


def boss(state: str) -> Boss:
    return Boss(ts=0, map=631, instance_id=5, boss_id=0, boss_name="", state=state, members=RAID)


async def raid_rig(store, clock, tmp_path, **kw):
    r = make_rig(store, clock, tmp_path, **kw)
    await r.event(Group(ts=0, leader_guid=102, raid=True, members=RAID))
    return r


def test_login_greetings(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        store.put_relationship(Relationship(a=101, b=HUMAN, affinity=55, familiarity=80,
                                            interactions=20))
        t = int(clock.now()) - 86400
        store.insert_memory(make_mem(
            owner=101, kind="conversation", text="Zan said he starts the new job on Monday.",
            actors=(HUMAN,), t_event=t, t_created=t, t_accessed=t, source="extract",
            evidence=("chat:1",)))
        r = make_rig(store, clock, tmp_path)
        await r.session()
        t0 = clock.now()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(120)
        says = r.sent.timed_says()
        assert 2 <= len(says) <= 3 and len({s.guid for _, s in says}) == len(says)
        # the ruling's delays (5-12 s, then 5-14 s after the one before), each landing after
        # its typing: at least 2 s, at most 4 s plus the line at 5 characters a second, plus a
        # 1 s clock step
        typing = (2.0, 4.0 + len("fair enough") / 5.0 + 1.0)
        times = [at - t0 for at, _ in says]
        assert 5 + typing[0] <= times[0] <= 12 + typing[1]
        assert all(5 + typing[0] <= b - a <= 14 + typing[1] for a, b in zip(times, times[1:]))
        assert {k for _, k in r.sent_kinds()} == {"greet"}
        arkon = [p for p in r.prompts() if "You play Arkon" in p]
        assert arkon and ("Follow up on this, if it still fits: last night: Zan said he starts "
                          "the new job on Monday.") in arkon[0]
        others = [p for p in r.prompts() if "You play Arkon" not in p]
        assert others and not any("Follow up" in p for p in others)
        greeted = len(r.sent.says())
        await r.event(Login(ts=0, guid=HUMAN, human=True))  # again, in the same session
        await r.advance(120)
        assert len(r.sent.says()) == greeted
    run(main())


GREETINGS = ["evening zan, good to see you", "yo zan, about time", "hey zan, welcome back",
             "evening, zan", "there he is", "look who it is"]


def test_greetings_never_invent_history(store, clock, tmp_path):
    """The rehearsal: greeters with no memory of him were told to pick up "something from
    last time", made up a quest he was stuck on, and the second copied the first. A greeting
    is plain unless the greeter remembers something with him (a memory about others is not
    history with him); the later greeters see who already said hi and may stay quiet."""
    async def main():
        seed_test_cast(store)
        store.put_relationship(Relationship(a=101, b=HUMAN, affinity=55, familiarity=80,
                                            interactions=20))
        t = int(clock.now()) - 86400
        store.insert_memory(make_mem(owner=101, text="Coros pulled the whole room again.",
                                     actors=(103,), t_event=t, t_created=t, t_accessed=t))
        r = make_rig(store, clock, tmp_path, script=script_from(GREETINGS))
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(120)
        prompts, says = r.prompts(), r.sent.says()
        assert len(says) >= 2 and len(prompts) == len(says)
        assert not any("last time" in p or "Follow up" in p for p in prompts)
        said = [r.engine.personas.get(s.guid).character_name for s in says]
        assert "already said hi" not in prompts[0]
        for i, later in enumerate(prompts[1:], 1):
            names = later.split(" already said hi")[0].rsplit(". ", 1)[-1]
            assert set(names.replace(" and ", ", ").split(", ")) == set(said[:i])
            assert "or answer (nothing)" in later
            # who said hi, never their words, and nothing in chat is hers to answer
            assert all(f"{n} said hi to Zan" in later for n in said[:i])
            assert not any(s.text in later for s in says[:i])
            assert later.endswith("Talk to Zan; nothing in chat is addressed to you.")
    run(main())


def _thin_login_memories(store, clock, seed: int) -> bool:
    """Thin memories of him, as the rehearsal had them: the owner's standing canon with him in
    it for everyone, a chat about someone else, and on even seeds one real memory of
    something he said in an earlier session. True when that real memory is there."""
    t = int(clock.now()) - 3 * 86400
    for g in (101, 102, 103, 104, 105):
        store.insert_memory(make_mem(
            owner=g, kind="conversation", text="Zan and I always chat at the bank before raid.",
            actors=(HUMAN,), t_event=t, t_created=t, t_accessed=t, source="owner",
            evidence=("owner:bank",)))
        store.insert_memory(make_mem(owner=g, text="Coros pulled the whole room before raid.",
                                     actors=(103,), t_event=t, t_created=t, t_accessed=t))
    if seed % 2:
        return False
    for g in (101, 102, 103, 104, 105):
        store.insert_memory(make_mem(
            owner=g, kind="conversation", text="Zan said he was stuck on the bombing quest.",
            actors=(HUMAN,), t_event=t, t_created=t, t_accessed=t, source="extract",
            evidence=("chat:1",)))
    return True


def test_thirty_logins_on_thin_memories_greet_him_and_invent_nothing(clock, tmp_path):
    """Rehearsal 2, finding 1: the best-known greeter, told to follow up on "something he told
    you last time" whenever any memory had him in it, invented a quest; the second greeter
    answered her question instead of greeting him. This model takes every bait: it asks about
    a quest whenever its prompt mentions one, and answers any question it sees in chat. Over
    30 logins no greeting asks about a quest without a real memory of one, and none answers
    another member."""
    from leeroy_mind.storage.db import open_db
    from leeroy_mind.storage.store import Store
    quest_q, answer = "you still stuck on that quest?", "yeah. still doing it. you?"
    hellos = iter(["evening zan, how was your week?", "hey zan, you good?", "yo zan, about time",
                   "evening, zan", "there he is", "look who it is"] * 40)

    def script(model, messages):
        system, user = messages[0]["content"], messages[1]["content"]
        if "quest" in (system + user).casefold() or "last time" in user:
            return quest_q  # the bait taken: a quest, or a "last time" to make up
        chat = user.split("Chat:\n", 1)[1].split("\n\n", 1)[0] if "Chat:\n" in user else ""
        return answer if "?" in chat else next(hellos)

    async def main():
        for seed in range(30):
            store = Store(open_db(tmp_path / f"mind{seed}.db"), clock=clock)
            seed_test_cast(store)
            save_member(store, 102, player_gender="female", stance="lust", style="bold")
            save_member(store, 104, player_gender="female", stance="drawn")
            store.put_relationship(Relationship(a=101 + seed % 5, b=HUMAN, affinity=55,
                                                familiarity=80, interactions=20))
            real = _thin_login_memories(store, clock, seed)
            r = make_rig(store, clock, tmp_path, script=script, seed=seed)
            await r.session()
            await r.event(Login(ts=0, guid=HUMAN, human=True))
            await r.advance(120)
            says = [s.text for s in r.sent.says()]
            assert says, seed
            assert answer not in says, seed  # every greeting is to him
            if not real:  # no memory of a quest of his: nobody asks about one
                assert quest_q not in says, seed
            for p in r.prompts():
                assert "jump at it" not in p, seed
                if "Follow up" in p:  # only ever on the real memory, quoted
                    assert real and ("Follow up on this, if it still fits: three days ago: Zan "
                                     "said he was stuck on the bombing quest.") in p, seed
            await r.engine.end_session()
            store.close()
    run(main())


def test_a_login_brings_two_or_three_hellos_then_quiet(store, clock, tmp_path):
    """The rehearsal: seven hellos in two minutes (two greetings, then three answers to his
    "evening all"). The greetings and the answers to his own hello come to 2-3 in all; for
    LOGIN_QUIET_S after his login only his lines are answered (no bursts, no gz for a
    member's achievement); then the guild talks among itself again."""
    async def main():
        r = make_rig(store, clock, tmp_path, chattiness=10, script=numbered())
        r.engine.bits.p_fire = 0.0
        await r.session()
        t0 = clock.now()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(20)
        await r.human("evening all")
        await r.event(Achievement(ts=0, guid=103, achievement_id=9, name="Glory"))
        while clock.now() < t0 + LOGIN_QUIET_S - 30:
            await r.advance(1)
            await r.engine.tick(clock.now())
        kinds = [k for _, k in r.sent_kinds()]
        assert 2 <= len(kinds) <= 3 and set(kinds) <= {"greet", "reply_human"}
        await r.human("anyone know where the argent tournament is?")  # answered as ever
        for _ in range(29):
            await r.advance(1)
            await r.engine.tick(clock.now())
        assert "reply_human" in [k for _, k in r.sent_kinds()[len(kinds):]]
        assert {k for _, k in r.sent_kinds()} <= {"greet", "reply_human"}
        for _ in range(900):  # after the quiet, the social talk comes back
            await r.advance(1)
            await r.engine.tick(clock.now())
        assert {"open", "continue"} & {k for _, k in r.sent_kinds()}
    run(main())


def test_asides_count_against_the_hour_only_when_said(store, clock, tmp_path):
    """romance.py:247: at jealousy 10 four asides start an hour. Here every man answers
    (nothing), so none was said and none uses the hour up: more than four are asked."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="drawn", style="bold")
        for g in (101, 103, 104, 105):
            save_member(store, g, jealousy_mode="teaser")
        talk = numbered("thought number {n} for zan")

        def script(model, messages):
            if "flirting with Zan in chat" in messages[1]["content"]:
                return "(nothing)"
            return talk(model, messages)
        r = make_rig(store, clock, tmp_path, jealousy=10, script=script)
        for _ in range(12):  # three minutes apart, all inside one hour
            task = asyncio.create_task(r.engine.speak(
                Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN)))
            await r.advance(180, step=10)
            assert task.result()
        assert sum("flirting with Zan in chat" in p for p in r.prompts()) > 4
    run(main())


def test_each_utterance_says_why_it_was_said(store, clock, tmp_path):
    """Carried to Task 17 from the cognition memo: every utterance row, sent or dropped, keeps
    the select reason and the bit, and the mind's own lines keep who could see them."""
    async def main():
        seed_test_cast(store)
        store.put_relationship(Relationship(a=101, b=HUMAN, affinity=55, familiarity=80,
                                            interactions=20))
        r = make_rig(store, clock, tmp_path, script=numbered("evening there number {n}"))
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(120)

        def rows(kind):
            return store.conn.execute("SELECT guid, why, bit, dropped FROM utterance "
                                      "WHERE kind = ? ORDER BY id", (kind,)).fetchall()
        greets = rows("greet")
        assert greets and {x["why"] for x in greets} <= {"greet:reason_pool",
                                                          "greet:weighted_draw"}
        assert [x["why"] for x in greets if x["guid"] == 101] == ["greet:reason_pool"]
        await r.human("dunna you up?")
        await r.advance(40)
        replies = rows("reply_human")
        assert [x["why"] for x in replies if x["guid"] == 104] == ["reply:named"]
        assert {x["why"] for x in replies} <= {"reply:named", "reply:reason_pool",
                                               "reply:weighted_draw"}
        # a member clear of the 60 s per-person gap plays a bit
        quiet = next(g for g in (102, 103, 105, 101) if all(
            s.guid != g for t, s in r.sent.timed_says() if clock.now() - t < 60))
        bit = r.engine.personas.get(quiet).bits[0]
        task = asyncio.create_task(r.engine.speak(Turn(
            kind=TurnKind.BIT, speaker=quiet, channel="guild", bit=bit, max_lines=1)))
        await r.advance(30)
        assert task.result()
        said, = rows("bit")
        assert (said["bit"], said["why"], said["dropped"]) == (bit.id, "speak", None)
        again = asyncio.create_task(r.engine.speak(Turn(kind=TurnKind.OPEN, speaker=quiet,
                                                        channel="guild")))
        await r.advance(5)
        assert again.result() == []
        dropped = rows("open")[-1]
        assert (dropped["why"], dropped["dropped"]) == ("speak", "guards")
        audiences = store.conn.execute(
            "SELECT audience FROM chat_line WHERE origin = 'mind'").fetchall()
        assert audiences and all(json.loads(a[0]) == [101, 102, 103, 104, 105]
                                 for a in audiences)
    run(main())


def test_the_first_greeting_waits_5_to_12_s(store, clock, tmp_path):
    async def main():
        delays = []
        for seed in range(30):
            r = make_rig(store, clock, tmp_path, seed=seed)
            await r.event(Login(ts=0, guid=HUMAN, human=True))
            await asyncio.sleep(0)  # the job starts on its first turn
            (first,) = r.engine.pending_turns()
            delays.append(first.delay)
            await r.engine.end_session()
        assert all(5 <= d <= 12 for d in delays) and max(delays) - min(delays) > 4
    run(main())


def test_a_login_greets_once_whatever_the_order(store, clock, tmp_path):
    """Ruling 5 without depending on the app's order: the event may come before the session,
    and a replayed login in the same stretch greets nobody twice."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        await r.event(Login(ts=0, guid=HUMAN, human=True))  # before any session
        await r.event(Login(ts=0, guid=HUMAN, human=True))  # replayed, still before it
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))  # and again, inside it
        await r.advance(120)
        greeted = len(r.sent.says())
        assert 2 <= greeted <= 3
        await r.event(Logout(ts=0, guid=HUMAN, human=True))  # his logout ends the stretch
        await r.advance(60)
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(120)
        assert len(r.sent.says()) > greeted
        greeted = len(r.sent.says())
        await r.engine.end_session()  # and so does the session's end
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(120)
        assert len(r.sent.says()) > greeted
    run(main())


def test_combat_silences_and_cancels_pending(store, clock, tmp_path):
    async def main():
        r = await raid_rig(store, clock, tmp_path)
        for _ in range(200):  # trash: the budget plans a raid-channel line within 150 s
            await r.advance(1)
            await r.engine.tick(clock.now())
            if r.engine.pending_turns():
                break
        assert r.engine.pending_turns() and r.sent.says() == []
        await r.event(boss("engaged"))
        assert r.engine.pending_turns() == []
        for _ in range(60):
            await r.advance(1)
            await r.engine.tick(clock.now())
        assert r.sent.says() == []
        task = asyncio.create_task(
            r.engine.say_as(102, "raid", "spread out now", kind=TurnKind.URGENT))
        await r.advance(15)
        assert task.result() is True
        assert [(s.guid, s.channel) for s in r.sent.says()] == [(102, "raid")]
    run(main())


def test_a_pull_cancels_the_refused_turn_not_the_ones_behind_it(store, clock, tmp_path):
    """Coros (no officer) answers first and the raid lead after him, in one job. The pull
    refuses Coros's reply; the lead's, which the raid rules still allow, is spoken."""
    async def main():
        r = await raid_rig(store, clock, tmp_path)
        await r.human("coros and belra you ready?", channel="raid")
        await r.advance(1)
        assert [t.speaker for t in r.engine.pending_turns()] == [103]
        await r.event(boss("engaged"))
        await r.advance(40)
        said = {s.guid for s in r.sent.says()}
        assert 102 in said and 103 not in said  # officers (102, 104) may answer in a fight
        assert [u["dropped"] for u in r.utterances() if u["guid"] == 103] == ["cancelled"]
    run(main())


def test_a_cancelled_turn_holds_the_send_lock_until_its_line_settles(store, clock, tmp_path):
    """Coros's line is with the bridge when the pull cancels his turn. The lead's call waits
    for that line to settle, and Coros's turn leaves one row: what he said."""
    gate, sent = asyncio.Event(), []

    async def slow_send(cmd):
        sent.append(cmd)
        if cmd.guid == 103:
            await gate.wait()
        return Ack(ts=0, id=cmd.id, ok=True, error="")

    async def main():
        r = await raid_rig(store, clock, tmp_path, send=slow_send)
        await r.human("coros you ready?", channel="raid")
        for _ in range(30):
            await r.advance(1)
            if sent:
                break
        assert [c.guid for c in sent] == [103]
        await r.event(boss("engaged"))
        call = asyncio.create_task(
            r.engine.say_as(102, "raid", "spread out now", kind=TurnKind.URGENT))
        await r.advance(10)
        assert [c.guid for c in sent] == [103]  # the lock is still his
        gate.set()
        await r.advance(10)
        assert call.result() and [c.guid for c in sent][:2] == [103, 102]
        assert [u["dropped"] for u in r.utterances() if u["guid"] == 103] == [None]
    run(main())


def test_a_line_said_as_its_turn_is_cancelled_is_still_her_own(store, clock, tmp_path):
    """Task 16 carry: a turn cancelled while its line was with the bridge skipped the own-line
    record, so the next prompt and the repeat check never saw what was said."""
    gate, sent = asyncio.Event(), []

    async def slow_send(cmd):
        sent.append(cmd)
        if cmd.guid == 103:
            await gate.wait()
        return Ack(ts=0, id=cmd.id, ok=True, error="")

    async def main():
        seed_test_cast(store)
        save_member(store, 103, typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0)
        r = await raid_rig(store, clock, tmp_path, send=slow_send, reply="ready when you are")
        await r.human("coros you ready?", channel="raid")
        for _ in range(30):
            await r.advance(1)
            if sent:
                break
        assert [c.guid for c in sent] == [103]
        await r.event(boss("engaged"))  # cancels his turn while the line is out
        gate.set()
        await r.advance(10)
        assert [u["dropped"] for u in r.utterances() if u["guid"] == 103] == [None]
        assert [text for _, _, text in r.engine._own[103]] == ["ready when you are"]
    run(main())


def test_an_achievement_in_a_fight_is_congratulated_after_it(store, clock, tmp_path):
    async def main():
        r = await raid_rig(store, clock, tmp_path)
        await r.event(boss("engaged"))
        await r.event(Achievement(ts=0, guid=103, achievement_id=7, name="Glory"))
        for _ in range(30):
            await r.advance(1)
            await r.engine.tick(clock.now())
        assert r.sent.says() == []  # not in the fight
        await r.event(boss("wiped"))
        for _ in range(80):  # the silence, the debrief, then trash
            await r.advance(1)
            await r.engine.tick(clock.now())
        gz = [u for u in r.utterances() if u["kind"] == "gz" and u["dropped"] is None]
        assert gz and all(u["channel"] == "guild" and u["guid"] != 103 for u in gz)
    run(main())


def test_kill_burst_and_emotes(store, clock, tmp_path):
    async def main():
        r = await raid_rig(store, clock, tmp_path)
        await r.event(boss("engaged"))
        await r.advance(30)
        t0 = clock.now()
        await r.event(boss("killed"))
        await r.advance(60)
        says = r.sent.timed_says()
        assert says[0][1].guid == 102 and r.sent_kinds()[0] == (102, "kill")
        assert "Lord Marrowgar" in r.prompts()[0]
        assert 3 <= len(says) <= 6
        assert all(at - t0 <= 45 and s.channel == "raid" for at, s in says)
        emotes = r.sent.emotes()
        assert 2 <= len(emotes) <= 4 and all(e.emote in ("cheer", "dance") for e in emotes)
        assert {e.guid for e in emotes} <= set(RAID)
    run(main())


def test_a_kill_is_cheered_gg_not_congrats(store, clock, tmp_path):
    """Prompts minor 5: "congrats" and "gratz" read oddly for a boss the guild kills every
    week; after a kill the raid types "gg", "nice" or "ez"."""
    async def main():
        for seed in range(6):
            r = await raid_rig(store, clock, tmp_path, seed=seed)
            await r.event(boss("engaged"))
            await r.advance(30)
            await r.event(boss("killed"))
            await r.advance(60)
            await r.engine.end_session()
        texts = [u["text"] for u in r.utterances() if u["kind"] == "gz" and u["dropped"] is None]
        words = {w.strip("!.:)(").casefold() for t in texts for w in t.split()}
        assert texts and not words & {"congrats", "gratz", "grats", "gz"}
        assert words & {"gg", "nice", "ez"}
    run(main())


def test_mornings_are_not_late_night_for_bits(store, clock, tmp_path):
    """Prompts minor 10: 09:00 counted as the small hours, so a late-night bit fit it."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        await r.advance(8 * 3600, step=3600)  # 04:00
        assert "late_night" in r.engine._bit_context(clock.now())
        await r.advance(5 * 3600, step=3600)  # 09:00
        assert "late_night" not in r.engine._bit_context(clock.now())
    run(main())


def test_wipe_silence_then_debrief(store, clock, tmp_path):
    async def main():
        r = await raid_rig(store, clock, tmp_path)
        await r.event(boss("engaged"))
        await r.advance(20)
        await r.event(boss("wiped"))
        await r.advance(10, step=0.5)
        assert r.sent.says() == []
        await r.advance(60)
        kinds = r.sent_kinds()
        assert kinds[0] == (102, "callout")
        assert [k for _, k in kinds].count("callout") == 1
        assert [k for _, k in kinds].count("wipe_react") <= 2
        reacts = [p for p in r.prompts() if "just wiped on Lord Marrowgar" in p]
        assert reacts and all(NO_JOKE in p for p in reacts)
        for _ in range(2):  # wipes two and three: one joke allowed
            await r.event(boss("engaged"))
            await r.advance(20)
            seen = len(r.prompts())
            await r.event(boss("wiped"))
            await r.advance(70)
        reacts = [p for p in r.prompts()[seen:] if "just wiped on Lord Marrowgar" in p]
        assert sum(JOKE_OK.format(n=3) in p for p in reacts) == 1
    run(main())


def test_when_he_died_first_nobody_else_is_told_they_did(store, clock, tmp_path):
    """Fix round 1 (review I4): with him first on the death list, the second death was told
    "You died first" next to facts saying he did."""
    async def main():
        r = await raid_rig(store, clock, tmp_path, script=numbered())
        await r.event(boss("engaged"))
        await r.advance(20)
        for g in (HUMAN, 103, 101):
            await r.event(Death(ts=0, guid=g, map=631, killer_entry=0))
            await r.advance(1)
        await r.event(boss("wiped"))
        await r.advance(70)
        reacts = [p for p in r.prompts() if "just wiped on Lord Marrowgar" in p]
        assert reacts and all("Zan died first" in p for p in reacts)
        assert not any(DIED_FIRST in p for p in r.prompts())
        rows = store.conn.execute("SELECT why FROM utterance WHERE kind = 'wipe_react'").fetchall()
        assert all(x["why"] == "wipe_react:weighted_draw" for x in rows)
    run(main())


def test_raid_calls_and_reactions_get_the_facts(store, clock, tmp_path):
    """The rehearsal: the debrief invented "healer chainbreaks". The lead's call and the
    reactions are told what happened: the boss, how long the pull lasted, which wipe it is
    tonight and who died first, and not to make mechanics up; the kill the same."""
    async def main():
        r = await raid_rig(store, clock, tmp_path, script=numbered())
        await r.event(boss("engaged"))
        await r.advance(20)
        for g in (103, 101, HUMAN, 102, 104, 105):
            await r.event(Death(ts=0, guid=g, map=631, killer_entry=0))
            await r.advance(1)
        await r.event(boss("wiped"))
        await r.advance(70)
        call = next(p for p in r.prompts() if "You're leading the raid" in p)
        assert "The raid wiped on Lord Marrowgar after 26 seconds" in call
        assert "the first wipe on it tonight" in call and "Coros died first" in call
        assert FACTS_RULE in call and "what went wrong" not in call
        reacts = [p for p in r.prompts() if "just wiped on Lord Marrowgar" in p]
        assert reacts and all("Coros died first" in p for p in reacts)
        # the first reaction is offered to whoever died first (rehearsal 2, judge A 9)
        assert DIED_FIRST in reacts[0] and "You play Coros" in reacts[0]
        assert not any(DIED_FIRST in p for p in reacts[1:])
        for g in (103, 101, HUMAN, 102, 104, 105):
            await r.event(Resurrect(ts=0, guid=g))
        await r.event(boss("engaged"))
        await r.advance(95)
        seen = len(r.prompts())
        await r.event(boss("killed"))
        await r.advance(60)
        kill = next(p for p in r.prompts()[seen:] if "just killed Lord Marrowgar" in p)
        assert "The raid killed Lord Marrowgar in 1:35" in kill
        assert "after 1 wipe on it tonight" in kill and "Nobody died" in kill
        assert KILL_RULE in kill  # what the facts give: no causes, no blame
    run(main())


GRIM_TACTICS = [
    chunk("Lord Marrowgar", "- Bone Whirl: Spins and hits everyone close.\n- Frost Lance: "
          "Impales a random player.", kind="strategy", section="Abilities"),
    chunk("Lord Marrowgar", "Tanks must stack on each other to soak Bone Whirl. Ranged should "
          "free anyone hit by Frost Lance right away.", kind="strategy", section="Strategy")]


def test_the_raid_leads_calls_carry_the_bosss_real_mechanics(store, clock, tmp_path):
    """The rehearsal's lead made up "healers stack on me every 30s" on Marrowgar. With the
    library his wipe debrief and his kill line get the boss's mechanics from its tactics page
    next to what happened, and are told to invent nothing; the reactions keep the run's facts
    only, and the page is looked up once."""
    async def main():
        lib = FakeLibrary(strategy=GRIM_TACTICS)
        r = await raid_rig(store, clock, tmp_path, script=numbered(), library=lib)
        await r.event(boss("engaged"))
        await r.advance(20)
        for g in (103, 101, HUMAN, 102, 104, 105):
            await r.event(Death(ts=0, guid=g, map=631, killer_entry=0))
            await r.advance(1)
        await r.event(boss("wiped"))
        await r.advance(70)
        call = next(p for p in r.prompts() if "You're leading the raid" in p)
        assert call.count("The raid wiped on Lord Marrowgar after 26 seconds") == 1
        assert "Coros died first" in call and FACTS_RULE not in call
        assert ("Lord Marrowgar's real mechanics: Tanks must stack on each other to soak Bone "
                "Whirl. Ranged should free anyone hit by Frost Lance right away.") in call
        # judge B 6: it knows the time and who died first, not why
        assert WIPE_KNOWN in call and MECHANICS_RULE in call
        assert call.index(WIPE_KNOWN) < call.index("real mechanics:")
        reacts = [p for p in r.prompts() if "just wiped on Lord Marrowgar" in p]
        assert reacts and all("Coros died first" in p and "real mechanics" not in p
                              for p in reacts)
        for g in (103, 101, HUMAN, 102, 104, 105):
            await r.event(Resurrect(ts=0, guid=g))
        await r.event(boss("engaged"))
        await r.advance(95)
        seen = len(r.prompts())
        await r.event(boss("killed"))
        await r.advance(60)
        kill = next(p for p in r.prompts()[seen:] if "just killed Lord Marrowgar" in p)
        assert "The raid killed Lord Marrowgar in 1:35" in kill
        # judge B 6: the kill call has the time, the wipes and the deaths, no mechanics block
        assert "real mechanics" not in kill and KILL_RULE in kill
        assert lib.strategy_calls == [("Lord Marrowgar", "Icecrown Citadel")]
    run(main())


def test_an_encounter_named_in_the_plural_takes_a_plain_apostrophe(store, clock, tmp_path):
    """Re-review nit (c): "the Northrend Beasts's real mechanics"."""
    async def main():
        beasts = [chunk("Icehowl", "Everyone should move away from Icehowl's charge path at "
                        "once.", kind="strategy", section="Strategy")]
        lib = FakeLibrary(strategy={"Icehowl": beasts})
        r = await raid_rig(store, clock, tmp_path, script=numbered(), library=lib)
        toc = Boss(ts=0, map=649, instance_id=6, boss_id=0, boss_name="", state="engaged",
                   members=RAID)
        await r.event(toc)
        await r.advance(20)
        await r.event(replace(toc, state="wiped"))
        await r.advance(70)
        call = next(p for p in r.prompts() if "You're leading the raid" in p)
        assert "the Northrend Beasts' real mechanics: Everyone should move away" in call
    run(main())


def test_a_boss_without_a_tactics_page_keeps_the_rule_against_made_up_mechanics(store, clock,
                                                                              tmp_path):
    async def main():
        r = await raid_rig(store, clock, tmp_path, script=numbered(), library=FakeLibrary())
        await r.event(boss("engaged"))
        await r.advance(20)
        await r.event(boss("wiped"))
        await r.advance(70)
        call = next(p for p in r.prompts() if "You're leading the raid" in p)
        assert FACTS_RULE in call and "real mechanics" not in call
    run(main())


def test_nobody_is_told_they_lead_a_raid_they_do_not(store, clock, tmp_path):
    """Prompts minor 3: he leads and no officer is in the group, so there is no raid lead:
    no member makes the lead's debrief call; the wipe still gets its reactions."""
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered())
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101, 103, 105)))
        await r.event(boss("engaged"))
        await r.advance(20)
        await r.event(boss("wiped"))
        await r.advance(70)
        assert not any("You're leading the raid" in p for p in r.prompts())
        assert "wipe_react" in [k for _, k in r.sent_kinds()]
    run(main())


def test_romance_never_toward_members(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="drawn", style="bold")
        r = make_rig(store, clock, tmp_path)
        to_member, _ = await r.engine.compose(
            Turn(kind=TurnKind.CONTINUE, speaker=102, channel="guild", addressee=101))
        assert ROMANCE not in "\n".join(m["content"] for m in to_member)
        to_him, _ = await r.engine.compose(
            Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN))
        assert ROMANCE in "\n".join(m["content"] for m in to_him)  # positive control
    run(main())


def test_romance_text_rides_a_share_of_her_turns_by_attitude(store, clock, tmp_path):
    """Task 15 carry: not every turn to him carries it, so fifteen lust women don't read
    one-note; the share follows the attitude (and a man never gets it)."""
    async def main():
        seed_test_cast(store)
        for g, stance in ((102, "lust"), (103, "drawn"), (104, "fond")):
            save_member(store, g, player_gender="female", stance=stance, style="bold")
        r = make_rig(store, clock, tmp_path)
        carried, overlay = {}, {}
        for g in (102, 103, 104, 105):
            carried[g] = overlay[g] = 0
            for _ in range(60):
                msgs, _ = await r.engine.compose(
                    Turn(kind=TurnKind.REPLY_HUMAN, speaker=g, channel="guild", addressee=HUMAN))
                carried[g] += ROMANCE in msgs[1]["content"]
                overlay[g] += OVERLAY in msgs[1]["content"]
        assert 0 < carried[104] < carried[103] < carried[102] < 60 and carried[105] == 0
        # at dial 9 only lust reaches the explicit band, whose wording is the local overlay
        assert overlay[102] == carried[102] and overlay[103] == overlay[104] == 0
    run(main())


def test_her_card_for_him_carries_her_attitude_every_turn(store, clock, tmp_path):
    """Prompts I4: her romance text rides only a share of her turns to him; her card says it
    on every one, and a man's card says nothing of the kind."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", style="bold")
        r = make_rig(store, clock, tmp_path)
        carried = 0
        for _ in range(30):
            msgs, _ = await r.engine.compose(
                Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN))
            people = msgs[1]["content"].split("People here:")[1].split("\n")[1]
            assert people.startswith("Zan (the guild master): you want him")
            carried += ROMANCE in msgs[1]["content"]
        assert 0 < carried < 30  # the romance text itself still varies
        msgs, _ = await r.engine.compose(
            Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild", addressee=HUMAN))
        assert "want him" not in msgs[1]["content"] and "into him" not in msgs[1]["content"]
    run(main())


def test_openers_bring_up_something_of_their_own(store, clock, tmp_path):
    """Prompts I9: an opener's only material was the card and the same few memories. Each
    OPEN gets a concrete situation from the member's own life and from tonight (rehearsal 2,
    judge A 2: never a bare label like a like or a dislike), chosen the same way on a re-run
    but different from one opener to the next tonight."""
    async def main():
        seed_test_cast(store)
        save_member(store, 101, likes=["vintage synths", "night fishing"],
                    dislikes=["queue jumpers"], wants=["the Ashes of Al'ar"],
                    outside_raids=["auction_house"], life_thread="training for a marathon")
        r = make_rig(store, clock, tmp_path)
        r.engine.bits.p_fire = 0.0

        def topic():
            _, meta = r.engine._social_turn(TurnKind.OPEN, 101, "guild", addressee=None,
                                            trigger=None, room=3, now=clock.now(), delay=0.0)
            return meta.topic
        topics = [topic() for _ in range(6)]
        assert len(set(topics)) >= 4 and all(topics)
        material = ("Ashes of Al'ar", "auction house", "marathon", "flatmate", "warehouse",
                    "Hallow's End")
        assert sum(any(m in t for m in material) for t in topics) >= 4
        labels = ("vintage synths", "night fishing", "queue jumpers", "old harbour town")
        assert not any(m in t for m in labels for t in topics)
        again = make_rig(store, clock, tmp_path)
        again.engine.bits.p_fire = 0.0
        assert [again.engine._social_turn(TurnKind.OPEN, 101, "guild", addressee=None,
                                          trigger=None, room=3, now=clock.now(),
                                          delay=0.0)[1].topic for _ in range(6)] == topics
    run(main())


def test_an_opener_knows_what_she_did_tonight(store, clock, tmp_path):
    """Spec I3 and its ruling: a compact "tonight so far" from the member's own game events."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        await r.event(Zone(ts=0, guid=101, zone_id=4395, zone_name="Dalaran"))
        await r.event(LootWon(ts=0, guid=101, item_id=50415, name="Bryntroll", method="need"))
        await r.event(Achievement(ts=0, guid=101, achievement_id=9, name="Glory of the Hero"))
        await r.event(LootWon(ts=0, guid=102, item_id=1, name="Not Hers", method="need"))
        msgs, _ = await r.engine.compose(Turn(kind=TurnKind.OPEN, speaker=101, channel="guild"))
        now = msgs[1]["content"].split("Right now:")[1].split("\n\n")[0]
        assert "Tonight so far:" in now and "you won Bryntroll" in now
        assert "you earned Glory of the Hero" in now and "you were in Dalaran" in now
        assert "Not Hers" not in now
        reply, _ = await r.engine.compose(Turn(kind=TurnKind.REPLY_HUMAN, speaker=101,
                                               channel="guild", addressee=HUMAN))
        assert "Tonight so far" not in reply[1]["content"]
        await r.advance(4 * 3600, step=3600)  # a stale evening is no longer "tonight"
        msgs, _ = await r.engine.compose(Turn(kind=TurnKind.OPEN, speaker=101, channel="guild"))
        assert "Tonight so far" not in msgs[1]["content"]
    run(main())


def test_her_answer_to_his_question_ends_on_answering_him(store, clock, tmp_path):
    """Prompts I5: on a reply to him her romance text comes first and the reply's own
    instruction last, so she answers what he asked before she flirts."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", style="bold")
        r = make_rig(store, clock, tmp_path)
        seen = 0
        for _ in range(20):
            msgs, _ = await r.engine.compose(
                Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN))
            last = msgs[1]["content"].rsplit("\n\n", 1)[1]
            assert last.endswith("Bring in a memory or a running joke only if it really fits.")
            if ROMANCE in last:
                seen += 1
                assert last.index(ROMANCE) < last.index("Answer Zan's last line")
        assert seen
    run(main())


def test_his_loot_is_cheered_by_the_women_who_want_him(store, clock, tmp_path):
    """Controller ruling: drawn or lust women first (lust weighted above drawn); her cheer is
    in her own words and feeds the men's loot_cheer asides."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", style="bold")
        for g, mode in ((101, "teaser"), (103, "grumbler"), (104, "play_by_play")):
            save_member(store, g, jealousy_mode=mode)
        picked = {g: 0 for g in RAID[1:]}
        asides = 0
        talk = numbered("gz zan, take {n}")  # her cheer names him: the asides follow it
        for seed in range(20):
            r = await raid_rig(store, clock, tmp_path, seed=seed, jealousy=10, script=talk)
            first = store.conn.execute("SELECT coalesce(max(id), 0) FROM utterance").fetchone()[0]
            await r.event(LootWon(ts=0, guid=HUMAN, item_id=50415, name="Bryntroll",
                                  method="need"))
            await r.advance(60)
            says = r.sent.says()
            assert 1 <= sum(s.channel == "raid" for s in says)
            cheers = store.conn.execute(  # the gz, not the men's asides that follow hers
                "SELECT DISTINCT guid FROM utterance WHERE id > ? AND kind = 'gz' AND "
                "channel = 'raid' AND dropped IS NULL", (first,)).fetchall()
            for (g,) in cheers:
                picked[g] += 1
            cheer = [p for p in r.prompts() if "You play Belra" in p and "just got Bryntroll" in p]
            assert all(ROMANCE in p for p in cheer)
            asides += any("cheering Zan's loot" in p for p in r.prompts())
            await r.engine.end_session()
        assert picked[102] > max(n for g, n in picked.items() if g != 102)
        assert asides >= 1
    run(main())


def test_his_goodnight_mid_raid_tells_the_raid_and_the_lead(store, clock, tmp_path):
    """Rehearsal 2 (judge A 9): he said goodnight while leading a formed ICC raid after one
    boss, and nobody, the raid lead included, said "we calling it?"."""
    async def main():
        r = await raid_rig(store, clock, tmp_path, script=numbered("night zan, take {n}"))
        await r.human("night all, gotta sleep")
        await r.advance(60)
        nights = [p for p in r.prompts() if "is logging off" in p]
        assert nights and all("Zan is leaving mid-raid" in p or "leaving mid-raid. You lead"
                              in p for p in nights)
        lead = [p for p in nights if "You play Belra" in p]  # the group's leader leads it
        assert len(lead) == 1 and "You lead the raid: say what happens now" in lead[0]
        assert (102, "goodnight") in r.sent_kinds()
    run(main())


def test_the_first_cheer_for_his_loot_names_the_item(store, clock, tmp_path):
    """Rehearsal 2 (judge A 9): his bracers got only templates. The first cheer is a member's
    own words and names the item, whoever gives it."""
    async def main():
        for seed in range(6):
            r = await raid_rig(store, clock, tmp_path, seed=seed, script=numbered("gz {n}"))
            await r.event(LootWon(ts=0, guid=HUMAN, item_id=50415, name="Bryntroll",
                                  method="need"))
            await r.advance(30)
            named = [p for p in r.prompts() if "Say which item it is: Bryntroll." in p]
            assert len(named) == 1, seed
            await r.engine.end_session()
    run(main())


def test_asides_follow_what_was_sent(store, clock, tmp_path):
    """Rehearsal 2 (judge C 6-8): only FLIRT turns set off the men's asides, so her public
    "miss me?" to him set off nothing, while a flirt that landed on a member did. Any public
    line that carried her romance text and names him does now; one that doesn't name him
    doesn't."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", style="bold")
        for g in (101, 103, 104, 105):
            save_member(store, g, jealousy_mode="teaser")
        hits, hers = 0, numbered("evening zan, miss me? take {n}")
        for seed in range(6):
            r = make_rig(store, clock, tmp_path, jealousy=10, seed=seed, script=lambda m, msgs: (
                "note" if "flirting with Zan in chat" in msgs[1]["content"]
                else hers(m, msgs)))
            line = his_line_in(r, store, "hey you ;) back again")
            task = asyncio.create_task(r.engine.speak(Turn(
                kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN,
                trigger_line=line)))
            await r.advance(60)
            assert task.result()
            hits += any("flirting with Zan in chat" in p for p in r.prompts())
            await r.engine.end_session()
        assert hits >= 3
        r = make_rig(store, clock, tmp_path, jealousy=10, reply="come home to me after you get it")
        task = asyncio.create_task(r.engine.speak(Turn(kind=TurnKind.FLIRT, speaker=102,
                                                       channel="guild", addressee=HUMAN)))
        await r.advance(60)
        assert task.result() and not any("flirting with Zan" in p for p in r.prompts())
    run(main())


def his_line_in(r, store, text: str) -> int:
    from leeroy_mind.protocol import Chat
    row = store.chat_lines([store.insert_chat_line(Chat(
        ts=0, channel="guild", from_guid=HUMAN, from_name="Testmage", to_guid=0, text=text,
        map=0, zone=10), session_id=None, origin="human")])[0]
    r.world.add_line(row, r.clock.now())
    return row.id


def test_a_woman_who_just_spoke_still_cheers_his_loot(store, clock, tmp_path):
    """Rehearsal 2 (judge C 6): right after a kill the women had just spoken in raid, and the
    recency penalty cancelled their boost: two men's templates cheered his loot."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", style="bold")
        cheered, talk = 0, numbered("gz zan {n}")
        for seed in range(40):
            r = await raid_rig(store, clock, tmp_path, seed=seed, script=talk)
            task = asyncio.create_task(r.engine.speak(Turn(kind=TurnKind.OPEN, speaker=102,
                                                           channel="raid")))
            await r.advance(90)  # she spoke a minute and a half ago: her weight was x0.3
            assert task.result()
            first = len(r.sent.says())
            await r.event(LootWon(ts=0, guid=HUMAN, item_id=50415, name="Bryntroll",
                                  method="need"))
            await r.advance(30)
            cheered += any(s.guid == 102 for s in r.sent.says()[first:])
            await r.engine.end_session()
        assert cheered >= 16  # 19 of 40 seeds; held back by the penalty, 11
    run(main())


def test_flirt_triggers_budgeted_aside(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="drawn", style="bold")
        for g, mode in ((101, "teaser"), (103, "sulker"), (104, "good_sport"),
                        (105, "play_by_play")):
            save_member(store, g, jealousy_mode=mode)
        flirts, notes = numbered("zan, take {n}"), numbered("note {n}")
        r = make_rig(store, clock, tmp_path, jealousy=5, seed=4, script=lambda model, msgs: (
            notes if "flirting with Zan in chat" in msgs[1]["content"] else flirts)(model, msgs))

        def asides(start: int = 0) -> tuple[int, int]:
            got = [p for p in r.prompts()[start:] if "flirting with Zan in chat" in p]
            defuses = sum("just got sour about it" in p for p in got)
            return len(got) - defuses, defuses

        flirt = Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN)
        task = asyncio.create_task(r.engine.speak(flirt))
        await r.advance(60)
        assert task.result()
        first, defuse = asides()
        assert first <= 1 and defuse <= first
        for _ in range(18):  # every 3 minutes, all inside the hour of the first flirt
            task = asyncio.create_task(r.engine.speak(
                Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN)))
            await r.advance(180, step=10)
            assert task.result()
        total, defuses = asides()
        assert 1 <= total <= 2  # jealousy 5: round(2 x 5 / 5) asides an hour
        assert defuses <= total
    run(main())


def test_gz_once_per_achievement(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, reply="gz on loremaster")
        await r.session()
        ach = Achievement(ts=0, guid=HUMAN, achievement_id=42, name="Loremaster")
        await r.event(ach)
        await r.advance(60)
        await r.event(ach)
        await r.advance(60)
        says = r.sent.says()
        assert 2 <= len(says) <= 4 and all(s.channel == "guild" for s in says)
        assert "Zan just got Loremaster" in r.prompts()[0]
        await r.event(Achievement(ts=0, guid=103, achievement_id=42, name="Loremaster"))
        await r.advance(60)
        member = r.sent.says()[len(says):]
        assert 1 <= len(member) <= 3 and 103 not in {s.guid for s in member}
        await r.engine.end_session()
        await r.session()  # a new session reacts again
        await r.event(ach)
        await r.advance(60)
        assert len(r.sent.says()) > len(says) + len(member)
    run(main())


def test_members_gz_each_other_only_while_he_is_on(store, clock, tmp_path):
    """Liveops M9: at 5.8 bot achievements an hour, member gz ran round the clock (16 lines in
    an hour he was offline, each a chat_line and utterance row) for nobody to read."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        await r.event(Logout(ts=0, guid=HUMAN, human=True))
        await r.event(Achievement(ts=0, guid=103, achievement_id=7, name="Glory"))
        await r.advance(60)
        assert r.sent.says() == [] and r.utterances() == []
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(400)  # past the login's hellos and quiet
        seen = len(r.sent.says())
        await r.event(Achievement(ts=0, guid=103, achievement_id=7, name="Glory"))
        await r.advance(60)
        assert r.sent.says()[seen:]  # positive control, and not held from the first time
    run(main())


def test_register_source_turns_are_spoken(store, clock, tmp_path):
    class Calls:
        def __init__(self):
            self.polls = 0

        async def propose(self, now):
            self.polls += 1
            if self.polls == 1:
                return [Turn(kind=TurnKind.CALLOUT, speaker=102, channel="raid")]
            return []

    async def main():
        r = await raid_rig(store, clock, tmp_path, reply="pull on my mark")
        src = Calls()
        r.engine.register_source(src)
        await r.engine.tick(clock.now())
        await r.advance(30)
        assert src.polls == 1
        assert [(s.guid, s.channel) for s in r.sent.says()] == [(102, "raid")]
        assert "You're leading the raid" in r.prompts()[0]
    run(main())


def test_a_burst_speaker_can_play_a_bit(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, chattiness=10, reply="the weather is grim again")
        r.engine.bits.p_fire = 1.0
        for _ in range(400):
            await r.advance(1)
            await r.engine.tick(clock.now())
            if r.sent.says():
                break
        guid, kind = r.sent_kinds()[0]
        assert kind == "bit" and "Your running bit, the way you usually do it:" in r.prompts()[0]
        assert store.last_bit_use(guid, f"bit{guid}") is not None
    run(main())


def test_every_prompt_knows_the_day_and_hallows_end(store, clock, tmp_path):
    """Rehearsal 2 (judge A 5): on Halloween nobody mentioned Halloween; "Right now:" had only
    the time of day. The test clock's night is Thursday 2026-10-29, inside Hallow's End."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        msgs, _ = await r.engine.compose(Turn(kind=TurnKind.OPEN, speaker=101, channel="guild"))
        assert "Right now:\nIt's Thursday evening. Hallow's End is on in the game." in \
            msgs[1]["content"]
        topics = [r.engine._open_topic(r.engine.personas.get(101), clock.now())
                  for _ in range(30)]
        assert any("Hallow's End in game" in t for t in topics)
    run(main())


BIRTHDAY_BIT = {"id": "birthday_announce", "instruction": "announce a member's birthday",
                "min_days_between": 0.0, "voice_bit": False, "contexts": ["any"], "lines": 1,
                "dates": []}


def test_a_birthday_bit_waits_for_a_real_birthday_and_hugs_as_an_emote(store, clock, tmp_path):
    """Rehearsal 2, finding 3: a member's birthday bit fired on a night that was nobody's birthday
    and typed "/hug /hug /hug" into chat. It needs a cast member whose card says tonight is
    their birthday, names them, and the hug goes out as an emote at them."""
    from leeroy_mind.personas.schema import Bit

    async def main():
        seed_test_cast(store)
        bit = Bit(**{**BIRTHDAY_BIT, "contexts": tuple(BIRTHDAY_BIT["contexts"]),
                     "dates": tuple(BIRTHDAY_BIT["dates"])})
        save_member(store, 102, bits=[BIRTHDAY_BIT], typo_rate=0.0, emoticon_rate=0.0,
                    split_rate=0.0, abbreviations={})
        r = make_rig(store, clock, tmp_path, reply="happy birthday dunna! /hug /hug /hug")
        r.engine.bits.p_fire = 1.0
        for _ in range(40):  # nobody's birthday tonight: never
            turn, _ = r.engine._social_turn(TurnKind.OPEN, 102, "guild", addressee=None,
                                            trigger=None, room=2, now=clock.now(), delay=0.0)
            assert turn.kind is not TurnKind.BIT
        save_member(store, 104, birthday="10-29")  # tonight is Dunna's
        r = make_rig(store, clock, tmp_path, reply="happy birthday dunna! /hug /hug /hug")
        turn, meta = next(t for t in (r.engine._social_turn(
            TurnKind.OPEN, 102, "guild", addressee=None, trigger=None, room=2, now=clock.now(),
            delay=0.0) for _ in range(40)) if t[0].kind is TurnKind.BIT)
        assert turn.bit == bit and "Tonight is Dunna's birthday" in turn.instruction
        task = asyncio.create_task(r.engine._speak(turn, meta))
        await r.advance(30)
        assert task.result() == ["happy birthday dunna"]  # no "/hug" typed into chat
        (hug,) = r.sent.emotes()
        assert (hug.guid, hug.emote, hug.target_guid) == (102, "hug", 104)
        # the same bit dated another day stays quiet on Dunna's birthday (fix round 1, I6);
        # its own id, so "once a night" doesn't hide it
        dated = {**BIRTHDAY_BIT, "id": "birthday_dated", "dates": ["06-03"]}
        save_member(store, 102, bits=[dated], typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0,
                    abbreviations={})
        r = make_rig(store, clock, tmp_path)
        r.engine.bits.p_fire = 1.0
        assert all(r.engine._social_turn(TurnKind.OPEN, 102, "guild", addressee=None,
                                         trigger=None, room=2, now=clock.now(),
                                         delay=0.0)[0].kind is not TurnKind.BIT
                   for _ in range(40))
    run(main())


def test_a_birthday_most_of_the_guild_shares_is_named_the_same_all_night(store, clock,
                                                                          tmp_path):
    """Fix round 1 (review I6): when most members share a birthday, they share the night.
    The bit names one or two of them and counts the rest, the same ones whoever speaks and
    whoever is online."""
    async def main():
        seed_test_cast(store)
        for g in (101, 103, 104, 105):
            save_member(store, g, birthday="10-29")
        for g in range(201, 205):
            save_persona(store, make_persona(g, f"Mem{g}", birthday="10-29"), source="seed",
                         ts=1000.0, race="Orc", class_name="Warrior")
        r = make_rig(store, clock, tmp_path)
        line, _ = r.engine._birthday_tonight(102)
        assert line.startswith("Tonight is the birthday of ") and "and 6 others in the guild" \
            in line and line.endswith("wish them all a happy birthday.")
        r.world.online.discard(101)
        assert r.engine._birthday_tonight(102)[0] == line  # the same whoever is online
        named = line.removeprefix("Tonight is the birthday of ").split(" and 6")[0].split(", ")
        speaker = next(g for g in (101, 103, 104, 105)
                       if r.engine.personas.get(g).character_name not in named)
        assert r.engine._birthday_tonight(speaker)[0].split(" and ")[0] == line.split(" and ")[0]
    run(main())


def test_burst_turns_flirt_only_for_drawn_and_lust_women(store, clock, tmp_path):
    """Controller ruling: an OPEN or CONTINUE turn by a drawn or lust woman becomes a FLIRT at
    the human (p = min(0.9, base x candour)) while he is active; fond women never."""
    async def kinds(stance: str) -> list[str]:
        save_member(store, 102, player_gender="female", stance=stance, style="bold")
        first = store.conn.execute("SELECT coalesce(max(id), 0) FROM utterance").fetchone()[0]
        r = make_rig(store, clock, tmp_path, online=(102,), chattiness=10,
                     script=numbered("thought number {n} for zan"))
        r.engine.bits.p_fire = 0.0
        for i in range(450):  # half an hour; he stays active
            await r.advance(4)
            if i % 60 == 0:
                r.world.apply(Zone(ts=0, guid=HUMAN, zone_id=10, zone_name="Dalaran"),
                              clock.now())
            await r.engine.tick(clock.now())
        await r.engine.end_session()
        return [x["kind"] for x in store.conn.execute(
            "SELECT kind FROM utterance WHERE id > ? AND dropped IS NULL", (first,))]

    async def main():
        seed_test_cast(store)
        lust = await kinds("lust")
        assert "flirt" in lust and set(lust) <= {"flirt", "open"}
        fond = await kinds("fond")
        assert fond and set(fond) == {"open"}
    run(main())


def test_whisper_planner_runs_once_per_quiet_stretch(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", style="bold")
        r = make_rig(store, clock, tmp_path, chattiness=0)
        calls = []
        maybe = r.engine.whispers.maybe

        def spy(now, trigger, candidates):
            calls.append(trigger)
            return maybe(now, trigger, candidates)

        r.engine.whispers.maybe = spy
        await r.session()
        t0, idle_at = clock.now(), None
        for _ in range(120):  # an hour of quiet
            await r.advance(30, step=30)
            await r.engine.tick(clock.now())
            if calls and idle_at is None:
                idle_at = clock.now() - t0
        assert 600 <= idle_at < 640  # 10 minutes of quiet (fix wave G1: was 15)
        # and the evening's one draw, 30-45 minutes into his session (fix wave G1)
        assert calls == ["idle", "evening"]
        await r.human("ok back, what did i miss")
        for _ in range(60):
            await r.advance(30, step=30)
            await r.engine.tick(clock.now())
        assert calls == ["idle", "evening", "idle"]
    run(main())


def test_the_evening_whisper_comes_after_his_loot_and_takes_the_late_hours_place(
        store, clock, tmp_path):
    """Rehearsal 2 (judge C 6): no woman whispered him first in 65 minutes. The evening's
    trigger draws once a session, after his loot or 30-45 minutes in, and the late hour
    doesn't draw again that night: one more chance, not two."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", style="bold")
        r = await raid_rig(store, clock, tmp_path, chattiness=0, script=numbered())
        calls = []
        maybe = r.engine.whispers.maybe

        def spy(now, trigger, candidates):
            calls.append(trigger)
            return maybe(now, trigger, candidates)
        r.engine.whispers.maybe = spy
        await r.session()
        await r.advance(200)  # past his login's quiet
        await r.event(LootWon(ts=0, guid=HUMAN, item_id=50415, name="Bryntroll",
                              method="need"))
        await r.event(LootWon(ts=0, guid=HUMAN, item_id=50416, name="Bracers", method="need"))
        assert calls == ["evening"]
        for _ in range(240):  # three hours: into the late hour, after 23:00
            await r.advance(60, step=60)
            await r.engine.tick(clock.now())
        assert "late" not in calls and calls.count("evening") == 1
    run(main())


def test_her_cheer_with_his_name_as_a_possessive_sets_off_asides(store, clock, tmp_path):
    """Fix round 1 (review minor 9): "gz on zan's new axe" named him, but the check took
    "zan's" for another word, so no aside followed it."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        assert r.engine._names_him("gz on zan's new axe")
        assert r.engine._names_him("ZAN you legend") and not r.engine._names_him("zanzibar")
    run(main())


def test_a_late_login_gets_one_whisper_draw_not_two(store, clock, tmp_path):
    """Fix round 1 (review minor 3): logging in after 23:00 drew "late" and then "evening"
    within the hour, against "one draw, not two"."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", style="bold")
        await clock.advance(3.5 * 3600, 600)  # 23:30, the late hour
        r = make_rig(store, clock, tmp_path, chattiness=0)
        calls = []
        maybe = r.engine.whispers.maybe

        def spy(now, trigger, candidates):
            calls.append(trigger)
            return maybe(now, trigger, candidates)
        r.engine.whispers.maybe = spy
        await r.session()
        for _ in range(60):  # an hour, with his chat keeping the idle trigger off
            await r.advance(60, step=60)
            await r.human("still here")
            await r.engine.tick(clock.now())
        assert calls == ["late"]
    run(main())


def test_a_second_wipe_on_the_boss_makes_a_passive_again_a_true_reaction(store, clock,
                                                                         tmp_path):
    """Fix round 1 (review M3): "killed by the adds again" on the first wipe on a boss tonight
    claims a past nobody remembers (asked again, then dropped); on the second wipe on it, it
    is what happened, and it goes out."""
    hint = "Only bring up the past from what you remember."
    lines = ["killed by the adds again, great", "spiked by the bone spikes again",
             "hit by the whirlwind again", "smashed by the boss again lol"]
    said = [0]

    def script(model, messages):
        if "just wiped on Lord Marrowgar" not in messages[-1]["content"]:
            return "ok"
        said[0] += 1
        return lines[said[0] % len(lines)]

    async def main():
        r = await raid_rig(store, clock, tmp_path, script=script)

        def reacted():
            return [u for u in r.utterances() if u["kind"] == "wipe_react" and u["dropped"] is None]
        await r.event(boss("engaged"))
        await r.advance(20)
        await r.event(boss("wiped"))
        await r.advance(70)
        assert any(hint in p for p in r.prompts()) and reacted() == []
        seen = len(r.prompts())
        await r.event(boss("engaged"))
        await r.advance(20)
        await r.event(boss("wiped"))
        await r.advance(70)
        second = [p for p in r.prompts()[seen:] if "just wiped on Lord Marrowgar" in p]
        assert second and not any(hint in p for p in second)
        assert reacted() and all("again" in u["text"] for u in reacted())
    run(main())
