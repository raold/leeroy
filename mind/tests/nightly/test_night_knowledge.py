"""The epistemic ledger (P1c Task 6): who knows each root and how, what they may pass on, what
they think others know, the "doesn't know you know" reminders, who could see a line, and the
check that no memory is weaker than what it rests on. Every name and line here is invented."""
import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import replace

import pytest

from conftest import make_mem, make_persona, run
from leeroy_mind.memory.privacy import derive_scope, secret_lines
from leeroy_mind.nightly import audience
from leeroy_mind.nightly.audience import (audience_of, bit_of, has_line_audience,
                                          has_utterance_columns, reason_of)
from leeroy_mind.nightly.evidence import load_items
from leeroy_mind.nightly.knowledge import (HOWS, PROBE_MAX_AGE_DAYS, PROBE_MIN_N, ROOT_PREFIXES,
                                           add_fact, believe, believes_knows, hops, is_private,
                                           is_secret, known_since, knowers, learn, may_tell,
                                           owner0_ok, parties, roots, scope_leaks,
                                           secret_probe_passed, secret_rooted, secrets_for,
                                           stage_knows, unknown_to)
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.storage.rows import MemoryRow
from nightly.conftest import IDENT, no_real_name

DAY = 86400.0


def knows_row(h, root: str, guid: int):
    return h.store.conn.execute("SELECT * FROM knows WHERE root = ? AND guid = ?",
                                (root, guid)).fetchone()


def scope(h, mid: int) -> tuple[str, int]:
    (m,) = h.store.get_memories([mid])
    return m.scope, m.scope_with


def probe(h, ts: float, n: int = 36, leaks: int = 2, rate: float = 0.056) -> None:
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO probe_run(ts, model, n, leaks, rate) "
                             "VALUES (?, ?, ?, ?, ?)", (ts, h.LIVE, n, leaks, rate))


def encounter(h, eid: int, a: int, b: int, *, private: int) -> None:
    with h.store.transaction():
        h.store.conn.execute(
            "INSERT INTO encounter(id, night, idx, a, b, setting, status, private) "
            "VALUES (?, ?, ?, ?, ?, 'late', 'done', ?)", (eid, h.night, eid, a, b, private))


def test_knows_from_the_day(h):
    line = h.say(h.T, 102, "pulled early again lol", channel="guild")
    seen = h.mem(101, "Belra pulled early.", evidence=(f"chat:{line}",), actors=(102,))
    h.mem(103, "Arkon says Belra pulled early.", source="hearsay", confidence=0.5, actors=(101, 102),
          evidence=(f"memory:{seen}", "encounter:4"))
    run(stage_knows(h.ctx()))
    root = f"chat:{line}"
    assert knowers(h.store, root) == {101, 103} and hops(h.store, root, 103) == 1 and hops(h.store, root, 101) == 0
    assert unknown_to(h.store, 101, 104, since=h.T - 1) == [root] and unknown_to(h.store, 101, 103, since=h.T - 1) == []
    told = knows_row(h, root, 103)
    assert (told["how"], told["via"], told["confidence"], told["learned"]) == ("told", 101, 0.5, h.T)


def test_reminders_wait_for_the_probe(h):
    line = h.say(h.T, 103, "between us belra failed her exam again", channel="whisper", to=104)
    with h.store.transaction():     # set by hand: the pipeline gives a secret root no overheard knower (defence in depth)
        f = add_fact(h.store, kind="said_about", root=f"chat:{line}", about=102, text="Belra failed her exam.", secret=1, now=h.T)
        learn(h.store, f"chat:{line}", 101, "overheard", ts=h.T); believe(h.store, 102, 101, f"chat:{line}", "no", now=h.T)
    run(stage_knows(h.ctx()))
    assert [m for m in h.rows(101) if (m.key or "").startswith("tom:")] == []          # no probe run yet: it waits
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO probe_run(ts, model, n, leaks, rate) VALUES (?, ?, 36, 2, 0.056)", (h.T, h.LIVE))
    run(stage_knows(h.ctx(night="2026-10-30")))
    (m,) = [m for m in h.rows(101) if m.key == f"tom:102:{f}"]
    assert m.text == "Belra failed her exam. Belra doesn't know you know." and (m.scope, m.scope_with) == ("secret", 0)   # a column, not a mark
    assert (m.kind, m.source, m.importance, m.actors, m.evidence) == (
        "reflection", "reflection", 5, (102,), (f"chat:{line}", f"rule:fact:{f}"))
    h.clock.advance(3600)
    run(stage_knows(h.ctx(night="2026-10-31")))                       # inserted only when absent
    (again,) = [x for x in h.rows(101) if (x.key or "").startswith("tom:")]
    assert (again.id, again.t_accessed) == (m.id, m.t_accessed)       # not even rewritten
    assert scope_leaks(h.store) == []


def test_long_secret_reminder_fits(h):                               # review C2: a 160-character fact used to overflow
    line = h.say(h.T, 102, "between us i failed again", channel="whisper", to=104)
    text = ("Belra failed her exam again and is scared her parents will find out before the results come in the post, "
            "so she asked everyone to keep it quiet for a while yet.")
    assert len(text) == 160
    with h.store.transaction():
        f = add_fact(h.store, kind="said_about", root=f"chat:{line}", about=102, text=text, secret=1, now=h.T)
        learn(h.store, f"chat:{line}", 101, "overheard", ts=h.T); believe(h.store, 102, 101, f"chat:{line}", "no", now=h.T)
        h.store.conn.execute("INSERT INTO probe_run(ts, model, n, leaks, rate) VALUES (?, ?, 36, 1, 0.028)", (h.T, h.LIVE))
    run(stage_knows(h.ctx()))                                        # no IntegrityError: the night goes on
    (m,) = [m for m in h.rows(101) if m.key == f"tom:102:{f}"]
    assert len(m.text) <= 200 and (m.scope, m.scope_with) == ("secret", 0) and m.text.endswith("Belra doesn't know you know.")


def test_roots_follow_memory_refs_and_stop_at_depth(h):
    m1 = h.mem(101, "Belra said she pulled early.", evidence=("chat:9",))
    m2 = h.mem(101, "Still thinking about that pull.", evidence=(f"memory:{m1}",))
    m3 = h.mem(101, "That pull is on my mind.", evidence=(f"memory:{m2}",))
    assert roots(h.store, {f"memory:{m3}"}) == {"chat:9"}
    assert roots(h.store, {f"memory:{m3}"}, depth=2) == set()
    assert roots(h.store, ["memory:99999", "story:4", "session:2", "owner:canon"]) == {"story:4", "session:2"}
    assert roots(h.store, [f"{p}1" for p in ROOT_PREFIXES]) == {f"{p}1" for p in ROOT_PREFIXES}
    loop = h.mem(102, "Round and round.", evidence=("memory:0",))
    with h.store.transaction():                                      # a cycle ends at the depth
        h.store.conn.execute("UPDATE memory SET evidence = ? WHERE id = ?",
                             (json.dumps([f"memory:{loop}", "event:3"]), loop))
    assert roots(h.store, [f"memory:{loop}"]) == {"event:3"}


def test_secret_roots_and_may_tell(h, caplog):
    his = h.say(h.T, 1, "got a new job btw", channel="whisper", to=102)
    hers = h.say(h.T + 1, 102, "omg congrats!!", channel="whisper", to=1)
    for line in (his, hers):                                         # private, not secret, from either end
        assert is_private(h.store, f"chat:{line}") and not is_secret(h.store, f"chat:{line}")
    with h.store.transaction():
        assert learn(h.store, f"chat:{hers}", 102, "witnessed", ts=h.T + 1)
        assert learn(h.store, f"chat:{his}", 102, "confided", via=1, ts=h.T)    # controller ruling I8: keeps 1
        assert not learn(h.store, f"chat:{his}", 102, "witnessed", ts=h.T + 5)  # the first learning wins
    for line in (his, hers):
        row = knows_row(h, f"chat:{line}", 102)
        assert (row["secret"], row["may_tell"]) == (0, 1) and may_tell(h.store, 102, f"chat:{line}")
    assert knows_row(h, f"chat:{his}", 102)["how"] == "confided" and hops(h.store, f"chat:{his}", 102) == 0

    asked = h.say(h.T + 2, 103, "dont tell anyone but i failed my exam", channel="whisper", to=104)
    with h.store.transaction():
        learn(h.store, f"chat:{asked}", 104, "confided", via=103, ts=h.T + 2)
    assert is_secret(h.store, f"chat:{asked}") and not is_private(h.store, f"chat:{asked}")
    assert (knows_row(h, f"chat:{asked}", 104)["secret"], knows_row(h, f"chat:{asked}", 104)["may_tell"]) == (1, 0)
    assert not may_tell(h.store, 104, f"chat:{asked}")

    guild = h.say(h.T + 3, 104, "anyone up for archavon", channel="guild")
    assert not is_secret(h.store, f"chat:{guild}") and not is_private(h.store, f"chat:{guild}")
    encounter(h, 7, 103, 104, private=1)
    encounter(h, 8, 101, 105, private=0)
    assert is_secret(h.store, "encounter:7") and not is_secret(h.store, "encounter:8")
    assert not is_private(h.store, "encounter:7") and not is_private(h.store, "encounter:8")

    news = h.say(h.T + 4, 101, "my sister is visiting next week", channel="whisper", to=1)
    with h.store.transaction():
        learn(h.store, f"chat:{news}", 101, "witnessed", ts=h.T + 4)
    assert may_tell(h.store, 101, f"chat:{news}")
    with h.store.transaction():                                      # a secret fact makes its root secret
        add_fact(h.store, kind="whisper_scene", root=f"chat:{news}", about=101,
                 text="Arkon's sister is visiting.", secret=1, now=h.T)
        add_fact(h.store, kind="feud_cause", root="encounter:8", about=101, text="", secret=1, now=h.T)
    assert is_secret(h.store, f"chat:{news}") and not is_private(h.store, f"chat:{news}")
    assert is_secret(h.store, "encounter:8")
    assert not may_tell(h.store, 101, f"chat:{news}")                # no longer, though she learned it before

    ask = h.say(h.T + 5, 105, "dont tell anyone but im switching mains", channel="guild")
    with caplog.at_level(logging.INFO, logger="leeroy_mind"), h.store.transaction():
        f = add_fact(h.store, kind="said_about", root=f"chat:{ask}", about=105, text="Ebbo is switching mains.",
                     secret=1, now=h.T)
    assert h.store.conn.execute("SELECT secret FROM fact WHERE id = ?", (f,)).fetchone()[0] == 0
    assert "fact_secret_refused" in caplog.text and not is_secret(h.store, f"chat:{ask}")   # controller ruling B2
    with h.store.transaction():                                      # even one set by hand
        h.store.conn.execute("UPDATE fact SET secret = 1 WHERE id = ?", (f,))
    assert not is_secret(h.store, f"chat:{ask}")

    papers = h.say(h.T + 10, 1, "got the papers today", channel="whisper", to=103)
    ok = h.say(h.T + 11, 103, "dont tell anyone ok", channel="whisper", to=1)
    assert not is_secret(h.store, f"chat:{papers}")                  # not yet one thread
    h.store.set_thread([papers, ok], h.store.open_thread("whisper:1:103", h.T + 10))
    assert is_secret(h.store, f"chat:{papers}")                      # re-review M2: her ask covers his line
    with h.store.transaction():
        learn(h.store, f"chat:{papers}", 103, "witnessed", ts=h.T + 12)
        learn(h.store, f"chat:{papers}", 105, "told", via=103, ts=h.T + 13, may_tell=1)   # never raised for a secret
    for g in (103, 105):
        assert (knows_row(h, f"chat:{papers}", g)["secret"], knows_row(h, f"chat:{papers}", g)["may_tell"]) == (1, 0)
    with pytest.raises(ValueError):
        learn(h.store, f"chat:{papers}", 104, "guessed", ts=h.T)
    assert "guessed" not in HOWS and not may_tell(h.store, 101, f"chat:{papers}")   # no row: nothing to tell


def test_the_ledger_and_the_writer_read_secrecy_alike(h):            # PR27: one helper
    plain = h.say(h.T, 102, "you around later?", channel="whisper", to=101)
    asked = h.say(h.T + 1, 101, "between us im rerolling", channel="whisper", to=102)
    first = h.say(h.T + 2, 1, "got the papers today", channel="whisper", to=103)
    then = h.say(h.T + 3, 103, "dont tell anyone ok", channel="whisper", to=1)
    h.store.set_thread([first, then], h.store.open_thread("whisper:1:103", h.T + 2))
    public = h.say(h.T + 4, 104, "keep it between us but im switching mains", channel="guild")
    assert secret_lines(h.store.conn, [plain, asked, first, then, public, 99999]) == {asked, first, then}
    for line, end in ((plain, 101), (asked, 102), (first, 103), (then, 1), (public, 104)):
        derived = derive_scope(h.store.conn, end, [f"chat:{line}"]).scope
        assert is_secret(h.store, f"chat:{line}") == (derived == "secret"), line


def test_known_since(h):
    root = "story:4"
    with h.store.transaction():
        for g in (101, 102, 103, 104, 105):
            learn(h.store, root, g, "witnessed", ts=h.T - 2 * DAY)
        learn(h.store, root, 106, "told", via=101, ts=h.T)
        learn(h.store, root, 107, "told", via=106, ts=h.T + 1)
        learn(h.store, "story:9", 201, "told", via=202, ts=h.T)        # a loop of tellings
        learn(h.store, "story:9", 202, "told", via=201, ts=h.T)
    assert known_since(h.store, root, before=h.T) == 5
    assert known_since(h.store, root, before=h.T + 1) == 6 and known_since(h.store, root, before=h.T - 3 * DAY) == 0
    assert known_since(h.store, "story:5", before=h.T) == 0
    assert (hops(h.store, root, 107), hops(h.store, root, 106), hops(h.store, root, 101)) == (2, 1, 0)
    assert hops(h.store, "story:9", 201) == 2 and hops(h.store, root, 999) == 0


def test_thinks_knows_two_levels(h):
    w = h.say(h.T, 103, "you free for a dungeon later?", channel="whisper", to=104)
    mine = h.say(h.T + 1, 102, "ty for the run", channel="whisper", to=1)
    g = h.say(h.T + 2, 101, "nice pull on that last one", channel="guild", to=102)
    with h.store.transaction():
        h.store.conn.execute("UPDATE chat_line SET audience = ? WHERE id = ?", (json.dumps([101, 102, 103, 1]), g))
        add_fact(h.store, kind="said_about", root=f"chat:{g}", about=102, text="Arkon praised Belra's pull.",
                 secret=0, now=h.T)
    h.mem(103, "I asked Dunna along for a dungeon.", evidence=(f"chat:{w}",), actors=(104,))
    h.mem(105, "Coros says he asked Dunna along.", source="hearsay", confidence=0.5, actors=(103, 104),
          evidence=(f"chat:{w}",))
    h.mem(102, "Thanked Testy for the run.", evidence=(f"chat:{mine}",), actors=(1,))
    run(stage_knows(h.ctx()))
    wr, gr = f"chat:{w}", f"chat:{g}"
    assert believes_knows(h.store, 103, 104, wr) == believes_knows(h.store, 104, 103, wr) == "yes"   # a whisper's two ends
    assert believes_knows(h.store, 105, 103, wr) == believes_knows(h.store, 103, 105, wr) == "yes"   # a told row
    assert believes_knows(h.store, 104, 105, wr) == "unsure"                                        # no row
    assert {r["guid"]: r["how"] for r in h.store.conn.execute("SELECT guid, how FROM knows WHERE root = ?", (gr,))} == {
        101: "witnessed", 102: "witnessed", 103: "overheard"}       # the line's own two never overheard it
    assert believes_knows(h.store, 101, 103, gr) == believes_knows(h.store, 102, 103, gr) == "no"   # they did not notice 103
    assert believes_knows(h.store, 102, 1, f"chat:{mine}") == "yes"
    assert h.store.conn.execute("SELECT count(*) FROM thinks_knows WHERE holder = 1").fetchone()[0] == 0  # he is not simulated
    with h.store.transaction():
        believe(h.store, 101, 103, gr, "yes", now=h.T + 5)
        believe(h.store, 101, 103, gr, "no", now=h.T + 6)
    assert believes_knows(h.store, 101, 103, gr) == "yes"                                           # never back to "no"
    assert [r[1] for r in h.store.conn.execute("PRAGMA table_info(thinks_knows)")] == [
        "holder", "other", "root", "belief", "updated"]


def test_real_name_fact_by_id(h):
    h.say(h.T, 101, "gn Quillon", channel="guild")                       # P1b keeps it capitalised in a close member's line
    h.say(h.T + 300, 103, "night Quillon, good raid", channel="guild")   # in 101's audience, and a speaker too
    h.say(h.T + 600, 104, "night all", channel="guild")                  # within 15 minutes of both
    h.say(h.T + 2000, 105, "anyone up for archavon", channel="guild")    # too late for either
    h.say(h.T + 3600, 102, "the quillon on this sword is bent", channel="guild")   # the lower-case word
    off = h.ctx(night="2026-10-28", window=(0.0, h.T + DAY))
    off.ident = replace(IDENT, real_name="")                             # no real name configured: skipped
    run(stage_knows(off))
    assert h.count("fact") == 0
    run(stage_knows(h.ctx()))
    assert [tuple(r) for r in h.store.conn.execute("SELECT kind, root, about, text, secret, holder FROM fact")] == [
        ("real_name", "rule:real_name", 1, "", 0, 0)]
    who = {r["guid"]: r["how"] for r in h.store.conn.execute("SELECT guid, how FROM knows WHERE root = 'rule:real_name'")}
    assert who == {101: "witnessed", 103: "witnessed", 104: "overheard"}
    h.say(h.T + DAY, 102, "QUILLON you legend", channel="guild")
    h.mem(105, "Arkon says Testy has a real name only close friends use.", source="hearsay", confidence=0.5,
          actors=(101,), evidence=("rule:real_name",), t=h.T + DAY)
    run(stage_knows(h.ctx(night="2026-10-30")))
    assert h.count("fact") == 1 and knows_row(h, "rule:real_name", 102)["how"] == "witnessed"   # once
    assert knows_row(h, "rule:real_name", 105)["how"] == "told"      # a memory citing it teaches it too
    assert h.store.conn.execute("SELECT count(*) FROM thinks_knows WHERE root = 'rule:real_name'").fetchone()[0] == 0
    assert not [m for g in (101, 102, 103, 104, 105) for m in h.rows(g) if (m.key or "").startswith("tom:")]
    assert no_real_name(h.store) == []


def test_audience_column_and_loud_fallback(h, caplog, monkeypatch):
    monkeypatch.setattr(audience, "_WARNED", set())
    w = h.say(h.T, 102, "you around later?", channel="whisper", to=1)
    pair = h.say(h.T + 10, 103, "you around later?", channel="whisper", to=104)
    at = h.T + 3600                                                     # an hour after the whispers
    line = h.say(at, 101, "pulling in 5", channel="guild")
    h.say(at - 600, 104, "brb", channel="guild")                        # 10 minutes before
    h.say(at + 840, 103, "ready", channel="party")                      # 14 minutes after, any channel
    h.say(at + 1200, 105, "back", channel="guild")                      # 20 minutes after: out
    h.say(at + 30, 1, "go", channel="guild")                            # the human is never in it
    h.store.upsert_cast([Member(guid=106, name="Fenna", race=2, class_=1, gender=1, level=70, rank=3,
                                online=True, map=0, zone=0, spec_tab=-1)], h.T)
    save_persona(h.store, make_persona(106, "Fenna"), source="seed", ts=h.T, race="Orc", class_name="Warrior")
    h.say(at + 45, 106, "inc", channel="guild")                         # a level-70 alt is not cast
    monkeypatch.setattr(audience, "AUDIENCE_COLUMN", "audience_not_built")   # PR5: no ALTER TABLE
    assert not has_line_audience(h.store)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert audience_of(h.store, w) == {102}                         # a whisper's cast ends
        assert audience_of(h.store, pair) == {103, 104}
        assert audience_of(h.store, line) == {101, 103, 104}
    assert caplog.text.count("audience_fallback: reason=no_column") == 1     # once a process, not once a line
    assert levels(caplog, "audience_fallback") == {"WARNING"}           # loud, never silent (review M8)
    monkeypatch.setattr(audience, "AUDIENCE_COLUMN", "audience")
    assert has_line_audience(h.store)
    with h.store.transaction():
        h.store.conn.execute("UPDATE chat_line SET audience = ? WHERE id = ?", (json.dumps([101, 103, 1, 106]), line))
    assert audience_of(h.store, line) == {101, 103}                     # the human (and the alt) dropped
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert audience_of(h.store, pair) == {103, 104} and audience_of(h.store, w) == {102}   # NULL: the fallback
    assert caplog.text.count("audience_fallback: reason=null_value") == 1
    assert levels(caplog, "audience_fallback") == {"WARNING"}
    assert audience_of(h.store, 99999) == frozenset()

    b = h.store.log_utterance(h.T + 100, 101, "guild", "bit", "the usual", [], None, None, bit="bit7", why="burst:weighted_draw")
    nul = h.store.log_utterance(h.T + 100, 102, "guild", "bit", "the usual", [], None, None)
    late = h.store.log_utterance(h.T + 300, 105, "guild", "bit", "the usual", [], None, None)
    early = h.store.log_utterance(h.T + 400, 104, "guild", "bit", "the usual", [], None, None)
    o = h.store.log_utterance(h.T + 200, 103, "guild", "open", "evening all", [], None, None)
    for g, bit, at in ((101, "bit8", 130), (102, "bit9", 130), (105, "bit3", 500), (104, "bit5", 370), (103, "bit4", 230)):
        h.store.record_bit(g, bit, h.T + at, h.night, False, None)
    assert has_utterance_columns(h.store)
    assert [bit_of(h.store, u) for u in (b, nul, late, early, o)] == ["bit7", "bit9", None, None, None]
    assert reason_of(h.store, b) == "burst:weighted_draw" and reason_of(h.store, o) is None
    row = h.store.conn.execute("SELECT * FROM utterance WHERE id = ?", (b,)).fetchone()
    assert bit_of(h.store, row) == "bit7" and reason_of(h.store, row) == "burst:weighted_draw"   # a row works too
    monkeypatch.setattr(audience, "BIT_COLUMN", "bit_not_built")
    assert not has_utterance_columns(h.store)                           # both columns, or it is False
    monkeypatch.setattr(audience, "REASON_COLUMN", "why_not_built")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert (bit_of(h.store, b), bit_of(h.store, nul), bit_of(h.store, o)) == ("bit8", "bit9", None)  # 30 s after a bit
        assert reason_of(h.store, b) is None and reason_of(h.store, o) is None
    assert caplog.text.count("utterance_reason_missing") == 1 and caplog.text.count("bit_fallback: reason=no_column") == 1
    assert levels(caplog, "utterance_reason_missing") == levels(caplog, "bit_fallback") == {"WARNING"}


def levels(caplog, event: str) -> set[str]:
    return {r.levelname for r in caplog.records if r.getMessage().startswith(event)}


def test_secrets_for_and_owner0_ok(h):
    w = h.say(h.T, 102, "dont tell anyone but i got the job", channel="whisper", to=101)
    root = f"chat:{w}"
    with h.store.transaction():
        add_fact(h.store, kind="life_news", root=root, about=102, text="Belra got the job.", secret=1, now=h.T)
        learn(h.store, root, 101, "confided", via=102, ts=h.T)
    assert not may_tell(h.store, 101, root)
    assert secrets_for(h.store, 101, {103}) == ["Belra got the job."] and secrets_for(h.store, 101, {1}) == ["Belra got the job."]
    with h.store.transaction():
        learn(h.store, root, 102, "witnessed", ts=h.T)
    assert secrets_for(h.store, 101, {102}) == [] and secrets_for(h.store, 103, {104}) == []   # she knows; 103 knows nothing
    his = h.say(h.T + 1, 1, "between us im moving next month", channel="whisper", to=101)
    with h.store.transaction():
        add_fact(h.store, kind="life_news", root=f"chat:{his}", about=1, text="Testy is moving.", secret=1, now=h.T + 1)
        learn(h.store, f"chat:{his}", 101, "confided", via=1, ts=h.T + 1)
    assert secrets_for(h.store, 101, {1}) == ["Belra got the job."]                 # his own secret is no secret from him
    assert secrets_for(h.store, 101, {104}) == ["Testy is moving.", "Belra got the job."]   # newest first
    plain = h.say(h.T + 3, 104, "my sister is visiting", channel="whisper", to=101)
    with h.store.transaction():                                      # private, not secret: hers to pass on
        add_fact(h.store, kind="life_news", root=f"chat:{plain}", about=104, text="Dunna's sister is visiting.",
                 secret=0, now=h.T + 3)
        learn(h.store, f"chat:{plain}", 101, "witnessed", ts=h.T + 3)
        add_fact(h.store, kind="noticed", root=root, about=102, text="Belra looked thrilled.", secret=0,
                 now=h.T + 5, holder=105)                            # another bystander's own words
    assert secrets_for(h.store, 101, {103}) == ["Testy is moving.", "Belra got the job."]
    cites = h.mem(101, "Belra got the job she wanted.", evidence=(root,), scope="secret", scope_with=102)
    open_line = h.say(h.T + 2, 104, "gz on the kill all", channel="guild")
    fine = h.mem(101, "Dunna cheered the kill.", evidence=(f"chat:{open_line}",))
    raised = h.mem(101, "Dunna cheered the kill, she told me.", evidence=(f"chat:{open_line}",),
                   scope="private", scope_with=104)
    assert not owner0_ok(h.store, [f"memory:{cites}"]) and not owner0_ok(h.store, [root])
    assert not owner0_ok(h.store, [f"chat:{plain}"]) and not owner0_ok(h.store, [f"memory:{raised}"])
    assert owner0_ok(h.store, [f"memory:{fine}", "event:5"])


def put(ctx, h, owner: int, text: str, evidence, *, shown=(), **kw) -> int:
    row = MemoryRow(id=None, owner=owner, kind="reflection", text=text, t_event=int(h.T), t_created=int(h.T),
                    t_accessed=int(h.T), access_n=0, actors=(), place=None, boss_id=None, raid_id=None,
                    importance=4, valence=0.0, source="reflection", evidence=tuple(evidence), **kw)
    with h.store.transaction():
        return ctx.writer.put(row, shown=shown)


def test_scope_leaks_finds_what_the_writer_would_not_write(h):   # controller rulings N4 and B2
    ctx = h.ctx()
    w = h.say(h.T, 1, "my exam went fine", channel="whisper", to=101)
    pair = h.say(h.T + 1, 102, "wanna run a heroic later?", channel="whisper", to=101)
    wr = f"chat:{w}"
    a = put(ctx, h, 101, "Testy's exam went fine.", [wr])
    assert scope(h, a) == ("private", 1) and scope_leaks(h.store) == []
    hers = put(ctx, h, 101, "Belra wants a heroic run.", [f"chat:{pair}"])
    both = put(ctx, h, 101, "Testy and Belra both had news.", [wr], shown=[f"memory:{hers}"])
    assert scope(h, hers) == ("private", 102) and scope(h, both) == ("private", 0)
    assert scope_leaks(h.store) == []                                # a partner of 0 is never weaker (re-review M1)
    open_whisper = h.mem(101, "Testy's exam went fine, he said.", evidence=(wr,))
    other_partner = h.mem(101, "Testy's exam went fine, it seems.", evidence=(wr,), scope="private", scope_with=103)
    encounter(h, 9, 103, 104, private=1)
    open_encounter = h.mem(103, "We talked late.", source="offscreen", evidence=("encounter:9",))
    owner0 = h.mem(0, "The guild heard news.", evidence=(f"memory:{a}",), scope="private", scope_with=101)
    h.mem(101, "Testy's exam went fine, old news.", evidence=(wr,), status="superseded")   # only active rows count
    assert scope_leaks(h.store) == [open_whisper, other_partner, open_encounter, owner0]
    news = h.say(h.T + 2, 104, "my sister is visiting", channel="whisper", to=1)
    with h.store.transaction():
        add_fact(h.store, kind="whisper_scene", root=f"chat:{news}", about=104, text="Dunna's sister is visiting.",
                 secret=1, now=h.T)
    cites = put(ctx, h, 104, "Told Testy my sister is visiting.", [f"chat:{news}"])
    assert scope(h, cites) == ("private", 1)                         # the writer sees a plain whisper
    assert secret_rooted(h.store, [f"memory:{cites}"]) and not secret_rooted(h.store, [f"memory:{a}"])


# -- beyond the brief's list ---------------------------------------------------------------------

def test_what_the_memories_unit_reads(h):
    line = h.say(h.T, 102, "pulled early again lol", channel="guild")
    root = f"chat:{line}"
    h.mem(0, "Belra pulled early.", evidence=(root,))                                 # the guild's own row
    h.mem(106, "Belra pulled early.", evidence=(root,))                               # not cast
    h.mem(104, "Belra pulled early.", evidence=(root,), t=h.T - 2 * DAY)              # before the window
    h.mem(103, "Belra pulled early, I saw.", evidence=(root,), status="superseded")   # not active
    h.mem(105, "Late talk with Arkon.", source="offscreen", evidence=("encounter:6", root))  # a scene's other refs: gossip only
    run(stage_knows(h.ctx()))
    assert knowers(h.store, root) == set() and knowers(h.store, "encounter:6") == {105}


def test_a_root_s_own_parties_know_it(h):
    his = h.say(h.T, 1, "got a new job btw", channel="whisper", to=102)
    h.mem(102, "Testy told me he got a new job.", evidence=(f"chat:{his}",), actors=(1,))
    run(stage_knows(h.ctx()))
    root = f"chat:{his}"
    encounter(h, 7, 103, 104, private=0)
    assert knowers(h.store, root) == {102} and parties(h.store, root) == {1, 102}
    assert parties(h.store, "encounter:7") == {103, 104} and parties(h.store, "story:1") == frozenset()
    assert unknown_to(h.store, 102, 1, since=h.T - 1) == [] and unknown_to(h.store, 102, 104, since=h.T - 1) == [root]
    (item,) = load_items(h.store, [root], ident=IDENT)
    assert item.knowers == {1, 102}                                  # he said it: no tom claim can say he does not know
    assert item.text == "Testy: got a new job btw"                   # shown as what the guild calls him
    with h.store.transaction():
        for i in range(5):
            learn(h.store, f"story:{i}", 102, "witnessed", ts=h.T + 1 + i)
    assert unknown_to(h.store, 102, 104, since=h.T - 1) == ["story:4", "story:3", "story:2"]


def test_a_secret_only_the_ledger_sees_is_asked_for(h):
    encounter(h, 6, 101, 102, private=1)                             # a 2am confession
    plain = h.say(h.T, 104, "my sister is visiting", channel="whisper", to=105)
    with h.store.transaction():                                      # set by hand: the pipeline never does this
        c = add_fact(h.store, kind="confession", root="encounter:6", about=101, text="Arkon misses home.",
                     secret=1, now=h.T)
        w = add_fact(h.store, kind="whisper_scene", root=f"chat:{plain}", about=104,
                     text="Dunna's sister is visiting.", secret=1, now=h.T)   # a confidential fact (Task 8)
        for root, p in (("encounter:6", 101), (f"chat:{plain}", 104)):
            learn(h.store, root, 103, "overheard", ts=h.T)
            believe(h.store, p, 103, root, "no", now=h.T)
    probe(h, h.T)
    run(stage_knows(h.ctx()))
    assert {m.key: (m.scope, m.scope_with) for m in h.rows(103) if m.key} == {
        f"tom:101:{c}": ("secret", 0), f"tom:104:{w}": ("secret", 0)}   # the writer alone would say open, and private
    assert scope_leaks(h.store) == []


def test_open_reminders_need_no_probe_and_use_her_own_fact(h):
    encounter(h, 5, 101, 102, private=0)
    with h.store.transaction():
        own = add_fact(h.store, kind="noticed", root="encounter:5", about=101, text="Arkon and Belra looked tense.",
                       secret=0, now=h.T, holder=103)
        shared = add_fact(h.store, kind="said_about", root="encounter:5", about=101, text="Arkon and Belra argued.",
                          secret=0, now=h.T)
        assert add_fact(h.store, kind="noticed", root="encounter:5", about=101, text="other words", secret=0,
                        now=h.T, holder=103) == own
        for g in (103, 104):
            learn(h.store, "encounter:5", g, "overheard", ts=h.T)
            believe(h.store, 101, g, "encounter:5", "no", now=h.T)
        believe(h.store, 102, 103, "encounter:5", "no", now=h.T)
        believe(h.store, 102, 104, "encounter:5", "yes", now=h.T)     # she knows 104 knows: no reminder
        learn(h.store, "encounter:5", 102, "witnessed", ts=h.T)       # witnessed counts as overheard does
        believe(h.store, 101, 102, "encounter:5", "no", now=h.T)
        learn(h.store, "encounter:5", 105, "overheard", ts=h.T - 15 * DAY)   # older than the look-back
        believe(h.store, 101, 105, "encounter:5", "no", now=h.T)
    run(stage_knows(h.ctx()))                                        # no probe has run
    got = {(m.owner, m.key): (m.text, m.scope) for g in (102, 103, 104, 105) for m in h.rows(g) if m.key}
    assert got == {(103, f"tom:101:{own}"): ("Arkon and Belra looked tense. Arkon doesn't know you know.", "open"),
                   (103, f"tom:102:{own}"): ("Arkon and Belra looked tense. Belra doesn't know you know.", "open"),
                   (104, f"tom:101:{shared}"): ("Arkon and Belra argued. Arkon doesn't know you know.", "open"),
                   (102, f"tom:101:{shared}"): ("Arkon and Belra argued. Arkon doesn't know you know.", "open")}


def _argued(h) -> int:
    """A scene 103 overheard, with a fact, and Arkon believing 103 does not know: one reminder due."""
    encounter(h, 5, 101, 102, private=0)
    with h.store.transaction():
        f = add_fact(h.store, kind="said_about", root="encounter:5", about=101, text="Arkon and Belra argued.",
                     secret=0, now=h.T)
        learn(h.store, "encounter:5", 103, "overheard", ts=h.T)
        believe(h.store, 101, 103, "encounter:5", "no", now=h.T)
    return f


def test_a_failing_reminders_unit_fails_alone(h):
    f = _argued(h)
    ctx = h.ctx()
    ctx.reasoner.llm.fail_next()                                     # the reminder's embedding fails
    run(stage_knows(ctx))                                            # no exception: the night goes on
    assert ctx.failures["knows"] == 1 and ctx.done("knows", "memories") and ctx.done("knows", "audience")
    assert not ctx.done("knows", "reminders") and not [m for m in h.rows(103) if m.key]
    run(stage_knows(h.ctx()))                                        # the next pass writes it
    assert [m.key for m in h.rows(103) if m.key] == [f"tom:101:{f}"]


def test_a_reminder_written_meanwhile_is_not_written_again(h):
    f = _argued(h)
    gate = asyncio.Event()
    ctx = h.ctx(gate=gate)

    async def go() -> int:
        task = asyncio.create_task(stage_knows(ctx))
        for _ in range(1000):                                        # until the embedding is in flight
            if ctx.reasoner.llm.calls:
                break
            await asyncio.sleep(0)
        mine = h.mem(103, "Arkon and Belra had words.", kind="reflection", source="reflection",
                     key=f"tom:101:{f}", evidence=("encounter:5", f"rule:fact:{f}"))
        gate.set()
        await task
        return mine
    mine = run(go())
    assert ctx.reasoner.llm.calls and [(m.id, m.text) for m in h.rows(103) if m.key] == [(mine, "Arkon and Belra had words.")]


def test_the_probe_gate(h):
    assert not secret_probe_passed(h.store, now=h.T)
    probe(h, h.T - (PROBE_MAX_AGE_DAYS + 1) * DAY)
    assert not secret_probe_passed(h.store, now=h.T)                 # too old
    probe(h, h.T - DAY, n=PROBE_MIN_N - 1)
    assert not secret_probe_passed(h.store, now=h.T)                 # too few samples
    probe(h, h.T - DAY + 1, rate=0.111)
    assert not secret_probe_passed(h.store, now=h.T) and secret_probe_passed(h.store, now=h.T, max_rate=0.2)
    probe(h, h.T - DAY + 2, rate=0.1)
    assert secret_probe_passed(h.store, now=h.T)                     # the newest row decides
    assert secret_probe_passed(h.store, now=h.T - DAY + 2 + PROBE_MAX_AGE_DAYS * DAY)
    assert not secret_probe_passed(h.store, now=h.T - DAY + 2 + PROBE_MAX_AGE_DAYS * DAY + 1)


def test_real_name_facts_never_hold_text(h):
    with h.store.transaction():
        f = add_fact(h.store, kind="real_name", root="rule:real_name", about=1, text="Quillon", secret=0, now=h.T)
    assert h.store.conn.execute("SELECT text FROM fact WHERE id = ?", (f,)).fetchone()[0] == ""
    assert no_real_name(h.store) == []


# -- fix round 1 (review-task6.md) ---------------------------------------------------------------

def reads(h, fn) -> list[str]:
    """The statements fn() runs that read the ledger's own tables (knows, fact, thinks_knows)."""
    seen: list[str] = []
    h.store.conn.set_trace_callback(seen.append)
    try:
        fn()
    finally:
        h.store.conn.set_trace_callback(None)
    return [s for s in seen if re.search(r"\b(?:FROM|JOIN)\s+(?:knows|fact|thinks_knows)\b", s)]


def test_reminders_read_the_ledger_a_fixed_number_of_times(h):       # review I1
    f = _argued(h)
    with h.store.transaction():          # two weeks of learning: rows with facts, nobody thinking they don't know
        for i in range(400):
            add_fact(h.store, kind="said_about", root=f"story:{i}", about=101, text="Something happened.",
                     secret=0, now=h.T)
            learn(h.store, f"story:{i}", 101 + i % 5, "overheard", ts=h.T)
        believe(h.store, 102, 103, "encounter:5", "no", now=h.T)
    ctx = h.ctx()
    for unit in ("memories", "audience"):
        assert ctx.skip("knows", unit)
    ledger = reads(h, lambda: run(stage_knows(ctx)))
    assert len(ledger) <= 4, ledger                                  # not once per knows row
    assert sorted(m.key for m in h.rows(103) if m.key) == [f"tom:101:{f}", f"tom:102:{f}"]


def test_the_memories_unit_reads_more_memories_than_sqlite_binds(h):   # review M1 and M6
    h.store.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 600)
    with h.store.transaction():
        for i in range(700):
            h.store.insert_memory(make_mem(owner=101 + i % 5, text="We killed a boss.", t_event=int(h.T),
                                           t_created=int(h.T), t_accessed=int(h.T), evidence=(f"event:{i}",)))
    ctx = h.ctx()
    ledger = reads(h, lambda: run(stage_knows(ctx)))
    assert not ctx.failures and h.count("knows") == 700
    assert len([s for s in ledger if re.search(r"\bFROM\s+fact\b", s)]) <= 3   # the secret facts once, not per root


def test_the_human_never_holds_a_belief(h, caplog):                  # review M2
    line = h.say(h.T, 103, "between us im burnt out", channel="whisper", to=104)
    root = f"chat:{line}"
    with caplog.at_level(logging.INFO, logger="leeroy_mind"), h.store.transaction():
        believe(h.store, 1, 103, root, "no", now=h.T)
    assert believes_knows(h.store, 1, 103, root) == "unsure"
    assert [r.levelname for r in caplog.records if r.getMessage().startswith("believe_refused")] == ["WARNING"]
    with h.store.transaction():                                      # a row set past believe() makes no reminder of him
        add_fact(h.store, kind="said_about", root=root, about=103, text="Coros is burnt out.", secret=1, now=h.T)
        learn(h.store, root, 105, "overheard", ts=h.T)
        h.store.conn.execute("INSERT INTO thinks_knows(holder, other, root, belief, updated) "
                             "VALUES (1, 105, ?, 'no', ?)", (root, h.T))
    probe(h, h.T)
    run(stage_knows(h.ctx()))
    assert not [m for m in h.rows(105) if m.key]


def test_a_fact_ref_carries_its_facts_secrecy(h):                   # review M3
    asked = h.say(h.T, 103, "dont tell anyone but i failed", channel="whisper", to=104)
    plain = h.say(h.T + 1, 104, "my sister is visiting", channel="whisper", to=105)
    public = h.say(h.T + 2, 105, "gz all", channel="guild")
    with h.store.transaction():
        s = add_fact(h.store, kind="said_about", root=f"chat:{asked}", about=103, text="Coros failed.", secret=1,
                     now=h.T)
        p = add_fact(h.store, kind="life_news", root=f"chat:{plain}", about=104, text="Dunna's sister is visiting.",
                     secret=0, now=h.T)
        o = add_fact(h.store, kind="said_about", root=f"chat:{public}", about=105, text="Ebbo cheered.", secret=0,
                     now=h.T)
    assert is_secret(h.store, f"rule:fact:{s}") and not is_private(h.store, f"rule:fact:{s}")
    assert is_private(h.store, f"rule:fact:{p}") and not is_secret(h.store, f"rule:fact:{p}")
    assert not is_secret(h.store, f"rule:fact:{o}") and not is_private(h.store, f"rule:fact:{o}")
    assert not is_secret(h.store, "rule:fact:99999")
    leaky = h.mem(103, "Coros failed, I heard.", evidence=(f"rule:fact:{s}",))    # open, past the writer
    fine = h.mem(103, "Ebbo cheered us on.", evidence=(f"rule:fact:{o}",))
    assert scope_leaks(h.store) == [leaky] and secret_rooted(h.store, [f"memory:{leaky}"])
    assert not owner0_ok(h.store, [f"rule:fact:{s}"]) and not owner0_ok(h.store, [f"rule:fact:{p}"])
    assert owner0_ok(h.store, [f"memory:{fine}"])


def test_secrets_for_counts_what_she_was_part_of(h):                # review M4
    w = h.say(h.T, 102, "dont tell anyone but i got the job", channel="whisper", to=101)
    encounter(h, 6, 101, 103, private=1)
    with h.store.transaction():                                      # no ledger row for 101 at all
        add_fact(h.store, kind="life_news", root=f"chat:{w}", about=102, text="Belra got the job.", secret=1, now=h.T)
        add_fact(h.store, kind="confession", root="encounter:6", about=103, text="Coros misses home.", secret=1,
                 now=h.T + 1)
    assert secrets_for(h.store, 101, {104}) == ["Coros misses home.", "Belra got the job."]
    assert secrets_for(h.store, 102, {104}) == ["Belra got the job."] and secrets_for(h.store, 104, {105}) == []
    assert secrets_for(h.store, 101, {102}) == ["Coros misses home."]   # she was in the whisper, not the scene
    assert secrets_for(h.store, 101, {103}) == ["Belra got the job."]   # he was in the scene, not the whisper


def test_only_a_whispers_own_two_may_pass_it_on(h):                 # review M5, ruling I8
    his = h.say(h.T, 1, "got a new job btw", channel="whisper", to=102)
    guild = h.say(h.T + 1, 102, "gz on the kill all", channel="guild")
    root, open_root = f"chat:{his}", f"chat:{guild}"
    with h.store.transaction():
        learn(h.store, root, 102, "confided", via=1, ts=h.T)
        learn(h.store, root, 104, "told", via=102, ts=h.T + 1)            # her one hop
        learn(h.store, root, 105, "told", via=104, ts=h.T + 2, may_tell=1)  # a second hop asks to pass it on
        learn(h.store, open_root, 104, "told", via=102, ts=h.T + 3)      # a public line spreads
    assert may_tell(h.store, 102, root) and not may_tell(h.store, 104, root) and not may_tell(h.store, 105, root)
    assert may_tell(h.store, 104, open_root)
