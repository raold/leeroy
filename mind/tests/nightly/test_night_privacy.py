"""The memory scope through P1b's write and recall paths (P1c Task 1; controller rulings N4, B1
and B2): what he whispers a member stays with the two of them, whoever writes the memory."""
import json
import logging
import shutil
import sqlite3
from datetime import datetime, timezone

import pytest

from conftest import make_mem, run
from engine_rig import make_rig
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.extract import Extractor
from leeroy_mind.memory.privacy import backfill_scopes, derive_scope, secret_lines
from leeroy_mind.memory.recall import PRIVATE_TAIL, Recall, format_memory
from leeroy_mind.memory.threads import ClosedThread
from leeroy_mind.memory.writer import Embedder, MemoryCandidate, MemoryWriter, WriteResult
from leeroy_mind.nightly.knowledge import is_secret, scope_leaks
from leeroy_mind.storage import db as db_mod
from leeroy_mind.storage.db import open_db
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_llm import FakeLLM, script_from
from nightly.conftest import IDENT

UTC = timezone.utc
PUBLIC = ("guild", "officer", "party", "raid", "raid_warning", "say", "yell", "")


def writer(h) -> MemoryWriter:
    return MemoryWriter(h.store, Embedder(LLMScheduler(FakeLLM(), clock=h.clock), h.EMB), h.clock,
                        human=lambda: IDENT)


def scope(h, mid: int) -> tuple[str, int]:
    """The stored (scope, scope_with) of a memory."""
    (m,) = h.store.get_memories([mid])
    return m.scope, m.scope_with


# -- recall ------------------------------------------------------------------------------------

def test_recall_keeps_private_and_secret_out_of_public_prompts(h, caplog):
    seen = h.mem(101, "Belra kept us all alive on the gunship.", actors=(102,))
    private = h.mem(101, "Testy says Belra is his favourite healer.", actors=(102,),
                    scope="private", scope_with=1)
    secret = h.mem(101, "Belra is moving to a new town soon.", actors=(102,), scope="secret",
                   scope_with=102)
    nobody = h.mem(101, "Belra and Coros had words over the loot.", actors=(102,),
                   scope="private", scope_with=0)
    r = Recall(h.store, Embedder(LLMScheduler(FakeLLM(), clock=h.clock), h.EMB), h.clock,
               tau_days=5.0)

    def recalled(channel=None, to_guid=0) -> list[int]:
        return sorted(m.id for m in run(r.recall(101, "belra", present=[102], addressee=None,
                                                 channel=channel, to_guid=to_guid)))
    with caplog.at_level(logging.DEBUG, logger="leeroy_mind"):
        for c in PUBLIC:
            assert recalled(c) == [seen], c
    assert caplog.text.count("recall_scope_excluded: n=3") == len(PUBLIC)
    assert recalled("whisper", 1) == sorted([seen, private])
    assert recalled("whisper", 102) == sorted([seen, secret])
    assert recalled("whisper", 103) == [seen]
    assert recalled() == sorted([seen, private, secret, nobody])  # no channel: nothing left out
    rows = h.store.get_memories([seen, private, secret, nobody])
    assert [format_memory(m, h.T, tz=UTC).endswith(PRIVATE_TAIL) for m in rows] == [
        False, True, True, True]

    # P1b's interim rule still runs under the scope: an open row resting only on his whispers
    line = h.say(h.T, 1, "belra is my favourite healer, just saying", channel="whisper", to=101)
    whispered = h.mem(101, "Testy told me Belra is his favourite.", actors=(1, 102),
                      evidence=(f"chat:{line}",))
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="leeroy_mind"):
        assert recalled("guild") == [seen]
    assert "recall_private_excluded: n=1" in caplog.text
    assert recalled("whisper", 1) == sorted([seen, private, whispered])


def test_a_public_turn_addressed_to_the_partner_still_recalls_open_memories_only(h):
    # Task 1 review, minor 1: the scope reads the turn's channel, not only its partner, so a
    # public turn whose to_guid is the memory's partner (a free field of every Turn) still
    # leaves a private or secret memory out
    seen = h.mem(101, "Belra kept us all alive on the gunship.", actors=(102,))
    private = h.mem(101, "Testy says Belra is his favourite healer.", actors=(102,),
                    scope="private", scope_with=1)
    secret = h.mem(101, "Belra is moving to a new town soon.", actors=(102,), scope="secret",
                   scope_with=102)
    r = Recall(h.store, Embedder(LLMScheduler(FakeLLM(), clock=h.clock), h.EMB), h.clock,
               tau_days=5.0)

    def recalled(channel, to_guid) -> list[int]:
        return sorted(m.id for m in run(r.recall(101, "belra", present=[102], addressee=None,
                                                 channel=channel, to_guid=to_guid)))
    for c in PUBLIC:
        for to in (0, 1, 102):
            assert recalled(c, to) == [seen], (c, to)
    assert recalled("whisper", 1) == sorted([seen, private])
    assert recalled("whisper", 102) == sorted([seen, secret])


def test_compose_on_a_public_turn_to_the_partner_leaves_a_scoped_memory_out(store, clock,
                                                                            tmp_path):
    r = make_rig(store, clock, tmp_path)
    t = int(clock.now())
    text = "He told me he is changing jobs next month."
    mid = store.insert_memory(make_mem(owner=101, kind="conversation", text=text, actors=(1,),
                                       t_event=t, t_created=t, t_accessed=t, scope="private",
                                       scope_with=1))

    async def go():
        return [await r.engine.compose(Turn(kind=TurnKind.OPEN, speaker=101, channel=c,
                                            to_guid=1)) for c in ("guild", "officer")]
    for messages, ids in run(go()):
        assert mid not in ids and text not in "\n".join(m["content"] for m in messages)


def test_compose_shows_a_scoped_memory_only_in_a_whisper_to_its_partner(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    t = int(clock.now())
    text = "He told me he is changing jobs next month."
    mid = store.insert_memory(make_mem(owner=101, kind="conversation", text=text, actors=(1,),
                                       t_event=t, t_created=t, t_accessed=t, scope="private",
                                       scope_with=1))

    async def go():
        guild = await r.engine.compose(Turn(kind=TurnKind.OPEN, speaker=101, channel="guild"))
        whisper = await r.engine.compose(Turn(kind=TurnKind.REPLY_HUMAN, speaker=101,
                                              channel="whisper", to_guid=1, addressee=1))
        return guild, whisper
    (guild_messages, guild_ids), (whisper_messages, whisper_ids) = run(go())
    assert mid not in guild_ids and text not in "\n".join(m["content"] for m in guild_messages)
    assert mid in whisper_ids
    (line,) = [ln for m in whisper_messages for ln in m["content"].splitlines() if text in ln]
    assert line.endswith(PRIVATE_TAIL)


# -- the writer: the scope comes from the evidence ---------------------------------------------

def test_writer_derives_the_scope_from_the_evidence(h):
    w = writer(h)

    def put(*evidence, owner=101, shown=()):
        return scope(h, w.put(make_mem(owner=owner, text="A note.", evidence=evidence),
                              shown=shown))
    his = h.say(h.T, 1, "got the job at the hospital", channel="whisper", to=101)
    hers = h.say(h.T + 5, 101, "omg thats huge, proud of you", channel="whisper", to=1)
    told = h.say(h.T + 10, 1, "my sister is getting married", channel="whisper", to=101)
    hush = h.say(h.T + 15, 101, "aww congrats, dont tell anyone i cried", channel="whisper", to=1)
    h.store.set_thread([told, hush], h.store.open_thread("whisper:1:101", h.T + 10))
    guild = h.say(h.T + 20, 1, "raid at 8 tonight", channel="guild")
    belra = h.say(h.T + 25, 102, "can i borrow your flask", channel="whisper", to=101)
    to_coros = h.say(h.T + 30, 1, "coros you around", channel="whisper", to=103)
    switching = h.say(h.T + 35, 1, "between us, im switching mains", channel="guild")
    mine = h.mem(101, "Testy trusts me with his news.", scope="private", scope_with=1)
    shared = h.mem(102, "Testy told me his sister is getting married.", scope="private",
                   scope_with=1)

    assert put(f"chat:{his}") == ("private", 1)
    assert put(f"chat:{hers}") == ("private", 1)                    # either end
    assert put(f"chat:{told}") == ("secret", 1)                     # her "dont tell" in its thread
    assert put(f"chat:{guild}") == ("open", 0)
    assert put(f"chat:{guild}", f"chat:{his}") == ("private", 1)    # any whisper evidence
    assert put(f"chat:{belra}") == ("private", 102)
    assert put(f"chat:{his}", f"chat:{belra}") == ("private", 0)    # two partners: nobody
    assert put(f"chat:{to_coros}") == ("private", 0)                # 101 is not one of its ends
    assert put(f"memory:{mine}") == ("private", 1)
    assert put(f"memory:{shared}") == ("private", 102)              # the member who shared it
    assert put(f"chat:{switching}") == ("open", 0)                  # a public line is never secret
    assert put("event:1") == ("open", 0)
    assert put(f"chat:{guild}", shown=[f"chat:{his}"]) == ("private", 1)   # what the prompt showed


def test_callers_may_raise_but_never_lower(h, caplog):
    w = writer(h)
    his = h.say(h.T, 1, "got the job at the hospital", channel="whisper", to=101)
    guild = h.say(h.T + 5, 1, "raid at 8 tonight", channel="guild")

    def put(*evidence, **kw):
        return scope(h, w.put(make_mem(owner=101, text="A note.", evidence=evidence, **kw)))
    assert put("encounter:5", scope="secret", scope_with=102) == ("secret", 102)   # a raise
    assert put(f"chat:{his}", scope="open") == ("private", 1)          # the evidence decides
    assert put(f"chat:{his}", scope="private", scope_with=103) == ("private", 1)   # same partner
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert put(f"chat:{guild}", scope="secret") == ("open", 0)     # said in guild: never secret
    assert caplog.text.count("scope_secret_refused") == 1

    first = w.put(make_mem(owner=101, text="Testy has a new job.", evidence=(f"chat:{his}",),
                           key="followup:job"))
    again = w.put(make_mem(owner=101, text="Testy started his new job.",
                           evidence=(f"chat:{guild}",), key="followup:job"))
    assert again == first and scope(h, again) == ("private", 1)       # a keyed rewrite never lowers

    h.persona(101)
    r = run(w.write(MemoryCandidate(
        owner=101, kind="observation", text="Testy called the raid for 8.", t_event=int(h.T),
        actors=(1,), importance=4, valence=0.0, source="extract", evidence=(f"chat:{guild}",),
        scope="secret")))
    assert r.action == "inserted" and scope(h, r.id) == ("open", 0)


def test_a_keyed_write_takes_the_scope_of_its_evidence(h):
    # Task 1 review, minor 2: write()'s keyed branch is scoped too. A first keyed write has no
    # stored row whose scope the store's never-lower rule could keep, so only the writer's own
    # derivation makes it private
    h.persona(101)
    w = writer(h)
    his = h.say(h.T, 1, "got the job at the hospital", channel="whisper", to=101)
    guild = h.say(h.T + 5, 1, "raid at 8 tonight", channel="guild")

    def keyed(line: int, key: str):
        return run(w.write(MemoryCandidate(
            owner=101, kind="plan", text="Ask Testy how the new job is going.", t_event=int(h.T),
            actors=(1,), importance=5, valence=0.2, source="extract", evidence=(f"chat:{line}",),
            key=key)))
    whispered, public = keyed(his, "followup:job"), keyed(guild, "followup:raid")
    assert (whispered.action, public.action) == ("updated", "updated")
    assert scope(h, whispered.id) == ("private", 1) and scope(h, public.id) == ("open", 0)


# -- P1b's extractor, unedited: the writer scopes what it writes -------------------------------

def threaded(h, key: str, *lines: tuple[int, str, str, int]) -> tuple[ClosedThread, list[int]]:
    """The lines (guid, text, channel, to) stored a second apart and put in one thread, as
    P1b's ThreadTracker files them."""
    tid = h.store.open_thread(key, h.clock.now())
    ids = [h.say(h.clock.now() + 1, g, text, channel=ch, to=to) for g, text, ch, to in lines]
    h.store.set_thread(ids, tid)
    return ClosedThread(tid, key, tuple(ids), "silence"), ids


def arkon(*memories: tuple, overheard=None) -> str:
    """The extraction's reply: Arkon's memories, each (text, the line it cites[, what])."""
    def one(text: str, line: int, *what: str) -> dict:
        m = {"speaker": "Arkon", "text": text, "importance": 6, "valence": 0.2, "about": ["Testy"],
             "evidence": [f"L{line}"]}
        return m | ({"what": what[0]} if what else {})
    return json.dumps({"memories": [one(*m) for m in memories], "overheard": overheard})


def extract(h, t: ClosedThread, reply: str, *, bystanders=()) -> list[tuple[str, tuple[str, int]]]:
    """P1b's Extractor on thread `t`, its model replying `reply`: (kind, (scope, scope_with))
    of each memory it adds for 101."""
    known = {m.id for m in h.rows(101)}
    sched = LLMScheduler(FakeLLM(script_from([reply])), clock=h.clock)
    w = MemoryWriter(h.store, Embedder(sched, h.EMB), h.clock, human=lambda: IDENT)
    ex = Extractor(h.store, sched, w, h.LIVE, human_guid=lambda: 1,
                   bystanders=lambda thread, speakers: list(bystanders), human=lambda: IDENT)
    assert run(ex.extract(t)).status == "extracted"
    return [(m.kind, (m.scope, m.scope_with)) for m in h.rows(101) if m.id not in known]


def test_extractor_scopes_whisper_threads(h):
    answered, (his, hers) = threaded(
        h, "whisper:1:101", (1, "got the job at the hospital, i start in june", "whisper", 101),
        (101, "no way, that is amazing news", "whisper", 1))
    # P1b keeps a member's own line only as something that happened (a promise here)
    assert extract(h, answered, arkon(("Testy told me he got the hospital job.", his),
                                      ("I promised Testy I'd visit him at the hospital.", hers,
                                       "promise"))) == [
        ("conversation", ("private", 1))] * 2                          # whichever line it cites

    unanswered, _ = threaded(
        h, "whisper:1:101", (1, "you awake? need to vent about my landlord", "whisper", 101),
        (1, "he wants more rent again in march", "whisper", 101))
    overheard = {"text": "Testy is fed up with his landlord.", "importance": 6}
    assert extract(h, unanswered, arkon(overheard=overheard), bystanders=[101]) == [
        ("observation", ("private", 1))]                               # the overheard branch (B6)

    hushed, (told, _) = threaded(
        h, "whisper:1:101", (1, "my sister is getting married in june", "whisper", 101),
        (101, "aww congrats! dont tell anyone but i cried", "whisper", 1))
    assert extract(h, hushed, arkon(("Testy told me his sister is getting married.", told))) == [
        ("conversation", ("secret", 1))]

    guild, (_, mine) = threaded(
        h, "guild", (1, "who is up for heroics after the raid", "guild", 0),
        (101, "me, i still need emblems for my trinket", "guild", 0))
    reply = arkon(("I told Testy I'd join his heroics after the raid.", mine, "plan"))
    assert extract(h, guild, reply) == [("conversation", ("open", 0))]


def test_writer_never_merges_across_scopes(h):
    h.persona(101)
    w = writer(h)
    guild = h.say(h.T, 1, "got a new job guys", channel="guild")
    first = h.say(h.T + 5, 1, "the new job pays really well", channel="whisper", to=101)
    second = h.say(h.T + 10, 1, "and the new boss seems nice", channel="whisper", to=101)
    hushed = h.say(h.T + 15, 1, "dont tell anyone but i might quit it already", channel="whisper",
                   to=101)

    def write(line: int):
        return run(w.write(MemoryCandidate(
            owner=101, kind="conversation", text="Testy got a new job.", t_event=int(h.T),
            actors=(1, 101), importance=5, valence=0.0, source="extract",
            evidence=(f"chat:{line}",))))
    public, private = write(guild), write(first)
    assert (public.action, private.action) == ("inserted", "inserted") and public.id != private.id
    assert (scope(h, public.id), scope(h, private.id)) == (("open", 0), ("private", 1))
    assert write(second) == WriteResult(private.id, "merged")
    assert h.store.get_memories([private.id])[0].evidence == (f"chat:{first}", f"chat:{second}")
    secret = write(hushed)  # the private one's whisper pair too: only the scope tells them apart
    assert secret.action == "inserted" and secret.id not in (public.id, private.id)
    assert scope(h, secret.id) == ("secret", 1)


# -- the migration's backfill (controller ruling B1) -------------------------------------------

P1B_MEMORY_COLS = ("owner, kind, text, t_event, t_created, t_accessed, access_n, actors, place, "
                   "boss_id, raid_id, importance, valence, source, evidence, confidence, status, "
                   "superseded_by, key, merged_n, embedding")


def nightly_version() -> int:
    (v,) = [v for v, p in db_mod._migration_files() if p.name.endswith("_nightly.sql")]
    return v


def before_the_scope(path, tmp_path, monkeypatch, name: str) -> sqlite3.Connection:
    """mind.db built by every migration below the nightly one, as P1b left it."""
    old = tmp_path / name
    old.mkdir()
    below = nightly_version()
    for v, f in db_mod._migration_files():
        if v < below:
            shutil.copy(f, old / f.name)
    with monkeypatch.context() as m:
        m.setattr(db_mod, "MIGRATIONS_DIR", old)
        return open_db(path)


def chat_thread(conn, key: str) -> int:
    return conn.execute("INSERT INTO chat_thread(key, opened, status) VALUES (?, 1, 'extracted')",
                        (key,)).lastrowid


def line(conn, channel: str, frm: int, to: int, text: str, thread: int | None = None) -> int:
    return conn.execute(
        "INSERT INTO chat_line(ts, channel, from_guid, from_name, to_guid, text, thread_id, "
        "origin) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
        (channel, frm, f"M{frm}", to, text, thread, "human" if frm == 1 else "mind")).lastrowid


def remember(conn, owner: int, *evidence: str, status: str = "active") -> int:
    """A memory as P1b's store wrote it: its 21 columns, before the scope."""
    return conn.execute(
        f"INSERT INTO memory({P1B_MEMORY_COLS}) VALUES ({','.join('?' * 21)})",
        (owner, "conversation", "Something from before the scope.", 1000, 1000, 1000, 0, "[]",
         None, None, None, 5, 0.0, "extract", json.dumps(list(evidence)), 1.0, status, None, None,
         1, None)).lastrowid


def test_the_migration_backfills_existing_memories(tmp_path, monkeypatch):
    path = tmp_path / "mind.db"
    conn = before_the_scope(path, tmp_path, monkeypatch, "old")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == nightly_version() - 1
        his = line(conn, "whisper", 1, 101, "got the job at the hospital")
        hers = line(conn, "whisper", 101, 1, "so proud of you")
        hush = chat_thread(conn, "whisper:1:101")
        line(conn, "whisper", 1, 101, "between us, im leaving my job", hush)
        sealed = line(conn, "whisper", 101, 1, "ok, my lips are sealed", hush)
        guild = line(conn, "guild", 1, 0, "raid at 8 tonight")
        belra = line(conn, "whisper", 102, 101, "can i borrow your flask")
        want = {
            remember(conn, 101, f"chat:{his}", f"chat:{hers}"): ("private", 1),
            remember(conn, 101, f"chat:{sealed}"): ("secret", 1),      # its thread: "between us"
            remember(conn, 101, f"chat:{guild}"): ("open", 0),
            remember(conn, 101, f"chat:{guild}", f"chat:{his}"): ("private", 1),
            remember(conn, 101, "event:7"): ("open", 0),
            remember(conn, 101, f"chat:{his}", f"chat:{belra}"): ("private", 0),
            remember(conn, 101, f"chat:{his}", status="archived"): ("private", 1),   # every status
        }
        conn.commit()
    finally:
        conn.close()
    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db_mod.latest_version()
        rows = conn.execute("SELECT id, owner, evidence, scope, scope_with FROM memory").fetchall()
        assert {r["id"]: (r["scope"], r["scope_with"]) for r in rows} == want
        for r in rows:
            d = derive_scope(conn, r["owner"], json.loads(r["evidence"]))
            assert (d.scope, d.scope_with) == (r["scope"], r["scope_with"]), r["id"]
    finally:
        conn.close()

    broken = tmp_path / "broken.db"
    conn = before_the_scope(broken, tmp_path, monkeypatch, "old-2")
    try:
        remember(conn, 101, "event:7")
        conn.commit()
    finally:
        conn.close()

    def fails(conn):
        raise RuntimeError("backfill failed")
    monkeypatch.setattr("leeroy_mind.memory.privacy.backfill_scopes", fails)
    with pytest.raises(RuntimeError, match="backfill failed"):
        open_db(broken)
    conn = sqlite3.connect(broken)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == nightly_version() - 1
        columns = {r[1] for r in conn.execute("PRAGMA table_info(memory)")}
        assert not {"scope", "scope_with"} & columns
        # the data step shares the migration's transaction: nothing of the nightly one is left
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'night_run'"
                            ).fetchone()[0] == 0
    finally:
        conn.close()


# -- an ask covers the pair's later threads that night (fix I1) --------------------------------

# Nights run 06:00 to 06:00 (clock.night_of), and privacy reads them in the host's zone (the
# mind clock's in production), so these tests set their times in local time.
EVENING = datetime(2026, 10, 29, 21).timestamp()


def whispers(h, t: float, *lines: tuple[int, int, str]) -> list[int]:
    """The (from, to, text) whispers stored a second apart from `t`, in one thread of their
    pair's key (threaded). Returns the line ids."""
    h.clock.set(t)
    frm, to, _ = lines[0]
    return threaded(h, f"whisper:{min(frm, to)}:{max(frm, to)}",
                    *((g, text, "whisper", other) for g, other, text in lines))[1]


def test_an_ask_covers_the_pairs_later_threads_that_night(h):
    # P1b cuts a whisper thread after 90 s of silence or at 12 lines, so his ask and the secret
    # it covers can land in different threads (review-task8 I1; amendment A4)
    before = whispers(h, EVENING, (1, 101, "you still up?"), (101, 1, "yeah whats up"))
    asked = whispers(h, EVENING + 600, (1, 101, "can i tell you something? between us"),
                     (101, 1, "of course"))
    told = whispers(h, EVENING + 900, (1, 101, "im leaving my job next month"),
                    (101, 1, "wow, ok"))                                # after 90 s of silence
    later = whispers(h, EVENING + 4 * 3600, (101, 1, "you doing ok about the job?"))   # 01:00
    assert secret_lines(h.store.conn, before + asked + told + later) == {*asked, *told, *later}
    w = writer(h)

    def put(line: int) -> tuple[str, int]:
        return scope(h, w.put(make_mem(owner=101, text="A note.", evidence=(f"chat:{line}",))))
    assert [put(i) for i in before] == [("private", 1)] * 2          # never reaches back
    assert [put(i) for i in asked] == [("secret", 1)] * 2            # the ask's own thread
    assert [put(i) for i in told + later] == [("secret", 1)] * 3     # and the rest of that night
    assert [is_secret(h.store, f"chat:{i}") for i in before + told] == [False, False, True, True]

    # a memory stored with the per-thread reading: the check lists it, the backfill raises it
    old = h.mem(101, "Testy is leaving his job.", evidence=(f"chat:{told[0]}",), scope="private",
                scope_with=1)
    assert old in scope_leaks(h.store)
    with h.store.transaction():
        backfill_scopes(h.store.conn)
    assert scope(h, old) == ("secret", 1) and scope_leaks(h.store) == []


def test_an_ask_never_covers_another_pair(h):
    whispers(h, EVENING, (1, 101, "between us, im leaving my job"), (101, 1, "my lips are sealed"))
    h.clock.set(EVENING + 300)
    _, (guild,) = threaded(h, "guild", (1, "between us guys, im leaving my job", "guild", 0))
    others = [*whispers(h, EVENING + 600, (1, 102, "you coming to raid tonight?")),  # him, 102
              *whispers(h, EVENING + 900, (101, 102, "wanna run heroics later?")),   # her, 102
              *whispers(h, EVENING + 1200, (1, 103, "got a sec?"))]          # after a guild ask
    theirs = whispers(h, EVENING + 1500, (101, 1, "so when do you tell your boss?"))
    assert secret_lines(h.store.conn, [*others, guild, *theirs]) == set(theirs)
    assert [derive_scope(h.store.conn, 102, [f"chat:{i}"]).scope for i in others[:2]] == [
        "private", "private"]


def test_an_ask_ends_with_its_night(h):
    dawn, six = datetime(2026, 10, 29, 6).timestamp(), datetime(2026, 10, 30, 6).timestamp()
    whispers(h, dawn + 600, (1, 101, "between us, ok?"), (101, 1, "ok"))     # night 2026-10-29
    that_night = whispers(h, six - 300, (1, 101, "my sister is getting married"))   # 05:55
    next_night = [*whispers(h, six + 60, (1, 101, "my brother is moving away")),
                  *whispers(h, six + 14 * 3600, (1, 101, "my dad is retiring"))]
    assert secret_lines(h.store.conn, that_night + next_night) == set(that_night)
    assert derive_scope(h.store.conn, 101, [f"chat:{next_night[0]}"]).scope == "private"
