import contextlib
import logging
import math
from datetime import datetime, timedelta, timezone

import pytest

from conftest import HUMAN, make_mem, run, seed_test_cast
from leeroy_mind.llm.scheduler import LLMScheduler, SchedulerError
from leeroy_mind.memory.recall import (REST_S, Recall, fts_query, format_memory, mmr,
                                       recency)
from leeroy_mind.memory.writer import Embedder
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM, fake_embed

DAY = 86400


def vec(text):
    return pack_vec(fake_embed(text))


def add(store, text, *, embed=True, **kw):
    kw.setdefault("owner", 101)
    return store.insert_memory(make_mem(text=text, embedding=vec(text) if embed else None, **kw))


@pytest.fixture
def rig(store, clock):
    seed_test_cast(store)
    llm = FakeLLM()
    emb = Embedder(LLMScheduler(llm, clock=clock), "emb-model")
    return Recall(store, emb, clock, tau_days=5.0), llm


def now_of(clock):
    return int(clock.now())


def test_fts_query_survives_operators(store, clock):
    seed_test_cast(store)
    t = now_of(clock)
    mid = add(store, "Sindragosa wiped us twice on the frost breath.", embed=False,
              t_created=t, t_accessed=t)
    q = fts_query('he said "NEAR" * -x OR (y) sindragosa')
    assert q is not None and q.count('"') % 2 == 0
    rows = store.conn.execute("select rowid from memory_fts where memory_fts match ?", (q,)).fetchall()
    assert [r[0] for r in rows] == [mid]
    assert [i for i, _ in store.fts(101, q)] == [mid]
    assert fts_query("!! ??") is None


def test_fts_query_strips_item_links():
    q = fts_query("|cffa335ee|Hitem:50415:0:0:0|h[Bryntroll, the Bone Arbiter]|h|r")
    assert q == '"bryntroll" OR "bone" OR "arbiter"'


def test_fts_query_limits_and_multibyte():
    q = fts_query("alpha bravo charlie delta echoes foxtrot golf hotel india juliet alpha")
    assert q.count(" OR ") == 7
    assert q.count('"alpha"') == 1
    q = fts_query("Zuletzt hat Müller den Drachen gelegt 龙龙龙")
    assert q is not None and '"müller"' in q and '"龙龙龙"' in q


def test_recall_with_operator_text_returns_hit(rig, store, clock):
    r, _ = rig
    t = now_of(clock)
    mid = add(store, "Sindragosa wiped us twice on the frost breath.", embed=False,
              t_created=t, t_accessed=t)
    add(store, "Nothing about that fight.", embed=False, t_created=t, t_accessed=t)
    got = run(r.recall(101, 'he said "NEAR" * -x OR (y) sindragosa', present=[], addressee=None, k=1))
    assert [m.id for m in got] == [mid]


def test_presence_wins_tie(rig, store, clock):
    r, _ = rig
    t = now_of(clock)
    other = add(store, "Testy carried the raid last night.", actors=(103,), t_created=t, t_accessed=t)
    mine = add(store, "Testy carried the raid last night.", actors=(102,), t_created=t, t_accessed=t)
    assert mine > other
    got = run(r.recall(101, "unrelated", present=[102, 103], addressee=102, k=2))
    assert [m.id for m in got] == [mine, other]


def test_recency_uses_tau(rig, store, clock):
    r, _ = rig
    t = now_of(clock)
    assert recency(t - 5 * DAY, t, 5.0) == pytest.approx(math.exp(-1))
    old = add(store, "We cleared the keep together.", actors=(102,), t_created=t, t_accessed=t - 5 * DAY)
    new = add(store, "We cleared the keep together.", actors=(102,), t_created=t, t_accessed=t)
    got = run(r.recall(101, "keep", present=[102], addressee=None, k=2))
    assert got[0].id == new and got[1].id == old


def test_mmr_avoids_near_duplicates():
    ids = [1, 2, 3, 4, 5, 6]
    scores = {1: 1.0, 2: 0.99, 3: 0.98, 4: 0.97, 5: 0.96, 6: 0.8}
    sim = lambda a, b: 1.0 if a != 6 and b != 6 else 0.0  # noqa: E731
    out = mmr(ids, scores, sim, 2)
    assert out[0] == 1 and 6 in out
    assert mmr([], {}, sim, 3) == []


def test_mmr_end_to_end(rig, store, clock):
    r, _ = rig
    t = now_of(clock)
    dupes = [add(store, "The raid wiped on the second boss again.", actors=(102,), t_created=t,
                 t_accessed=t, importance=10) for _ in range(5)]
    odd = add(store, "Coros tanked the last boss alone once the raid wiped.", actors=(102,),
              t_created=t, t_accessed=t, importance=5)
    add(store, "Someone sold a stack of linen cloth.", actors=(102,), t_created=t,
        t_accessed=t, importance=1)
    got = [m.id for m in run(r.recall(101, "wiped raid boss tanked", present=[102], addressee=102, k=2))]
    # Margins with the fake embedder (a hashed bag of words), measured: at the second pick a
    # second dupe is worth 0.40 (similarity 1.0 to the first), the linen line 0.40 and the
    # odd memory 0.50 (similarity 0.64). The odd one wins until its similarity to the dupes
    # passes about 0.97, so this pins MMR's redundancy term, not the embedder (Task 11 carry).
    assert odd in got
    assert len([i for i in got if i in dupes]) == 1


def test_recall_touches_selected(rig, store, clock):
    r, _ = rig
    t = now_of(clock)
    a = add(store, "Frost breath wiped us.", actors=(102,), t_created=t - DAY, t_accessed=t - DAY)
    b = add(store, "Frost breath wiped us again.", actors=(102,), t_created=t - DAY,
            t_accessed=t - DAY)
    got = run(r.recall(101, "frost breath", present=[102], addressee=102, k=1))
    assert len(got) == 1
    (m,) = store.get_memories([got[0].id])
    assert m.access_n == 1 and m.t_accessed == t
    other = b if got[0].id == a else a
    assert store.get_memories([other])[0].access_n == 0


def test_recall_without_touch_marks_nothing(rig, store, clock):
    r, _ = rig
    t = now_of(clock)
    a = add(store, "Frost breath wiped us.", actors=(102,), t_created=t - DAY, t_accessed=t - DAY)
    got = run(r.recall(101, "frost breath", present=[102], addressee=102, k=1, touch=False))
    assert [m.id for m in got] == [a]
    (m,) = store.get_memories([a])
    assert m.access_n == 0 and m.t_accessed == t - DAY


def test_recall_without_embeddings(rig, store, clock):
    r, llm = rig
    llm.fail_next(1)
    t = now_of(clock)
    f = add(store, "Sindragosa breath killed half the raid.", t_created=t, t_accessed=t)
    p = add(store, "Chatted about fishing.", actors=(102,), t_created=t, t_accessed=t)
    add(store, "Unrelated gossip.", t_created=t, t_accessed=t)
    got = run(r.recall(101, "sindragosa", present=[102], addressee=102, k=5))
    ids = [m.id for m in got]
    assert f in ids and p in ids and len(ids) == 2


def test_recall_degrades_on_scheduler_error(rig, store, clock):
    r, _ = rig
    t = now_of(clock)

    class Boom:
        async def embed(self, *a, **k):
            raise SchedulerError("closed")
    r._embedder = Boom()
    f = add(store, "Sindragosa breath killed half the raid.", embed=False, t_created=t,
            t_accessed=t)
    assert [m.id for m in run(r.recall(101, "sindragosa", present=[], addressee=None))] == [f]


def test_vector_candidates_found(rig, store, clock):
    r, _ = rig
    t = now_of(clock)
    a = add(store, "gearing up for icecrown", t_created=t, t_accessed=t)
    add(store, "completely different words here", t_created=t, t_accessed=t)
    got = run(r.recall(101, "gearing up for icecrown", present=[], addressee=None, k=1))
    assert got[0].id == a


def test_inactive_excluded(rig, store, clock):
    r, _ = rig
    t = now_of(clock)
    add(store, "Sindragosa fight notes.", status="superseded", t_created=t, t_accessed=t)
    assert run(r.recall(101, "sindragosa", present=[], addressee=None)) == []


def test_owner_isolation(rig, store, clock):
    r, _ = rig
    t = now_of(clock)
    add(store, "Sindragosa secret of someone else.", owner=102, actors=(103,), t_created=t,
        t_accessed=t)
    mine = add(store, "Sindragosa mine.", t_created=t, t_accessed=t)
    got = run(r.recall(101, "sindragosa", present=[103], addressee=103))
    assert [m.id for m in got] == [mine]


def test_importance_breaks_tie(rig, store, clock):
    r, _ = rig
    t = now_of(clock)
    add(store, "Funny moment at the keep.", embed=False, actors=(102,), importance=2,
        t_created=t, t_accessed=t)
    hi = add(store, "Funny moment at the keep!", embed=False, actors=(102,), importance=9,
             t_created=t, t_accessed=t)
    got = run(r.recall(101, "zzz", present=[102], addressee=None, k=2))
    assert got[0].id == hi


def test_format_memory(clock):
    now = now_of(clock)
    tz = clock.local().tzinfo
    f = lambda **kw: format_memory(make_mem(**kw), now, tz=tz)  # noqa: E731
    assert f(t_event=now - 3 * DAY, text="x.") == "- three days ago: x."
    assert f(confidence=0.5, t_event=now - 3600, text="y.").endswith("(heard)")
    assert f(t_event=now - 20 * DAY, text="z.") == "- two weeks ago: z."
    assert "months ago" in f(t_event=now - 90 * DAY, text="z.")
    assert f(t_event=now - 1800, text="w.").startswith("- 30 minutes ago")
    assert f(t_event=now - 20 * 3600, text="v.").startswith("- last night")


def test_heard_boundary(clock):
    now = now_of(clock)
    tz = clock.local().tzinfo
    assert not format_memory(make_mem(confidence=0.8, t_event=now - 60, text="a."), now,
                             tz=tz).endswith("(heard)")
    assert format_memory(make_mem(confidence=0.79, t_event=now - 60, text="a."), now,
                         tz=tz).endswith("(heard)")


def test_format_memory_requires_the_clock_zone(clock):
    # no host-timezone fallback: the zone is keyword-only and required
    with pytest.raises(TypeError):
        format_memory(make_mem(), now_of(clock))
    with pytest.raises(TypeError):
        format_memory(make_mem(), now_of(clock), timezone.utc)


def test_format_follows_clock_timezone_not_host():
    plus8 = timezone(timedelta(hours=8))
    c = FakeClock(datetime(2026, 10, 29, 20, 0, tzinfo=plus8))
    now = int(c.now())
    mk = lambda ago: make_mem(t_event=now - ago, text="q.")  # noqa: E731
    fmt = lambda m: Recall(None, None, c, tau_days=5.0).format(m)  # noqa: E731
    assert fmt(mk(10 * 3600)).startswith("- ten hours ago")   # 10:00+08; 02:00 UTC would read last night
    c2 = FakeClock(datetime(2026, 10, 29, 7, 0, tzinfo=plus8))
    now2 = int(c2.now())
    m2 = make_mem(t_event=now2 - 2 * 3600, text="q.")          # 05:00+08, before the 06:00 night break
    assert Recall(None, None, c2, tau_days=5.0).format(m2).startswith("- last night")


@pytest.mark.parametrize("bad", [[], [b"short"], [b""]])
def test_recall_survives_bad_query_vector(rig, store, clock, bad):
    r, _ = rig
    t = now_of(clock)

    class Bad:
        async def embed(self, *a, **k):
            return bad
    r._embedder = Bad()
    f = add(store, "Sindragosa breath killed half the raid.", embed=False, t_created=t, t_accessed=t)
    assert [m.id for m in run(r.recall(101, "sindragosa", present=[], addressee=None))] == [f]


# -- whispers stay between the two people (the interim rule until P1c's private scope) --------

PUBLIC = ("guild", "officer", "party", "raid", "raid_warning", "say", "yell", "")


def said(store, channel: str, frm: int, to: int = 0) -> str:
    """A stored chat line, as the evidence ref a memory resting on it carries."""
    line = store.insert_chat_line(
        Chat(ts=0, channel=channel, from_guid=frm, from_name=f"M{frm}", to_guid=to, text="ok",
             map=0, zone=0), session_id=None, origin="game")
    return f"chat:{line}"


def remembered(store, clock, text: str, *evidence: str, actors=(HUMAN,),
               kind: str = "conversation", source: str = "extract") -> int:
    """Arkon's memory resting on `evidence`."""
    t = now_of(clock)
    return add(store, text, actors=actors, evidence=evidence, kind=kind, source=source,
               t_created=t, t_accessed=t)


def recalled(r, channel: str | None = None, to_guid: int = 0) -> list[int]:
    """Arkon's recall, with the human and Belra present, for a turn on `channel` (a whisper
    going to `to_guid`)."""
    got = run(r.recall(101, "quitting", present=[HUMAN, 102], addressee=None, k=5,
                       channel=channel, to_guid=to_guid))
    return [m.id for m in got]


@contextlib.contextmanager
def statements(store):
    """Every SQL statement the store runs inside the block."""
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)
    try:
        yield sql
    finally:
        store.conn.set_trace_callback(None)


def test_whisper_only_memories_stay_out_of_public_turns(rig, store, clock, caplog):
    """A memory resting only on his whispers with Arkon never comes up on a public channel (or
    one not chosen yet) and is not marked accessed there; Arkon's whisper back to him can."""
    r, _ = rig
    secret = remembered(store, clock, "Zan told me he is quitting his job.",
                        said(store, "whisper", HUMAN, 101), said(store, "whisper", 101, HUMAN))
    with caplog.at_level(logging.DEBUG, logger="leeroy_mind"):
        for channel in PUBLIC:
            assert recalled(r, channel) == [], channel
    assert caplog.text.count("recall_private_excluded: n=1") == len(PUBLIC)
    assert store.get_memories([secret])[0].access_n == 0
    assert recalled(r, "whisper", HUMAN) == [secret]


def test_a_whisper_between_two_members_stays_between_them(rig, store, clock):
    """Arkon's memory of Belra's whisper is not for guild chat and not for his whisper to the
    human. One resting on whispers with both of them is for neither whisper."""
    r, _ = rig
    theirs = remembered(store, clock, "Belra whispered me she is quitting the guild.",
                        said(store, "whisper", 102, 101), actors=(102,))
    remembered(store, clock, "Zan and Belra both told me they are quitting.",
               said(store, "whisper", HUMAN, 101), said(store, "whisper", 101, 102),
               actors=(HUMAN, 102))
    assert recalled(r, "guild") == []
    assert recalled(r, "whisper", HUMAN) == []
    assert recalled(r, "whisper", 102) == [theirs]


def test_a_memory_with_a_guild_line_is_not_whisper_only(rig, store, clock):
    r, _ = rig
    mixed = remembered(store, clock, "Zan said in guild and then told me he is quitting.",
                       said(store, "guild", HUMAN), said(store, "whisper", HUMAN, 101))
    assert recalled(r, "guild") == [mixed]
    assert recalled(r, "whisper", 102) == [mixed]


def test_memories_without_a_stored_chat_line_are_eligible_everywhere(rig, store, clock):
    """A game event has no chat evidence. A chat ref with no stored line counts as none: the
    rule rests on the lines it can read (the log never deletes one)."""
    r, _ = rig
    game = remembered(store, clock, "We killed Sindragosa.", "event:1", kind="event",
                      source="game")
    dangling = remembered(store, clock, "Zan pulled early again.", "chat:999")
    for channel, to in (("guild", 0), ("whisper", HUMAN), ("whisper", 102)):
        assert sorted(recalled(r, channel, to)) == [game, dangling], (channel, to)


def test_the_whisper_rule_reads_the_lines_in_one_query(rig, store, clock):
    """One store query per recall however many memories cite lines, and none without a
    channel: Recall's other callers are left as they were."""
    r, _ = rig
    for i in range(6):
        remembered(store, clock, f"Zan told me plan {i} for the raid.",
                   said(store, "guild", HUMAN), said(store, "whisper", HUMAN, 101),
                   said(store, "whisper", 101, HUMAN))
    secret = remembered(store, clock, "Zan told me he is quitting his job.",
                        said(store, "whisper", HUMAN, 101))
    with statements(store) as sql:
        got = recalled(r, "guild")
    assert len(got) == 5 and secret not in got
    assert sum("chat_line" in s for s in sql) == 1
    with statements(store) as sql:
        assert len(recalled(r)) == 5
    assert not any("chat_line" in s for s in sql)


def said_in(store, session: int, frm: int, channel: str = "guild", to: int = 0) -> str:
    line = store.insert_chat_line(
        Chat(ts=0, channel=channel, from_guid=frm, from_name=f"M{frm}", to_guid=to,
             text="quitting talk", map=0, zone=0), session_id=session, origin="mind")
    return f"chat:{line}"


def test_her_own_lines_from_this_session_stay_out_of_her_recall(rig, store, clock):
    """Dress rehearsal 2 (judge A fix 4): a member's prompt carried "eleven minutes ago:" and
    her own line, which "Your last lines" already showed, and she said it again. A memory
    resting only on the speaker's own lines of the session now open stays out of her recall;
    one with his line in it stays, and so does one from an earlier session, or one of what
    someone else said."""
    r, _ = rig
    before = store.start_session(clock.now() - 7200)
    old = remembered(store, clock, "I said I'm quitting raids for a week.",
                     said_in(store, before, 101))
    store.end_session(before, clock.now() - 3600)
    sid = store.start_session(clock.now() - 600)
    mine = remembered(store, clock, "I said I'm quitting the fishing contest.",
                      said_in(store, sid, 101), said_in(store, sid, 101))
    whisper = remembered(store, clock, "I whispered Zan I'm quitting early tonight.",
                         said_in(store, sid, 101, "whisper", HUMAN))
    with_him = remembered(store, clock, "Zan asked if I'm quitting and I said no.",
                          said_in(store, sid, HUMAN), said_in(store, sid, 101))
    told = remembered(store, clock, "Belra said she is quitting, and I'm not.",
                      said_in(store, sid, 102), said_in(store, sid, 101), actors=(102,))
    assert sorted(recalled(r, "guild")) == sorted([old, with_him, told])
    assert sorted(recalled(r, "whisper", HUMAN)) == sorted([old, with_him, told])
    assert mine in recalled(r)  # without a channel nothing is left out
    store.end_session(sid, clock.now())
    assert {mine, whisper} <= set(recalled(r, "whisper", HUMAN))  # history now


def test_the_own_lines_rule_reads_the_lines_in_one_more_query(rig, store, clock):
    r, _ = rig
    sid = store.start_session(clock.now() - 600)
    for i in range(6):
        remembered(store, clock, f"I told Zan plan {i} for quitting the raid.",
                   said_in(store, sid, 101), said_in(store, sid, HUMAN))
    with statements(store) as sql:
        assert len(recalled(r, "guild")) == 5
    assert sum("chat_line" in s for s in sql) == 2


# -- ages, lock-in and owner-authored memories (final fix wave) --------------------------------

HOUR = 3600


@pytest.mark.parametrize("ago, want", [
    (40, "just now"), (5 * 60, "five minutes ago"), (59 * 60, "59 minutes ago"),
    (2 * HOUR, "two hours ago"), (HOUR + 5, "an hour ago"), (20 * HOUR, "last night"),
    (3 * DAY, "three days ago"), (6 * DAY, "six days ago"), (9 * DAY, "a week ago"),
    (20 * DAY, "two weeks ago"), (40 * DAY, "a month ago"), (75 * DAY, "two months ago"),
    (183 * DAY, "six months ago"), (300 * DAY, "ten months ago"),
    (400 * DAY, "about a year ago"), (2 * 365 * DAY, "about two years ago"),
    (int(3.4 * 365 * DAY), "about three years ago"), (5 * 365 * DAY, "about five years ago"),
])
def test_ages_read_naturally_at_every_scale(clock, ago, want):
    """The guild history the owner approved is seeded months and years back: every age reads
    as people say it, never "earlier today" for an old memory nor "200 months ago"."""
    now = now_of(clock)
    line = format_memory(make_mem(t_event=now - ago, text="x."), now, tz=clock.local().tzinfo)
    assert line == f"- {want}: x."


def test_yesterday_by_day_last_night_by_night(clock):
    """The clock reads 20:00: a memory from yesterday afternoon is "yesterday", one from last
    night's raid "last night"."""
    now = now_of(clock)
    tz = clock.local().tzinfo
    def when(ago):
        return format_memory(make_mem(t_event=now - ago, text="x."), now, tz=tz)
    assert when(30 * HOUR) == "- yesterday: x."  # 14:00 the day before
    assert when(22 * HOUR) == "- last night: x."  # 22:00 the day before


DINNER = "I burned dinner because the raid ran late."
TWELVE = [DINNER, "Belra lent me flasks for the Sindragosa pull.",
          "We ran heroics until two in the morning.", "Coros pulled the whole room again.",
          "I finally got the drake from the weekly.", "The guild bank ran out of frost lotus.",
          "Dunna taught me the jump on Putricide.", "I lost the roll on the trinket to Ebbo.",
          "We wiped four times on the gunship.", "Ebbo sold me cheap enchanting mats.",
          "The weekly raid was Lord Marrowgar again.", "My keyboard died in the middle of a pull."]


def test_recall_rests_what_it_just_brought_up(rig, store, clock):
    """Prompts I3: every live prompt touched its picks, and min-max recency then gave those
    five 1.0 and the rest about 0: the same five came back for every query all night. A memory
    a live prompt used rests for REST_S; the others come up on their own merits."""
    r, _ = rig
    t = now_of(clock)
    ids = {add(store, text, actors=(102,), t_event=t - DAY, t_created=t - DAY - i,
               t_accessed=t - DAY): text for i, text in enumerate(reversed(TWELVE))}

    def ask(q):
        return [m.id for m in run(r.recall(101, q, present=[102], addressee=None, k=5))]
    first = ask("anyone want to run heroics")
    assert DINNER not in {ids[i] for i in first}
    second = ask("what's for dinner tonight")
    assert len(set(first) & set(second)) <= 2
    assert DINNER in {ids[i] for i in second}  # its own full-text match comes up
    run(clock.advance(REST_S + 60, step=REST_S))
    again = ask("anyone want to run heroics")
    assert set(again) & set(first)  # rested, not forgotten


def test_recency_is_not_stretched_over_the_candidates(rig, store, clock):
    """Two memories used an hour apart differ by a hair in recency, not by a full point."""
    r, _ = rig
    t = now_of(clock)
    a = add(store, "We cleared the keep together.", actors=(102,), t_created=t,
            t_accessed=t - 2 * HOUR, importance=6)
    b = add(store, "We cleared the keep together!", actors=(102,), t_created=t,
            t_accessed=t - 3 * HOUR, importance=7)
    got = run(r.recall(101, "keep", present=[102], addressee=None, k=2))
    assert got[0].id == b and got[1].id == a  # importance wins over an hour of recency


def test_owner_memories_are_public(rig, store, clock):
    """Part A seeds the guild's history as owner-authored memories (evidence "owner:<moment>").
    It is public, non-chat evidence: such a memory reaches every channel's turns, even one that
    also cites a whisper between two others."""
    r, _ = rig
    lore = remembered(store, clock, "We were the first guild to clear the old keep.",
                      "owner:the-keep-2007", kind="story", source="owner")
    mixed = remembered(store, clock, "Belra and I were quitting over the keep wipes.",
                       "owner:quitting-2008", said(store, "whisper", 103, 104), kind="story",
                       source="owner")
    for channel in PUBLIC:
        assert {lore, mixed} <= set(recalled(r, channel)), channel
    assert {lore, mixed} <= set(recalled(r, "whisper", to_guid=HUMAN))


def test_memory_depth_can_change_without_a_restart(rig, store, clock):
    """Spec minor 3: a reloaded mind.toml changes the recency half-life (TalkEngine passes
    it on in apply_config)."""
    r, _ = rig
    t = now_of(clock)
    m = make_mem(t_accessed=t - 5 * DAY)
    assert r.tau_days == 5.0
    r.set_tau(10.0)
    assert r.tau_days == 10.0 and recency(m.t_accessed, t, r.tau_days) == pytest.approx(
        math.exp(-0.5))
    with pytest.raises(ValueError):
        r.set_tau(0)


def test_old_game_memories_lose_the_tonight_their_template_baked_in(clock):
    """Prompts minor 4: days later recall showed "- 5 days ago: We wiped 3x on Sindragosa
    tonight." The stored texts stay as events.py writes them (its tests pin them); the line a
    prompt shows says "3 times", and "tonight" only for tonight's."""
    now = now_of(clock)
    tz = clock.local().tzinfo

    def line(text, ago, kind="event"):
        return format_memory(make_mem(kind=kind, text=text, t_event=now - ago), now, tz=tz)
    assert line("We wiped 3× on Sindragosa tonight.", 5 * DAY) \
        == "- five days ago: We wiped 3 times on Sindragosa."
    assert line("Tonight: 3 battlegrounds (2 won); spent time in Wintergrasp.", 2 * DAY) \
        == "- two days ago: 3 battlegrounds (2 won); spent time in Wintergrasp."
    assert line("We wiped 4× on Sindragosa, then killed it.", 3 * DAY) \
        == "- three days ago: We wiped 4 times on Sindragosa, then killed it."
    assert line("We wiped 2× on Lord Marrowgar tonight.", 2 * HOUR) \
        == "- two hours ago: We wiped 2 times on Lord Marrowgar tonight."
    assert line("Zan said he would raid tonight.", 5 * DAY, kind="conversation") \
        == "- five days ago: Zan said he would raid tonight."  # someone's words stay theirs


# -- fix wave G3: who holds the guild's past on his line -----------------------------------------

def test_holders_hold_the_subject_and_never_hearsay_nor_a_whisper(rig, store, clock):
    """Rehearsal 3, check 3: the members who held the guild's history on his question were
    never drawn. Recall.holders searches everyone's memories at once; hearsay and a memory
    resting only on whispers are no one's to tell in public."""
    r, _ = rig
    seeded = add(store, "Zan split bank duty in 2008: the even weeks are Arkon's.", owner=101,
                 source="owner", evidence=("owner:M1",))
    add(store, "Belra said the bank duty weeks are hers.", owner=102, source="extract",
        confidence=0.6, evidence=(said(store, "guild", 102),))
    add(store, "Zan whispered me the bank duty weeks are mine.", owner=103, source="extract",
        evidence=(said(store, "whisper", HUMAN, 103),))
    event = add(store, "We sorted out bank duty after the raid.", owner=104, source="game",
                evidence=("event:1",))
    add(store, "Bank duty again.", owner=105, status="archived")
    got = r.holders("who gets the even weeks for bank duty again?")
    assert [m.id for m in got] == [seeded, event]
    assert r.holders("") == [] and r.holders("?!") == []
