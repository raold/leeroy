"""Members answer him in the guild's Discord (discord.talk): who answers a post of his (a DM's
member, the members he @mentions, else one or two drawn by presence, warmth and fit), the
answer turns' prompt (the channel's purpose and its recent messages, only what the speaker's
audience reads), the 120 s deadline, a DM's first "(nothing)" asked again, an emoji-only answer,
and his `discord` lease on the GPU, released ten minutes after his last action. Invented text."""
import asyncio
import random
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import HUMAN, OFFICERS, TEST_CAST, make_mem, seed_test_cast
from engine_rig import NICK, make_rig, numbered, save_member
from leeroy_mind.discord import store as dstore
from leeroy_mind.bridge import ConnectionLost
from leeroy_mind.discord.desk import DiscordDesk
from leeroy_mind.discord.store import messages
from leeroy_mind.discord.talk import LEASE, LEASE_IDLE_S, DiscordTalk, answer_delay
from leeroy_mind.endpoint import discord_args
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.nightly.gpu import GpuArbiter
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_clock import FakeClock

US = tuple(TEST_CAST)  # in the US at 20:00 on a Thursday: online in Discord
REFL = "reflection-27b:latest"


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 29, 20, tzinfo=ZoneInfo("America/New_York")), yields=64)


class FakeAdmin:
    """Ollama's model admin as the arbiter uses it: `loaded` is what /api/ps lists, and each
    unload and preload is recorded in `calls`."""

    def __init__(self, loaded):
        self.loaded = list(loaded)
        self.calls: list[tuple] = []

    async def loaded_models(self):
        return list(self.loaded)

    async def unload(self, model):
        self.calls.append(("unload", model))
        if model in self.loaded:
            self.loaded.remove(model)

    async def preload(self, model, keep_alive, *, options=None):
        self.calls.append(("preload", model, keep_alive))
        if model not in self.loaded:
            self.loaded.append(model)


class FakeArbiter:
    """The GPU arbiter as the Discord talk uses it, recording each acquire, hand-over and
    release."""

    def __init__(self):
        self.log: list[tuple] = []
        self._holders: set[str] = set()

    @property
    def holders(self) -> frozenset[str]:
        return frozenset(self._holders)

    def hold(self, holder: str) -> bool:
        first = not self._holders
        self._holders.add(holder)
        self.log.append(("acquire", holder))
        return first

    async def hand_over(self) -> None:
        self.log.append(("hand_over",))

    async def release_live(self, holder: str) -> None:
        if holder in self._holders:
            self._holders.discard(holder)
            self.log.append(("release", holder))


def cast(store, *, us=US):
    """The test cast, the members in `us` living in the US and the rest in the UK."""
    seed_test_cast(store)
    for g in TEST_CAST:
        save_member(store, g, region="US" if g in us else "UK")


def rig_for(store, clock, tmp_path, *, us=US, **kw):
    cast(store, us=us)
    rig = make_rig(store, clock, tmp_path, **kw)
    rig.world.human_online = False  # out of the game: Discord goes on
    return rig


def talk_for(rig, *, arbiter=None, seed=1, personas=None):
    app = SimpleNamespace(store=rig.store, clock=rig.clock, engine=rig.engine, world=rig.world,
                          personas=personas or rig.engine.personas, human_guid=HUMAN,
                          arbiter=arbiter,
                          realm=SimpleNamespace(cfg=SimpleNamespace(trial_rank=4)),
                          rng=random.Random(seed), stopping=False)
    return DiscordTalk(app)


def post(rig, channel, text, **kw):
    return dstore.post(rig.store, channel=channel, author=HUMAN, origin="human", text=text,
                       now=rig.clock.now(), **kw)


def member_post(rig, guid, channel, text, **kw):
    return dstore.post(rig.store, channel=channel, author=guid, origin="mind", text=text,
                       now=rig.clock.now(), **kw)


def answer(rig, talk, mid, seconds=200.0):
    """His post handed to the talk, as the desk does, then `seconds` for the answers."""
    async def go():
        talk.on_his_post(mid)
        await rig.advance(seconds)
    rig.sync(go())


def answers(rig, channel, mid, **kw):
    return [m for m in messages(rig.store.conn, channel, **kw)
            if m["origin"] == "mind" and m["reply_to"] == mid]


def speakers(talk, mid, n=1):
    """Who would answer his post `mid`, n times over (the talk's own draws)."""
    return [[t.speaker for t, _ in talk.plan(mid)] for _ in range(n)]


def test_his_post_out_of_game_is_answered(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, script=numbered("sure im around {n}"))
    talk = talk_for(rig)
    assert store.conn.execute("SELECT count(*) FROM session").fetchone()[0] == 0  # no session
    mid = post(rig, "general", "who is around tonight")
    answer(rig, talk, mid)
    said = answers(rig, "general", mid)
    assert 1 <= len(said) <= 2
    assert {m["author"] for m in said} <= set(TEST_CAST)
    assert rig.sent.log == []
    rig.close()


def test_the_answer_turns_are_replies_to_him(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    talk = talk_for(rig)
    mid = post(rig, "general", "who is around tonight")
    for turn, meta in talk.plan(mid):
        assert (turn.kind, turn.channel, turn.to_guid, turn.addressee, turn.trigger_line,
                turn.priority) == (TurnKind.REPLY_HUMAN, "discord:general", 0, HUMAN, None,
                                   Priority.REPLY_HUMAN)
        assert (meta.answers_human, meta.why, meta.discord.reply_to, meta.discord.thread) == (
            True, "discord:reply", mid, None)
        assert 2.0 <= turn.delay <= 25.0
    dm = post(rig, "dm:103", "hey you")
    [(turn, meta)] = talk.plan(dm)
    assert (turn.channel, turn.to_guid, turn.addressee) == ("dm:103", HUMAN, HUMAN)
    rig.close()


def test_a_dm_is_answered_by_that_member(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, us=(101, 103, 104, 105), reply="hey, all good here")
    talk = talk_for(rig)
    mid = post(rig, "dm:102", "you around later?")  # she is offline in Discord: 01:00 for her
    assert speakers(talk, mid, 5) == [[102]] * 5
    answer(rig, talk, mid)
    [m] = answers(rig, "dm:102", mid)
    assert m["author"] == 102
    [u] = store.conn.execute("SELECT to_guid FROM utterance WHERE channel = 'dm:102' "
                             "AND dropped IS NULL").fetchall()
    assert u["to_guid"] == HUMAN
    assert all(m["channel"] == "dm:102" for m in messages(store.conn, "dm:102"))
    assert rig.sent.log == []
    rig.close()


def test_a_mention_brings_the_named_members(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, script=numbered("im in {n}"))
    talk = talk_for(rig)
    mid = post(rig, "general", "@arkon and @Coros you two up for heroics?")
    assert speakers(talk, mid, 5) == [[101, 103]] * 5
    answer(rig, talk, mid)
    assert sorted(m["author"] for m in answers(rig, "general", mid)) == [101, 103]
    four = post(rig, "general", "@Ebbo @Dunna @Arkon @Belra roll call")
    assert speakers(talk, four) == [[105, 104, 101]]  # three at most, in his order
    nobody = post(rig, "general", "@Zorblat @Testmage hello?")  # not a member, and him
    assert all(s for s in speakers(talk, nobody, 5))  # an ordinary post then: someone answers
    rig.close()


def test_replying_to_a_member_brings_her(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    talk = talk_for(rig)
    hers = member_post(rig, 104, "general", "anyone for the weekly")
    mid = post(rig, "general", "yes, count me in", reply_to=hers)
    assert speakers(talk, mid, 5) == [[104]] * 5
    rig.close()


def test_only_officers_answer_in_officer_chat(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, script=numbered("noted {n}"))
    talk = talk_for(rig)
    mid = post(rig, "officer-chat", "loot rules for next week?")
    drawn = speakers(talk, mid, 60)
    assert all(s for s in drawn) and {g for s in drawn for g in s} <= OFFICERS
    named = post(rig, "officer-chat", "@Arkon @Belra @Coros thoughts?")  # Belra the officer
    assert speakers(talk, named, 5) == [[102]] * 5
    answer(rig, talk, mid)
    said = answers(rig, "officer-chat", mid)
    assert said and {m["author"] for m in said} <= OFFICERS
    rig.close()


def test_a_class_channel_prefers_that_class(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    talk = talk_for(rig)
    mid = post(rig, "mage", "what spec for heroics?")  # Coros is the mage
    first = Counter(s[0] for s in speakers(talk, mid, 300))
    assert first[103] > 2 * max(first[g] for g in TEST_CAST if g != 103)
    assert len(first) > 1  # a preference, never a rule
    rig.close()


def test_raid_channels_prefer_raiders_and_officers(store, clock, tmp_path):
    seed_test_cast(store)
    for g in TEST_CAST:
        save_member(store, g, region="US", guild_role="social" if g in (101, 103, 105)
                    else "raider")
    rig = make_rig(store, clock, tmp_path)
    talk = talk_for(rig)
    mid = post(rig, "raid-planning", "who is in for sunday?")
    first = Counter(s[0] for s in speakers(talk, mid, 300))
    assert min(first[102], first[104]) > 2 * max(first[101], first[103], first[105])
    assert first[101] + first[103] + first[105] > 0
    rig.close()


def test_offline_members_do_not_answer_a_channel_post(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, us=(105,))
    talk = talk_for(rig)
    mid = post(rig, "general", "anyone awake?")
    assert {g for s in speakers(talk, mid, 40) for g in s} == {105}
    rig.world.online.discard(105)  # no body in the game: his Discord presence decides (R14)
    assert {g for s in speakers(talk, mid, 10) for g in s} == {105}
    answer(rig, talk, mid)
    assert {m["author"] for m in answers(rig, "general", mid)} == {105}
    rig.close()


def test_a_reply_in_a_thread_goes_to_that_thread(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, reply="thursday works for me")
    talk = talk_for(rig)
    parent = post(rig, "raid-planning", "the sunday raid")
    thread = dstore.open_thread(store, parent, name="sunday", now=clock.now())
    mid = post(rig, "raid-planning", "which time suits everyone?", thread=thread)
    for _, meta in talk.plan(mid):
        assert (meta.discord.reply_to, meta.discord.thread) == (mid, thread)
    answer(rig, talk, mid)
    said = answers(rig, "raid-planning", mid, thread=thread)
    assert said and all(m["thread"] == thread for m in said)
    assert answers(rig, "raid-planning", mid) == []  # none in the channel's own feed
    rig.close()


def test_answer_delay_is_slower_for_idle_and_slowest_for_dnd():
    rng = random.Random(4)
    for state in ("online", "idle", "dnd", "offline"):
        assert all(2.0 <= answer_delay(state, rng) <= 25.0 for _ in range(200))
    avg = {s: sum(answer_delay(s, rng) for _ in range(400)) / 400
           for s in ("online", "idle", "dnd")}
    assert avg["online"] < avg["idle"] < avg["dnd"]


def test_context_lines_fold_newlines_and_leave_out_deleted(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    talk = talk_for(rig)
    member_post(rig, 103, "general", "line one\nline two\n\n\nline three")
    gone = member_post(rig, 101, "general", "never mind this")
    dstore.delete(store, gone, author=101, now=clock.now())
    post(rig, "general", "hello all")
    member_post(rig, 104, "pets", "my cat")
    assert talk.context_lines(store.conn, "general", None) == [
        "Coros: line one / line two / line three", f"{NICK}: hello all"]
    for i in range(20):
        member_post(rig, 105, "general", f"spam {i}")
    lines = talk.context_lines(store.conn, "general", None)
    assert len(lines) == 12 and lines[0] == "Ebbo: spam 8" and lines[-1] == "Ebbo: spam 19"
    assert talk.context_lines(store.conn, "general", None, limit=2) == [
        "Ebbo: spam 18", "Ebbo: spam 19"]
    rig.close()


def test_purpose_line(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    talk = talk_for(rig)
    assert talk.purpose_line("general") == (
        "You are in #general on the guild's Discord: Anything and everything, for the whole "
        "guild.")
    assert talk.purpose_line("officer-chat") == (
        "You are in #officer-chat on the guild's Discord: Private talk for the officers.")
    assert talk.purpose_line("dm:102") == f"You are in a private message with {NICK}"
    rig.close()


def test_the_prompt_has_the_channel_purpose_and_recent_messages_but_no_officer_lines_for_a_non_officer(
        store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, reply="im up for it")
    talk = talk_for(rig)
    member_post(rig, 102, "officer-chat", "glorbish is on probation")
    member_post(rig, 104, "officer-chat", "agreed, glorbish again")
    member_post(rig, 102, "dm:102", "vexmarrow is our secret")
    member_post(rig, 103, "general", "quixfen raid tonight\nbring flasks")
    mid = post(rig, "general", "@Arkon you coming to quixfen?")
    answer(rig, talk, mid)
    [m] = answers(rig, "general", mid)
    assert m["author"] == 101
    prompt = rig.prompts()[-1]
    assert ("You are in #general on the guild's Discord: Anything and everything, for the "
            "whole guild.") in prompt
    assert "Recent messages:" in prompt
    assert "Coros: quixfen raid tonight / bring flasks" in prompt
    assert "Answer his message: @Arkon you coming to quixfen?" in prompt
    assert prompt.count("@Arkon you coming to quixfen?") == 1
    assert "Answer his message: @Arkon you coming to quixfen?" in prompt
    assert "glorbish" not in prompt and "vexmarrow" not in prompt
    # an officer answering in officer-chat sees it, and only it
    o = post(rig, "officer-chat", "@Dunna what do you think?")
    rig.script("probation is fair")
    answer(rig, talk, o)
    prompt = rig.prompts()[-1]
    assert "You are in #officer-chat" in prompt and "glorbish is on probation" in prompt
    assert "quixfen" not in prompt and "vexmarrow" not in prompt
    # a DM's context is that DM only
    d = post(rig, "dm:102", "about that secret")
    rig.script("shh not here")
    answer(rig, talk, d)
    prompt = rig.prompts()[-1]
    assert f"You are in a private message with {NICK}" in prompt
    assert "vexmarrow is our secret" in prompt
    assert "glorbish" not in prompt and "quixfen" not in prompt
    rig.close()


def test_the_second_answer_sees_the_first(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, script=numbered("zorblat answer {n}"))
    talk = talk_for(rig)
    mid = post(rig, "general", "@Arkon @Coros who brings the cauldron?")
    answer(rig, talk, mid)
    first, second = answers(rig, "general", mid)
    assert (first["author"], second["author"]) == (101, 103)
    assert f"Arkon: {first['text']}" in rig.prompts()[-1]
    rig.close()


def test_a_long_post_and_busy_channel_still_fit_the_instruction(store, clock, tmp_path, caplog):
    rig = rig_for(store, clock, tmp_path, reply="fair point")
    talk = talk_for(rig)
    for i in range(12):
        member_post(rig, 103, "general", f"long story {i} " + "blah " * 300)
    mid = post(rig, "general", "@Arkon " + "words " * 330 + "the end")
    answer(rig, talk, mid)
    assert answers(rig, "general", mid)
    prompt = rig.prompts()[-1]
    assert "Answer his message: @Arkon words" in prompt
    assert "instruction extra capped" not in caplog.text
    rig.close()


def test_a_discord_answer_waits_up_to_two_minutes_for_the_model(store, clock, tmp_path):
    async def main():
        gate = asyncio.Event()
        rig = rig_for(store, clock, tmp_path, reply="sorry, was afk", gate=gate)
        talk = talk_for(rig)
        mid = post(rig, "dm:102", "you there?")
        talk.on_his_post(mid)
        game = asyncio.create_task(rig.engine.speak(Turn(
            kind=TurnKind.REPLY_HUMAN, speaker=103, channel="guild", addressee=HUMAN)))
        await rig.advance(90)  # past the game's 30 s, inside the Discord answer's 120 s
        gate.set()
        await rig.advance(60)
        assert game.done() and game.result() == []
        [m] = answers(rig, "dm:102", mid)
        assert m["author"] == 102
        drops = [r["dropped"] for r in store.conn.execute(
            "SELECT dropped FROM utterance WHERE dropped IS NOT NULL")]
        assert drops == ["expired"]
    asyncio.run(main())


def test_a_first_nothing_to_his_dm_is_asked_again(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    rig.script("(nothing)", "oh hey, sorry")
    talk = talk_for(rig)
    mid = post(rig, "dm:104", "you ok?")
    answer(rig, talk, mid)
    [m] = answers(rig, "dm:104", mid)
    assert len(rig.chat_calls()) == 2
    assert "Answer Zan's message in a line, in your own voice." in rig.prompts()[-1]
    # An explicit mention also gets one retry; an ordinary drawn answer may stay quiet.
    rig.script("(nothing)", "yes im here")
    calls = len(rig.chat_calls())
    named = post(rig, "general", "@Ebbo you there?")
    answer(rig, talk, named)
    assert len(answers(rig, "general", named)) == 1 and len(rig.chat_calls()) == calls + 2
    rig.script("(nothing)", "(nothing)")
    calls = len(rig.chat_calls())
    drawn = post(rig, "general", "anybody around?")
    answer(rig, talk, drawn)
    assert answers(rig, "general", drawn) == []
    assert len(rig.chat_calls()) <= calls + 2  # at most two selected members, one call each
    assert m["author"] == 104
    rig.close()


def test_an_emoji_only_answer_is_a_message(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, reply="😂👍")
    talk = talk_for(rig)
    mid = post(rig, "dm:101", "did you see that wipe")
    answer(rig, talk, mid)
    [m] = answers(rig, "dm:101", mid)
    assert m["text"] == "😂👍"
    # in the game the same reply is silence, as before
    assert rig.speak_turn(Turn(kind=TurnKind.REPLY_HUMAN, speaker=103, channel="guild",
                               addressee=HUMAN)) == []
    rig.close()


def test_his_post_takes_the_discord_lease_and_it_ends_after_ten_minutes(store, clock, tmp_path):
    async def main():
        rig = rig_for(store, clock, tmp_path)
        arbiter = FakeArbiter()
        talk = talk_for(rig, arbiter=arbiter)
        assert LEASE == "discord" and LEASE_IDLE_S == DiscordTalk.LEASE_IDLE_S == 600.0
        talk.on_his_post(post(rig, "general", "morning all"))
        assert arbiter.holders == {"discord"}
        await rig.advance(1)
        assert arbiter.log == [("acquire", "discord"), ("hand_over",)]
        await rig.advance(299)
        talk.on_his_post(post(rig, "general", "anyone?"))  # a further post refreshes it
        await rig.advance(500)
        talk.on_his_action()  # so does any other action of his
        await rig.advance(599)
        assert arbiter.holders == {"discord"} and ("release", "discord") not in arbiter.log
        await rig.advance(2)
        assert arbiter.holders == frozenset()
        assert arbiter.log == [("acquire", "discord"), ("hand_over",), ("release", "discord")]
        talk.on_his_action()  # an action with no lease takes none
        await rig.advance(1)
        assert arbiter.holders == frozenset()
        talk.on_his_post(post(rig, "general", "back again"))  # a post takes it anew
        await rig.advance(1)
        assert arbiter.log[-2:] == [("acquire", "discord"), ("hand_over",)]
        await talk.close()
        assert arbiter.log[-1] == ("release", "discord") and arbiter.holders == frozenset()
    asyncio.run(main())


def test_his_post_hands_the_gpu_back_from_the_night(store, clock, tmp_path):
    """The night holds the GPU (its reflection model loaded): his post takes the lease, which
    unloads it and preloads the live model; the answer waits for that; ten minutes after his
    last action the lease ends and the live model is kept only RELEASED_KEEP_ALIVE more."""
    async def main():
        rig = rig_for(store, clock, tmp_path, reply="morning!")
        live = rig.cfg.models.live
        gate = asyncio.Event()

        class SlowAdmin(FakeAdmin):
            async def preload(self, model, keep_alive, *, options=None):
                if keep_alive == -1:
                    await gate.wait()
                await super().preload(model, keep_alive, options=options)
        admin = SlowAdmin([REFL, rig.cfg.models.embedding])
        settings = SimpleNamespace(live_model=live, reflection_model=REFL,
                                   embedding_model=rig.cfg.models.embedding,
                                   ollama_url="http://127.0.0.1:1")
        arbiter = GpuArbiter(admin, settings)
        talk = talk_for(rig, arbiter=arbiter)
        rig.engine.live_admission = talk.handback  # as the nightly wiring does
        mid = post(rig, "dm:105", "morning")
        talk.on_his_post(mid)
        assert arbiter.holders == {"discord"} and not arbiter.reflection_allowed()
        await rig.advance(60)
        assert admin.calls == [("unload", REFL)] and rig.chat_calls() == []
        gate.set()
        await rig.advance(60)
        assert admin.calls == [("unload", REFL), ("preload", live, -1)]
        [m] = answers(rig, "dm:105", mid)
        assert m["author"] == 105
        await rig.advance(LEASE_IDLE_S)
        await arbiter.settled()
        assert arbiter.holders == frozenset()
        assert admin.calls[-1] == ("preload", live, "10m")
        await talk.close()
    asyncio.run(main())


def test_his_other_actions_keep_the_lease(store, clock, tmp_path):
    async def main():
        rig = rig_for(store, clock, tmp_path)
        arbiter = FakeArbiter()
        talk = talk_for(rig, arbiter=arbiter)
        app = SimpleNamespace(store=store, clock=clock, stopping=False, human_guid=HUMAN,
                              discord_talk=talk, world=rig.world,
                              on_his_discord_line=lambda line, chat, ts: None)
        desk = DiscordDesk(app)
        r = await desk.handle(discord_args({"action": "post", "channel": "general",
                                            "text": "hi all"}))
        await rig.advance(590)
        assert (await desk.handle(discord_args({"action": "react", "message": r["id"],
                                                "emoji": "👍"})))["ok"]
        await rig.advance(590)
        assert arbiter.holders == {"discord"}
        await rig.advance(20)
        assert arbiter.holders == frozenset()
        await talk.close()
    asyncio.run(main())


def test_a_dm_to_a_member_with_no_persona_is_not_answered(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    cache = rig.engine.personas
    talk = talk_for(rig, personas=SimpleNamespace(
        get=lambda g: None if g == 105 else cache.get(g)))
    mid = post(rig, "dm:105", "hello?")
    assert talk.plan(mid) == []
    rig.close()


def test_a_member_s_own_post_and_a_deleted_one_bring_no_answer(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    talk = talk_for(rig)
    hers = member_post(rig, 101, "general", "hello")
    assert talk.plan(hers) == [] and talk.plan(99999) == []
    gone = post(rig, "general", "oops")
    dstore.delete(store, gone, author=HUMAN, now=clock.now())
    assert talk.plan(gone) == []
    rig.close()


def test_his_dm_is_answered_whatever_the_guild_s_pace(store, clock, tmp_path):
    """As an answer to his whisper: nobody else reads it, so the guild's minute never holds it
    back. An answer in a channel the guild reads still keeps the pace."""
    rig = rig_for(store, clock, tmp_path, script=numbered("here {n}"))
    talk = talk_for(rig)
    for i in range(12):  # the guild has said its fill this minute
        rig.engine.guards.record(f"busy{i}", 103, clock.now(), TurnKind.OPEN)
    dm = post(rig, "dm:102", "you around?")
    named = post(rig, "general", "@Arkon you around?")
    answer(rig, talk, dm, seconds=40)
    answer(rig, talk, named, seconds=40)
    assert [m["author"] for m in answers(rig, "dm:102", dm)] == [102]
    assert answers(rig, "general", named) == []
    assert [r["dropped"] for r in store.conn.execute(
        "SELECT dropped FROM utterance WHERE dropped IS NOT NULL")] == ["guards"]
    rig.close()


def remember(store, clock, owner: int, text: str) -> int:
    """A memory of `owner`'s with no one in it, so only recall's query can bring it up."""
    t = int(clock.now()) - 30 * 86400
    return store.insert_memory(make_mem(owner=owner, text=text, actors=(), t_event=t,
                                        t_created=t, t_accessed=t, source="owner"))


def test_recall_follows_his_words(store, clock, tmp_path):
    """R13: an answer's memories are looked for with his post's words, so the one about what
    he asked comes up and an unrelated one does not."""
    rig = rig_for(store, clock, tmp_path, reply="same as last time then")
    talk = talk_for(rig)
    remember(store, clock, 101, "We cleared the spider wing in glimmerwick time last month.")
    remember(store, clock, 101, "Caught a quorbel fish down at the harbour.")
    mid = post(rig, "general", "@Arkon spider wing tonight?")
    [(_, meta)] = talk.plan(mid)
    assert meta.recall_query == "@Arkon spider wing tonight?"
    answer(rig, talk, mid)
    assert answers(rig, "general", mid)
    prompt = rig.prompts()[-1]
    assert "glimmerwick" in prompt and "quorbel" not in prompt
    rig.close()


def test_recall_in_a_thread_also_follows_its_last_lines(store, clock, tmp_path):
    """R13: in a thread (or replying to a message) his few words take the thread's last lines
    and the message he replied to with them."""
    rig = rig_for(store, clock, tmp_path, reply="count me in")
    talk = talk_for(rig)
    remember(store, clock, 104, "Our first spider wing clear took glimmerwick hours.")
    remember(store, clock, 104, "Caught a quorbel fish down at the harbour.")
    parent = post(rig, "raid-planning", "sunday")
    thread = dstore.open_thread(store, parent, name="sunday", now=clock.now())
    member_post(rig, 101, "raid-planning", "too early for me", thread=thread)
    member_post(rig, 103, "raid-planning", "the spider wing again?\nor plague", thread=thread)
    hers = member_post(rig, 102, "raid-planning", "bring flasks", thread=thread)
    mid = post(rig, "raid-planning", "@Dunna you in?", thread=thread, reply_to=hers)
    assert talk.recall_query(mid) == (
        "bring flasks too early for me the spider wing again? / or plague @Dunna you in?")
    gone = member_post(rig, 105, "raid-planning", "deleted line", thread=thread)
    dstore.delete(store, gone, author=105, now=clock.now())
    assert "deleted" not in talk.recall_query(post(rig, "raid-planning", "ok", thread=thread))
    answer(rig, talk, mid)
    assert answers(rig, "raid-planning", mid, thread=thread)
    prompt = rig.prompts()[-1]
    assert "glimmerwick" in prompt and "quorbel" not in prompt
    rig.close()


def test_a_dm_is_answered_with_the_game_down(store, clock, tmp_path):
    """R14: the guild's Discord lives beside the game. With the worldserver and the bridge down
    no member has a body online, yet his DM is answered and stored in Discord, a channel post
    is answered by presence, and nothing is sent anywhere."""
    def down(cmd):
        raise ConnectionLost("bridge down")
    rig = rig_for(store, clock, tmp_path, script=numbered("still here {n}"), ack=down)
    rig.world.online.clear()
    talk = talk_for(rig)
    mid = post(rig, "dm:103", "server crashed, you there?")
    answer(rig, talk, mid)
    [m] = answers(rig, "dm:103", mid)
    assert m["author"] == 103
    chan = post(rig, "general", "anyone around while the server is down?")
    assert all(s for s in speakers(talk, chan, 5))
    answer(rig, talk, chan)
    assert answers(rig, "general", chan)
    assert rig.sent.log == []
    assert [r["dropped"] for r in store.conn.execute(
        "SELECT dropped FROM utterance WHERE dropped IS NOT NULL")] == []
    rig.close()
