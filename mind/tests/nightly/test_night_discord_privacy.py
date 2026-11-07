"""The guild's Discord in memory and the night (ruling R7): a DM (`dm:<guid>`) is private exactly
as a whisper is, and a line on an officers' Discord channel is known to that channel's audience
alone, never passed on and never public. Invented text only."""
import sqlite3
from types import SimpleNamespace

from conftest import make_mem, run
from leeroy_mind.discord.channels import (is_discord, is_private, is_public, is_restricted,
                                          private_sql)
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.extract import Extractor
from leeroy_mind.memory.privacy import Scope, derive_scope
from leeroy_mind.memory.recall import Recall
from leeroy_mind.memory.threads import ThreadTracker
from leeroy_mind.memory.writer import Embedder, MemoryCandidate, MemoryWriter
from leeroy_mind.nightly import gossip, knowledge, lexicon, relations, stories, vocab
from leeroy_mind.nightly.audience import audience_of
from leeroy_mind.nightly.knowledge import _learn_audiences
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_llm import FakeLLM, fake_embed
from nightly.conftest import IDENT

DM = "dm:102"
OFFICERS = "discord:officer-chat"
GENERAL = "discord:general"
OFFICER_AUDIENCE = (1, 102, 104)


def put(h, t, guid, text, channel, *, to=0, audience=None) -> int:
    """A stored line on `channel` (with its audience, as the pipeline records one), and for a
    member's line the utterance that goes with it."""
    h.clock.set(t)
    origin = "human" if guid == h.human else "mind"
    sid = h.store.open_session()
    line = h.store.insert_chat_line(
        Chat(ts=int(t * 1000), channel=channel, from_guid=guid, from_name=h._name(guid),
             to_guid=to, text=text, map=0, zone=0),
        session_id=sid, origin=origin, audience=audience)
    if origin == "mind":
        h.store.log_utterance(t, guid, channel, "reply_human", text, [], None, sid, to_guid=to)
    return line


def recall(h) -> Recall:
    return Recall(h.store, Embedder(LLMScheduler(FakeLLM(), clock=h.clock), h.EMB), h.clock,
                  tau_days=5.0)


def writer(h) -> MemoryWriter:
    return MemoryWriter(h.store, Embedder(LLMScheduler(FakeLLM(), clock=h.clock), h.EMB), h.clock,
                        human=lambda: IDENT)


def test_which_channels_are_private_restricted_and_public():
    assert is_private("whisper") and is_private(DM) and is_private("dm:abc")
    assert not any(is_private(c) for c in ("guild", "officer", "say", GENERAL, OFFICERS, None))
    assert is_restricted(OFFICERS) and is_restricted("discord:applications")
    assert is_restricted("discord:no-such-channel")  # a channel the layout lacks: fail closed
    assert not any(is_restricted(c) for c in ("guild", "officer", "whisper", DM, GENERAL))
    assert [c for c in ("guild", "officer", "whisper", DM, GENERAL, OFFICERS) if is_public(c)] == [
        "guild", "officer", GENERAL]
    assert is_discord(DM) and is_discord(OFFICERS) and not is_discord("whisper")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(channel TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(c,) for c in (
        "whisper", DM, "dm:7", "guild", GENERAL, "xdm:1", "DM:1")])
    assert [r[0] for r in conn.execute(f"SELECT channel FROM t WHERE {private_sql()}")] == [
        "whisper", DM, "dm:7"]


def test_a_dm_is_scoped_like_a_whisper(h):
    his = put(h, h.T, 1, "my sister is getting married next month", DM, to=102)
    asked = put(h, h.T + 10, 1, "dont tell anyone but i might quit", DM, to=102)
    hers = put(h, h.T + 20, 102, "aw congrats to her", DM, to=1)
    guild = put(h, h.T + 30, 1, "raid at eight tonight", "guild")
    conn = h.store.conn
    assert derive_scope(conn, 102, [f"chat:{his}"]) == Scope("private", 1, False)
    assert derive_scope(conn, 102, [f"chat:{hers}"]) == Scope("private", 1, False)
    assert derive_scope(conn, 102, [f"chat:{asked}"]).scope == "secret"
    assert derive_scope(conn, 103, [f"chat:{his}"]) == Scope("private", 0, False)
    assert derive_scope(conn, 102, [f"chat:{guild}"]) == Scope(public=True)
    # the writer stores what the evidence gives, whatever the caller asks
    mid = run(writer(h).write(MemoryCandidate(
        owner=102, kind="conversation", text="Testy told me his sister is getting married",
        t_event=h.T, actors=(102, 1), importance=6, valence=0.5, source="extract",
        evidence=(f"chat:{his}",), confidence=0.9))).id
    (m,) = h.store.get_memories([mid])
    assert (m.scope, m.scope_with) == ("private", 1)


def test_a_dm_memory_reaches_only_her_dm_with_him(h):
    his = put(h, h.T, 1, "my sister is getting married next month", DM, to=102)
    mid = h.mem(102, "Testy told me his sister is getting married.", actors=(102, 1),
                evidence=(f"chat:{his}",), scope="private", scope_with=1)
    r = recall(h)

    def got(channel, to=0, owner=102):
        return [m.id for m in run(r.recall(owner, "sister married", present=[1], addressee=1,
                                           channel=channel, to_guid=to))]
    assert got(DM, 1) == [mid]
    for channel, to in (("guild", 0), ("whisper", 1), (GENERAL, 0), (OFFICERS, 0), ("dm:103", 1)):
        assert got(channel, to) == [], channel
    assert got(DM, 1, owner=103) == []


def test_the_night_reads_a_dm_as_private(h):
    his = put(h, h.T, 1, "my sister is getting married next month", DM, to=102)
    hers = put(h, h.T + 20, 102, "aw congrats to her, so exciting", DM, to=1)
    guild = put(h, h.T + 25, 103, "nice raid everyone, see you thursday", "guild")
    store = h.store
    root = f"chat:{his}"
    assert knowledge.is_private(store, root) and not knowledge.is_secret(store, root)
    assert knowledge.could_have_seen(store, root) == {1, 102}
    # with no stored audience the fallback gives a DM its two ends, not whoever spoke nearby
    assert audience_of(store, hers) == {102} and 103 in audience_of(store, guild)
    # the ledger: a fact on it is never overheard, and one outside it may not pass it on
    knowledge.add_fact(store, kind="life_news", root=root, about=1, text="his sister marries",
                       secret=0, now=h.T + 30)
    ctx = h.ctx()
    _learn_audiences(ctx, store)
    assert store.conn.execute("SELECT count(*) FROM knows WHERE root = ?", (root,)).fetchone()[0] == 0
    knowledge.learn(store, root, 103, "told", via=102, ts=h.T + 40)
    knowledge.learn(store, root, 102, "witnessed", ts=h.T + 40)
    tell = dict(store.conn.execute("SELECT guid, may_tell FROM knows WHERE root = ?", (root,)))
    assert tell == {103: 0, 102: 1}  # her own discretion, as for a whisper; never the hearer's
    # none of the night's public readers take it; they do take a guild line (the control)
    assert lexicon.public_line(store, guild, human=1) is not None
    for line in (his, hers):
        assert lexicon.public_line(store, line, human=1) is None
        row = store.conn.execute("SELECT * FROM chat_line WHERE id = ?", (line,)).fetchone()
        assert vocab._line(SimpleNamespace(store=store, human=1), row, {1, 101, 102, 103}) is None
        assert stories._public_node(store, f"chat:{line}") is None
    shared = h.mem(102, "Testy told me his sister is getting married.", actors=(102, 1),
                   evidence=(root,), scope="private", scope_with=1)
    open_one = h.mem(102, "We had a good raid tonight.", actors=(102,))
    rows = store.get_memories([shared, open_one])
    assert Extractor._public(SimpleNamespace(_store=store), rows) == {open_one}
    told = h.mem(103, "Belra says Testy's sister is getting married.", actors=(103, 102),
                 evidence=(root,), scope="private", scope_with=0, source="hearsay")
    (m,) = store.get_memories([told])
    assert not gossip.source_allowed(ctx, m, 103, 104, require_score=False)


def test_dm_threads_are_extracted_like_whisper_threads(h):
    tracker = ThreadTracker(h.store)
    chat = Chat(ts=0, channel=DM, from_guid=102, from_name="Belra", to_guid=1, text="hey you",
                map=0, zone=0)
    line = h.store.insert_chat_line(chat, session_id=None, origin="mind")
    assert tracker.on_line_sent(line, chat) is None
    (row,) = h.store.chat_lines([line])
    key = h.store.conn.execute("SELECT key FROM chat_thread WHERE id = ?",
                               (row.thread_id,)).fetchone()[0]
    assert key == DM
    stranger = Chat(ts=0, channel="dm:999", from_guid=999, from_name="Nobody", to_guid=1,
                    text="hello there", map=0, zone=0)
    other = h.store.insert_chat_line(stranger, session_id=None, origin="game")
    tracker.on_line_sent(other, stranger)
    assert h.store.chat_lines([other])[0].thread_id is None  # as a whisper with no cast member


def test_an_officers_line_is_known_to_its_audience_only(h, monkeypatch):
    line = put(h, h.T, 102, "we should move arkon to raider next week", OFFICERS,
               audience=OFFICER_AUDIENCE)
    store = h.store
    root = f"chat:{line}"
    assert audience_of(store, line) == {102, 104}
    assert knowledge.is_restricted(store, root) and knowledge.is_private(store, root)
    assert not knowledge.is_secret(store, root)
    knowledge.add_fact(store, kind="said_about", root=root, about=101, text="arkon to raider",
                       secret=0, now=h.T + 30)
    _learn_audiences(h.ctx(), store)
    knowledge.learn(store, root, 102, "witnessed", ts=h.T + 40)
    tell = dict(store.conn.execute("SELECT guid, may_tell FROM knows WHERE root = ?", (root,)))
    assert tell == {102: 0, 104: 0}  # its audience knows it, and nobody passes it on
    assert lexicon.public_line(store, line, human=1) is None
    assert stories._public_node(store, root) is None
    row = store.conn.execute("SELECT * FROM chat_line WHERE id = ?", (line,)).fetchone()
    assert vocab._line(SimpleNamespace(store=store, human=1), row, {1, 101, 102, 104}) is None
    mid = h.mem(102, "I think Arkon should be a raider.", actors=(102, 101), evidence=(root,))
    other = h.mem(102, "Arkon tanked the drake well.", actors=(102, 101))
    rows = store.get_memories([mid, other])
    assert Extractor._public(SimpleNamespace(_store=store), rows) == {other}
    guild = put(h, h.T + 50, 102, "arkon tanked the drake so well", "guild",
                audience=(101, 102, 103, 104, 105))
    said = h.mem(102, "I told the guild Arkon tanked well.", actors=(102, 101),
                 evidence=(f"chat:{guild}",))
    ctx = h.ctx()
    (m, control) = store.get_memories([mid, said])
    assert gossip.source_allowed(ctx, control, 102, 101, require_score=False)  # the control
    assert not gossip.source_allowed(ctx, m, 102, 104, require_score=False)
    assert not gossip.source_allowed(ctx, m, 102, 101, require_score=False)
    # a fresh one, before the ledger has a row, even when her discretion would let it go
    fresh_line = put(h, h.T + 60, 102, "bank needs more flasks before thursday", OFFICERS,
                     audience=OFFICER_AUDIENCE)
    fresh = h.mem(102, "I said the bank needs more flasks.", actors=(102,),
                  evidence=(f"chat:{fresh_line}",))
    monkeypatch.setattr(gossip, "discretion_roll", lambda teller, root: 1.0)
    (f,) = store.get_memories([fresh])
    assert not gossip.source_allowed(ctx, f, 102, 101, require_score=False)
    r = recall(h)

    def got(channel):
        return [x.id for x in run(r.recall(102, "arkon raider", present=[], addressee=None,
                                           channel=channel, to_guid=0))]
    assert mid in got(OFFICERS)
    for channel in ("guild", "officer", GENERAL, "discord:applications"):
        assert mid not in got(channel), channel


def test_a_public_discord_line_is_public(h):
    line = put(h, h.T, 102, "anyone up for heroics later", GENERAL,
               audience=(1, 101, 102, 103, 104, 105))
    root = f"chat:{line}"
    assert not knowledge.is_private(h.store, root) and not knowledge.is_restricted(h.store, root)
    assert audience_of(h.store, line) == {101, 102, 103, 104, 105}


def _row(h, owner, text, evidence, **kw):
    return make_mem(owner=owner, text=text, evidence=tuple(evidence), t_event=int(h.T),
                    t_created=int(h.T), t_accessed=int(h.T), source="extract",
                    kind="conversation", embedding=pack_vec(fake_embed(text)), **kw)


def test_the_restriction_is_transitive_and_recorded_at_write_time(h):
    """R10: a memory resting on an officers' line through memory: refs is as restricted as one
    citing it, and the writer stores both so (private to nobody), so every open-only reader
    leaves them out. Rows stored open by any other path are left out by their roots too."""
    line = put(h, h.T, 102, "we should demote arkon next week", OFFICERS,
               audience=OFFICER_AUDIENCE)
    w = writer(h)
    direct = w.put(_row(h, 102, "I think Arkon should be demoted.", [f"chat:{line}"],
                        actors=(102, 101)))
    derived = w.put(_row(h, 102, "I worry Arkon will be demoted soon.", [f"memory:{direct}"],
                         actors=(102, 101)))
    deeper = w.put(_row(h, 102, "Arkon seems tense lately.", [f"memory:{derived}"],
                        actors=(102, 101)))
    assert [(m.scope, m.scope_with) for m in h.store.get_memories([direct, derived, deeper])] == [
        ("private", 0)] * 3
    # rows stored open by another path (the reviewer's probe): the roots still tell
    open_direct = h.mem(102, "I said Arkon should be demoted.", actors=(102, 101),
                        evidence=(f"chat:{line}",))
    open_derived = h.mem(102, "I fear Arkon gets demoted.", actors=(102, 101),
                         evidence=(f"memory:{open_direct}",), kind="reflection",
                         source="reflection")
    assert derive_scope(h.store.conn, 102, [f"memory:{open_derived}"]) == Scope("private", 0)
    control = h.mem(102, "Arkon tanked the drake well.", actors=(102, 101))
    every = [direct, derived, deeper, open_direct, open_derived]
    r = recall(h)

    def got(channel):
        return {m.id for m in run(r.recall(102, "arkon demoted tense", present=[101],
                                           addressee=101, channel=channel, to_guid=0))}
    for channel in ("guild", GENERAL, "officer", "whisper", "discord:applications"):
        assert not got(channel) & set(every), channel
    assert control in got("guild")
    rows = h.store.get_memories([*every, control])
    assert Extractor._public(SimpleNamespace(_store=h.store), rows) == {control}
    for sql in (relations._NEWEST_OPEN, relations._WEIGHTIEST_OPEN):  # the notes and the pins
        assert relations._open_ids(h.store, sql, 102, 101, 12) == [control]


def test_extraction_shows_only_open_memories(h):
    """R10: Extractor._public needs scope 'open' as well as public lines."""
    private = h.mem(102, "Testy is moving house soon.", actors=(102, 1), scope="private",
                    scope_with=1)
    nobody = h.mem(102, "Arkon and Coros fell out.", actors=(102,), scope="private",
                   scope_with=0)
    open_one = h.mem(102, "We cleared the drake tonight.", actors=(102,))
    rows = h.store.get_memories([private, nobody, open_one])
    assert Extractor._public(SimpleNamespace(_store=h.store), rows) == {open_one}


def test_private_sql_takes_a_column_name_only():
    assert private_sql("l.channel").startswith("(l.channel = 'whisper'")
    for bad in ("channel; DROP TABLE x", "1channel", "channel)", ""):
        try:
            private_sql(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
