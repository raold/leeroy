"""A memory's scope is never wider than its evidence (memory.privacy.resolve, memory.scopes).

Every text here is invented filler. The break cases are those of the 2026-11-17 review of the
lesson walk: a stored-open memory that reaches scoped evidence only through other memories."""
import json
from types import SimpleNamespace

import pytest

from conftest import HUMAN, make_mem, run, seed_test_cast
from leeroy_mind.learn import audience
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory import callbacks, privacy, scope_cli, scopes
from leeroy_mind.memory.privacy import GROUP, OFFICERS, STRICTEST, derive_scope, effective_scopes
from leeroy_mind.memory.recall import Recall
from leeroy_mind.memory.writer import Embedder
from leeroy_mind.nightly import knowledge, life_contact, relations
from leeroy_mind.protocol import Chat
from leeroy_mind.storage import db as db_mod
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_llm import FakeLLM, fake_embed

ASK = "fixture: don't tell anyone"
WORD = "kestrel"  # the one searchable word every fixture memory carries
STATES = ("unchecked", "checked", "raised")


def line(store, channel, frm, to=0, text="fixture", audience=None):
    return store.insert_chat_line(
        Chat(ts=0, channel=channel, from_guid=frm, from_name=f"M{frm}", to_guid=to, text=text,
             map=0, zone=0), session_id=None, origin="game", audience=audience)


def raw(store, owner, *evidence, scope="open", scope_with=0, kind="reflection", actors=(HUMAN,),
        key=None):
    """A memory row as another path left it: stored with the scope given, whatever its
    evidence says, and unchecked (straight SQL, past the store and the writer)."""
    t = int(store.clock.now())
    mid = store.conn.execute(
        "INSERT INTO memory(owner, kind, text, t_event, t_created, t_accessed, importance, "
        "valence, source, evidence, actors, scope, scope_with, key, embedding) "
        "VALUES (?,?,?,?,?,?,5,0,'reflection',?,?,?,?,?,?)",
        (owner, kind, f"Fixture {WORD} note.", t, t, t, json.dumps(list(evidence)),
         json.dumps(list(actors)), scope, scope_with, key,
         pack_vec(fake_embed(f"Fixture {WORD} note.")))).lastrowid
    store.conn.executemany("INSERT OR IGNORE INTO memory_actor(memory_id, guid) VALUES (?, ?)",
                           [(mid, g) for g in actors])
    store.conn.execute("INSERT INTO memory_vec(memory_id, owner, embedding) VALUES (?, ?, ?)",
                       (mid, owner, pack_vec(fake_embed(f"Fixture {WORD} note."))))
    store.conn.commit()
    return mid


def ref(mid):
    return f"memory:{mid}"


def to_state(store, state):
    """`unchecked`: as raw() left the rows; `checked`: as the migration leaves a live database
    (derived columns written, stored scopes untouched); `raised`: after memory rescope --go."""
    now = store.clock.now()
    if state == "checked":
        scopes.settle_pending(store.conn, now=now, raise_stored=False, source=None)
    elif state == "raised":
        scopes.apply(store.conn, now=now)
    store.conn.commit()


def stored(store, mid):
    return tuple(store.conn.execute("SELECT scope, scope_with FROM memory WHERE id = ?",
                                    (mid,)).fetchone())


def effective(store, mid):
    return effective_scopes(store.conn, [mid])[mid]


@pytest.fixture
def cast(store):
    seed_test_cast(store)
    store.set_meta("human_guid", str(HUMAN))
    return store


@pytest.fixture
def recall(cast, clock):
    emb = Embedder(LLMScheduler(FakeLLM(), clock=clock), "emb-model")
    return Recall(cast, emb, clock, tau_days=5.0)


def recalled(r, owner, channel, to_guid=0):
    got = run(r.recall(owner, WORD, present=[HUMAN, 102], addressee=None, k=20,
                       channel=channel, to_guid=to_guid, touch=False))
    return {m.id for m in got}


def lesson_visible(store, guid, mid, channel="guild", to_guid=0):
    return audience.visible(store.conn, [ref(mid)], guid=guid, channel=channel, to_guid=to_guid)


def callback_visible(store, guid, mid, channel="guild", to_guid=0):
    ctx = callbacks.CallbackContext(enabled=True, channel=channel, to_guid=to_guid,
                                    present=(HUMAN, guid))
    gate = callbacks._Privacy(store.conn, guid, ctx, store.clock.now() + 10, HUMAN)
    return gate.visible(ref(mid))


# -- the break cases ---------------------------------------------------------------------------

def secrecy_chain(store):
    """m0 open -> m1 open (mis-scoped) -> her secret m2 -> a whisper that asked for secrecy."""
    whisper = line(store, "whisper", HUMAN, 101, ASK)
    m2 = raw(store, 101, f"chat:{whisper}", scope="secret", scope_with=HUMAN)
    m1 = raw(store, 101, ref(m2))
    m0 = raw(store, 101, ref(m1), "event:1")
    return m0, m1, m2


@pytest.mark.parametrize("state", STATES)
def test_a_secrecy_whisper_two_levels_down_keeps_the_memory_from_public_turns(cast, recall, state):
    m0, m1, m2 = secrecy_chain(cast)
    to_state(cast, state)
    assert effective(cast, m0) == effective(cast, m1) == ("secret", HUMAN)
    for channel in ("guild", "party", "discord:general"):
        assert not recalled(recall, 101, channel) & {m0, m1, m2}
        assert not lesson_visible(cast, 101, m0, channel)
        assert not callback_visible(cast, 101, m0, channel)
    assert {m0, m1, m2} <= recalled(recall, 101, "whisper", HUMAN)
    assert not recalled(recall, 101, "whisper", 102) & {m0, m1, m2}
    assert (stored(cast, m0) == ("open", 0)) is (state != "raised")


@pytest.mark.parametrize("state", STATES)
def test_another_members_private_memory_two_down_reaches_nobody_else(cast, recall, state):
    """m0 open -> m1 open (mis-scoped) -> 102's private memory with him: it is 101's to tell
    only 102, never the guild, and no third member's lesson may rest on it."""
    theirs = raw(cast, 102, f"chat:{line(cast, 'whisper', HUMAN, 102)}", scope="private",
                 scope_with=HUMAN)
    m1 = raw(cast, 101, ref(theirs))
    m0 = raw(cast, 101, ref(m1))
    to_state(cast, state)
    assert effective(cast, m0) == ("private", 102)
    assert not recalled(recall, 101, "guild") & {m0, m1}
    assert not recalled(recall, 101, "whisper", HUMAN) & {m0, m1}
    for guid in (101, 103):
        assert not lesson_visible(cast, guid, m0)
        assert not callback_visible(cast, guid, m0)


@pytest.mark.parametrize("state", STATES)
def test_a_carried_memory_of_a_cited_line_is_gated_by_its_evidence(cast, state):
    """m0 open -> 102's guild line -> the memory that line was composed with (stored open,
    mis-scoped) -> 102's secret with him. The lesson walk opens the line's carried memory."""
    secret = raw(cast, 102, f"chat:{line(cast, 'whisper', HUMAN, 102, ASK)}", scope="secret",
                 scope_with=HUMAN)
    carried = raw(cast, 102, ref(secret))
    said = cast.conn.execute(
        "INSERT INTO chat_line(ts, channel, from_guid, from_name, to_guid, text, origin) "
        "VALUES (50, 'guild', 102, 'M102', 0, 'fixture line', 'mind')").lastrowid
    cast.conn.execute(
        "INSERT INTO utterance(ts, guid, channel, kind, text, memory_ids, to_guid) "
        "VALUES (50, 102, 'guild', 'reply', 'fixture line', ?, 0)", (json.dumps([carried]),))
    m0 = raw(cast, 101, f"chat:{said}")
    to_state(cast, state)
    assert effective(cast, carried) == ("secret", HUMAN)
    assert not lesson_visible(cast, 101, m0)
    assert not callback_visible(cast, 101, m0)


@pytest.mark.parametrize("state", STATES)
def test_a_secrecy_whisper_mixed_with_a_public_line(cast, recall, state):
    m1 = raw(cast, 101, f"chat:{line(cast, 'whisper', HUMAN, 101, ASK)}",
             f"chat:{line(cast, 'guild', HUMAN)}")
    m0 = raw(cast, 101, ref(m1))
    to_state(cast, state)
    assert effective(cast, m0) == ("secret", HUMAN)
    assert not recalled(recall, 101, "guild") & {m0, m1}
    assert not lesson_visible(cast, 101, m0)


@pytest.mark.parametrize("state", STATES)
def test_officers_evidence_at_depth(cast, recall, state):
    m2 = raw(cast, 102, f"chat:{line(cast, 'officer', 104)}")
    m1 = raw(cast, 102, ref(m2))
    m0 = raw(cast, 102, ref(m1))
    to_state(cast, state)
    assert effective(cast, m0) == ("private", OFFICERS)
    assert not recalled(recall, 102, "guild") & {m0, m1, m2}
    assert {m0, m1, m2} <= recalled(recall, 102, "officer")
    assert not lesson_visible(cast, 102, m0)


@pytest.mark.parametrize("state", STATES)
def test_group_dm_evidence_at_depth(cast, recall, state):
    m2 = raw(cast, 101, f"chat:{line(cast, 'gdm:1', 102, audience=[HUMAN, 101, 102])}")
    m1 = raw(cast, 101, ref(m2))
    m0 = raw(cast, 101, ref(m1))
    to_state(cast, state)
    assert effective(cast, m0) == ("private", GROUP)
    for channel in ("guild", "discord:general", "officer"):
        assert not recalled(recall, 101, channel) & {m0, m1, m2}
    assert not lesson_visible(cast, 101, m0)


@pytest.mark.parametrize("state", STATES)
def test_cycles_end_and_carry_the_strictest_scope_round(cast, recall, state):
    """a <-> b cite each other, b also cites her private memory; c <-> d are an open cycle."""
    private = raw(cast, 101, f"chat:{line(cast, 'whisper', HUMAN, 101)}", scope="private",
                  scope_with=HUMAN)
    a = raw(cast, 101, "event:1")
    b = raw(cast, 101, ref(a), ref(private))
    c = raw(cast, 101, "event:2")
    d = raw(cast, 101, ref(c))
    for mid, cites in ((a, [ref(b)]), (c, [ref(d)])):
        cast.conn.execute("UPDATE memory SET evidence = ? WHERE id = ?", (json.dumps(cites), mid))
    cast.conn.commit()
    to_state(cast, state)
    assert effective(cast, a) == effective(cast, b) == ("private", HUMAN)
    assert effective(cast, c) == effective(cast, d) == ("open", 0)
    assert not recalled(recall, 101, "guild") & {a, b}
    assert {c, d} <= recalled(recall, 101, "guild")


def test_two_partners_at_depth_reach_nobody(cast):
    his = raw(cast, 101, f"chat:{line(cast, 'whisper', HUMAN, 101)}", scope="private",
              scope_with=HUMAN)
    hers = raw(cast, 101, f"chat:{line(cast, 'whisper', 102, 101)}", scope="private",
               scope_with=102)
    top = raw(cast, 101, ref(raw(cast, 101, ref(his))), ref(raw(cast, 101, ref(hers))))
    assert effective(cast, top) == ("private", 0)


def test_a_scope_is_never_widened(cast):
    """A memory stored stricter than its evidence asks stays so, and a derived scope that was
    once strict stays strict when the evidence that asked for it is gone."""
    asked = raw(cast, 101, "event:1", scope="secret", scope_with=HUMAN)
    private = raw(cast, 101, f"chat:{line(cast, 'whisper', HUMAN, 101)}", scope="private",
                  scope_with=HUMAN)
    top = raw(cast, 101, ref(private))
    scopes.apply(cast.conn, now=cast.clock.now())
    assert stored(cast, asked) == ("secret", HUMAN) and stored(cast, top) == ("private", HUMAN)
    cast.conn.execute("UPDATE memory SET evidence = '[\"event:3\"]' WHERE id = ?", (top,))
    scopes.settle_pending(cast.conn, now=cast.clock.now())
    assert stored(cast, top) == effective(cast, top) == ("private", HUMAN)


def test_reading_past_the_bound_fails_closed(cast):
    chain = [raw(cast, 101, "event:1")]
    for _ in range(6):
        chain.append(raw(cast, 101, ref(chain[-1])))
    found = privacy.resolve(cast.conn, [chain[-1]], cap=3)
    assert found[chain[-1]].effective == STRICTEST and any(r.bounded for r in found.values())
    assert privacy.resolve(cast.conn, [chain[-1]])[chain[-1]].effective == ("open", 0)
    assert stored(cast, chain[-1]) == ("open", 0)  # a read writes nothing


# -- every reader -----------------------------------------------------------------------------

@pytest.mark.parametrize("state", STATES)
def test_every_reader_goes_by_the_effective_scope(cast, state):
    m0, m1, _ = secrecy_chain(cast)
    to_state(cast, state)
    conn = cast.conn
    # the store's rows: recall, the night's provenance walk (forget._node), outreach's sources
    assert [(m.scope, m.scope_with) for m in cast.get_memories([m0, m1])] == [("secret", HUMAN)] * 2
    assert all(m.scope == "secret" for m in cast.memories_with_actors(101, [HUMAN]))
    # what a new memory, a joke's or a slang term's sources, a claim citing it derive
    # (culture.seeds, nightly.lexicon, nightly.growth, the writer)
    derived = derive_scope(conn, 101, [ref(m0)])
    assert (derived.scope, derived.scope_with) == ("secret", HUMAN)
    assert derive_scope(conn, 103, [ref(m0)]).scope == "secret"
    # the night's ledger walk and the notes' evidence
    assert knowledge._memory_rows(conn, [m0])[m0][1] == "secret"
    assert m0 not in relations._open_ids(cast, relations._NEWEST_OPEN, 101, HUMAN, 10)
    # SQL readers that cannot derive take only a checked open memory as open
    open_now = {r[0] for r in conn.execute(f"SELECT id FROM memory WHERE {privacy.open_sql()}")}
    assert not open_now & {m0, m1}


@pytest.mark.parametrize("state", STATES)
def test_a_life_beat_made_private_at_depth_seeds_nothing_public(cast, state):
    """server.seeds reads the beat's own memory; one checked private by its evidence is no
    seed even while it is stored open."""
    from leeroy_mind.server import seeds
    private = raw(cast, 101, f"chat:{line(cast, 'whisper', HUMAN, 101)}", scope="private",
                  scope_with=HUMAN)
    beat = raw(cast, 101, "beat:1", ref(private))
    cast.conn.execute("UPDATE memory SET source = 'offscreen' WHERE id = ?", (beat,))
    cast.conn.commit()
    to_state(cast, state)
    if state == "unchecked":
        scopes.settle_pending(cast.conn, now=cast.clock.now(), raise_stored=False, source=None)
    row = cast.conn.execute(
        "SELECT (m.scope!='open' OR m.status!='active' OR COALESCE(m.derived_scope,'open')!='open') "
        "FROM memory m WHERE id = ?", (beat,)).fetchone()
    assert row[0] == 1
    assert "derived_scope" in seeds._life.__code__.co_consts.__repr__()


# -- propagation on each path that raises a scope ---------------------------------------------

def open_chain(store, owner=101, depth=3):
    """An honest open chain, checked: base <- ... <- top."""
    base = store.insert_memory(make_mem(owner=owner, text=f"Fixture {WORD} base.",
                                        evidence=("beat:7",), source="offscreen"))
    chain = [base]
    for _ in range(depth):
        chain.append(store.insert_memory(make_mem(owner=owner, text=f"Fixture {WORD} above.",
                                                  evidence=(ref(chain[-1]),))))
    assert all(stored(store, m) == ("open", 0) for m in chain)
    assert scopes.pending_count(store.conn) == 0
    return chain


def test_a_raw_raise_unchecks_everything_resting_on_it(cast, recall):
    chain = open_chain(cast)
    other = cast.insert_memory(make_mem(owner=101, text=f"Fixture {WORD} apart."))
    cast.conn.execute("UPDATE memory SET scope = 'private', scope_with = ? WHERE id = ?",
                      (HUMAN, chain[0]))
    cast.conn.commit()
    assert set(scopes.pending(cast.conn)) == set(chain[1:])
    # unchecked rows are derived by the reader at once, before any settle
    assert all(effective(cast, m) == ("private", HUMAN) for m in chain)
    assert recalled(recall, 101, "guild") == {other}
    done = scopes.settle_pending(cast.conn, now=cast.clock.now(), source="test")
    assert done.raised == 3 and done.receipt is not None
    assert all(stored(cast, m) == ("private", HUMAN) for m in chain)
    assert scopes.pending_count(cast.conn) == 0


def test_life_contact_raises_what_rests_on_the_beat_in_its_transaction(cast):
    chain = open_chain(cast)
    plan = life_contact.Plan(rescope=[(chain[0], 101, "")])
    assert life_contact.apply(cast, plan, human=HUMAN, now=cast.clock.now()) == (0, 1)
    assert all(stored(cast, m) == ("private", HUMAN) for m in chain)
    assert scopes.pending_count(cast.conn) == 0
    assert [r[2] for r in scopes.receipts(cast.conn)] == ["life-contact"]


def test_a_keyed_rewrite_and_a_merge_raise_what_rests_on_them(cast):
    keyed = cast.upsert_keyed_memory(make_mem(owner=101, key="fixture:key", evidence=("event:1",)))
    above = cast.insert_memory(make_mem(owner=101, evidence=(ref(keyed),)))
    cast.upsert_keyed_memory(make_mem(owner=101, key="fixture:key", evidence=("event:1",),
                                      scope="private", scope_with=HUMAN))
    assert stored(cast, above) == ("private", HUMAN)

    merged = cast.insert_memory(make_mem(owner=101, evidence=("event:2",)))
    top = cast.insert_memory(make_mem(owner=101, evidence=(ref(merged),)))
    cast.merge_memory(merged, [f"chat:{line(cast, 'whisper', HUMAN, 101)}"], 5)
    assert stored(cast, merged) == stored(cast, top) == ("private", HUMAN)


def test_the_store_never_stores_a_scope_wider_than_the_evidence(cast):
    """insert_memory itself, whoever calls it: a caller that skipped the writer's derive."""
    secret = raw(cast, 101, f"chat:{line(cast, 'whisper', HUMAN, 101, ASK)}", scope="secret",
                 scope_with=HUMAN)
    mid = cast.insert_memory(make_mem(owner=101, evidence=(ref(raw(cast, 101, ref(secret))),)))
    assert stored(cast, mid) == ("secret", HUMAN)


def test_an_ask_for_secrecy_after_the_memory_was_written(cast):
    """The memory was private when written; a later line of the thread asks for secrecy."""
    first = line(cast, "whisper", HUMAN, 101)
    thread = cast.open_thread(f"whisper:{HUMAN}:101", 0)
    cast.set_thread([first], thread)
    mid = cast.insert_memory(make_mem(owner=101, evidence=(f"chat:{first}",)))
    top = cast.insert_memory(make_mem(owner=101, evidence=(ref(mid),)))
    assert stored(cast, mid) == stored(cast, top) == ("private", HUMAN)
    cast.set_thread([line(cast, "whisper", HUMAN, 101, ASK)], thread)
    assert stored(cast, mid) == stored(cast, top) == ("secret", HUMAN)


def test_an_edit_that_adds_the_ask(cast):
    first = line(cast, "dm:101", HUMAN, 101)
    cast.set_thread([first], cast.open_thread("dm:101", 0))
    mid = cast.insert_memory(make_mem(owner=101, evidence=(f"chat:{first}",)))
    assert stored(cast, mid) == ("private", HUMAN)
    cast.conn.execute("UPDATE chat_line SET text = ? WHERE id = ?", (ASK, first))
    cast.lines_changed([first])
    assert stored(cast, mid) == ("secret", HUMAN)


def test_a_new_row_wakes_a_ref_that_was_waiting_for_its_id(cast):
    """A memory citing an id that has no row yet (ids are reused after a delete)."""
    waiting = raw(cast, 101, "memory:9000")
    scopes.settle_pending(cast.conn, now=cast.clock.now())
    cast.conn.execute(
        "INSERT INTO memory(id, owner, kind, text, t_event, t_created, t_accessed, importance, "
        "valence, source, evidence, scope, scope_with) VALUES (9000, 101, 'reflection', "
        "'Fixture.', 0, 0, 0, 5, 0, 'reflection', '[]', 'secret', 1)")
    assert effective(cast, waiting) == ("secret", HUMAN)


# -- settle, the night, rescope ----------------------------------------------------------------

def test_settling_twice_changes_nothing(cast):
    m0, m1, m2 = secrecy_chain(cast)
    first = scopes.apply(cast.conn, now=cast.clock.now())
    assert first.raised == 2 and {c[0] for c in first.changes} == {m0, m1}
    before = cast.conn.execute("SELECT id, scope, scope_with, derived_scope, derived_with "
                               "FROM memory ORDER BY id").fetchall()
    again = scopes.apply(cast.conn, now=cast.clock.now() + 5)
    assert again.raised == 0 and again.receipt is None
    assert scopes.settle_pending(cast.conn, now=cast.clock.now()).checked == 0
    assert before == cast.conn.execute("SELECT id, scope, scope_with, derived_scope, derived_with "
                                       "FROM memory ORDER BY id").fetchall()


def test_the_night_checks_recent_and_unchecked_rows_within_its_bound(cast):
    m0, m1, _ = secrecy_chain(cast)
    result = scopes.night(cast.conn, now=cast.clock.now(), rows=2)
    assert result["checked"] >= 2 and result["left"] >= 0
    # a second night finishes what the first left; the state is the rows' own
    scopes.night(cast.conn, now=cast.clock.now())
    assert stored(cast, m0) == stored(cast, m1) == ("secret", HUMAN)
    assert scopes.pending_count(cast.conn) == 0
    assert scopes.night(cast.conn, now=cast.clock.now())["raised"] == 0
    assert any(r[2] == "night" for r in scopes.receipts(cast.conn))


def test_the_stage_is_one_committed_unit(cast):
    m0, _, _ = secrecy_chain(cast)
    units = []

    class Ctx:
        night, clock, store = "2026-10-29", cast.clock, cast

        def done(self, stage, unit):
            return (stage, unit) in units

        def commit(self, stage, unit, write):
            with cast.transaction():
                write(cast)
            units.append((stage, unit))
            return True
    run(scopes.stage_scopes(Ctx()))
    assert units == [(scopes.STAGE, "settle")] and stored(cast, m0) == ("secret", HUMAN)
    run(scopes.stage_scopes(Ctx()))
    assert len(units) == 1


def test_the_stage_is_attached_to_every_night_runner():
    names = []
    runner = SimpleNamespace(add_stage=lambda name, fn, *, after, model: names.append(
        (name, after, model)))
    scopes.attach(runner)
    assert names == [("scopes", "forget", None)]


def test_undo_restores_what_is_stored_and_readers_still_go_by_the_evidence(cast, recall):
    m0, m1, _ = secrecy_chain(cast)
    done = scopes.apply(cast.conn, now=cast.clock.now())
    out = scopes.undo(cast.conn, done.receipt, now=cast.clock.now())
    assert out == {"restored": 2, "changed_since": 0, "gone": 0}
    assert stored(cast, m0) == stored(cast, m1) == ("open", 0)
    assert effective(cast, m0) == ("secret", HUMAN)
    assert not recalled(recall, 101, "guild") & {m0, m1}
    with pytest.raises(ValueError):
        scopes.undo(cast.conn, done.receipt, now=cast.clock.now())
    with pytest.raises(KeyError):
        scopes.undo(cast.conn, 999, now=cast.clock.now())


def test_undo_leaves_a_row_changed_since(cast):
    m0, m1, _ = secrecy_chain(cast)
    done = scopes.apply(cast.conn, now=cast.clock.now())
    cast.conn.execute("UPDATE memory SET scope_with = 0 WHERE id = ?", (m0,))
    out = scopes.undo(cast.conn, done.receipt, now=cast.clock.now())
    assert out["restored"] == 1 and out["changed_since"] == 1
    assert stored(cast, m0) == ("secret", 0)


def test_the_plan_counts_by_evidence_and_scope_and_holds_no_text(cast):
    secrecy_chain(cast)
    raw(cast, 101, f"chat:{line(cast, 'whisper', HUMAN, 101)}")           # a private line, direct
    raw(cast, 102, ref(raw(cast, 102, f"chat:{line(cast, 'officer', 104)}", scope="private",
                           scope_with=OFFICERS)))
    plan = scopes.plan(cast.conn)
    assert len(plan.changes) == 4
    assert plan.by_move == {"open -> secret with him": 2, "open -> private with him": 1,
                            "open -> private with officers": 1}
    assert plan.by_evidence["a cited memory stored private or secret"] == 2
    assert plan.by_evidence["a cited memory that itself rests on scoped evidence (deeper)"] == 1
    assert plan.by_evidence["a private line, cited directly"] == 1
    assert plan.by_evidence["a restricted line, through cited memories"] == 1
    assert WORD not in repr(plan) and "fixture" not in repr(plan).lower()
    assert scopes.pending_count(cast.conn) > 0  # a plan writes nothing


def test_turns_that_carried_a_memory_the_plan_refuses(cast):
    m0, _, _ = secrecy_chain(cast)
    for channel, to in (("guild", 0), ("whisper", HUMAN), ("discord:general", 0)):
        cast.conn.execute(
            "INSERT INTO utterance(ts, guid, channel, kind, text, memory_ids, to_guid) "
            "VALUES (100, 101, ?, 'reply', 'fixture', ?, ?)", (channel, json.dumps([m0]), to))
    cast.conn.execute("INSERT INTO utterance(ts, guid, channel, kind, text, memory_ids, to_guid) "
                      "VALUES (100, 103, 'guild', 'reply', 'fixture', ?, 0)", (json.dumps([m0]),))
    got = scopes.recalled(cast.conn, scopes.plan(cast.conn), since=0)
    assert got == {"turns": 4, "public_turns": 3, "public_refused": 3, "other_refused": 0,
                   "memories": 1, "other_members": 1}


def test_the_cli_dry_run_writes_nothing_and_go_writes_a_receipt(cast, tmp_path, capsys):
    m0, m1, _ = secrecy_chain(cast)
    cast.conn.commit()
    root = str(tmp_path)
    assert scope_cli.memory_main(["rescope", "--data-dir", root]) == 0
    out = capsys.readouterr().out
    assert "to make stricter 2" in out and "dry run, nothing written" in out
    assert WORD not in out and "fixture" not in out.lower()
    assert stored(cast, m0) == ("open", 0)
    cast.conn.commit()
    assert scope_cli.memory_main(["rescope", "--data-dir", root, "--go"]) == 0
    out = capsys.readouterr().out
    assert "2 memories made stricter, receipt 1" in out and "snapshot" in out
    assert stored(cast, m0) == stored(cast, m1) == ("secret", HUMAN)
    cast.conn.commit()
    assert scope_cli.memory_main(["rescope", "--data-dir", root, "--go"]) == 0
    assert "nothing written" in capsys.readouterr().out
    assert scope_cli.memory_main(["rescope", "--data-dir", root, "--undo", "1", "--go"]) == 0
    assert "2 stored scopes restored" in capsys.readouterr().out
    assert scope_cli.memory_main(["rescope", "--data-dir", root, "--undo", "1", "--go"]) == 1
    assert stored(cast, m0) == ("open", 0) and effective(cast, m0) == ("secret", HUMAN)


# -- the migration ----------------------------------------------------------------------------

def _before(path, tmp_path, monkeypatch):
    from test_migrations import db_before
    return db_before("scope_evidence", path, tmp_path, monkeypatch)


def test_the_migration_checks_every_stored_memory_and_changes_no_stored_scope(tmp_path, monkeypatch,
                                                                              clock):
    path = tmp_path / "mind.db"
    old = Store(_before(path, tmp_path, monkeypatch), clock=clock)
    old.set_meta("human_guid", str(HUMAN))
    whisper = line(old, "whisper", HUMAN, 101, ASK)

    def add(owner, *evidence, scope="open", scope_with=0):
        t = int(clock.now())
        mid = old.conn.execute(
            "INSERT INTO memory(owner, kind, text, t_event, t_created, t_accessed, importance, "
            "valence, source, evidence, scope, scope_with) VALUES (?, 'reflection', 'Fixture.', "
            "?, ?, ?, 5, 0, 'reflection', ?, ?, ?)",
            (owner, t, t, t, json.dumps(list(evidence)), scope, scope_with)).lastrowid
        return mid
    m2 = add(101, f"chat:{whisper}", scope="secret", scope_with=HUMAN)
    m1 = add(101, ref(m2))
    m0 = add(101, ref(m1), "event:1", "memory:x", 7)
    plain = add(101, "event:2")
    old.conn.execute("UPDATE memory SET evidence = 'not json' WHERE id = ?",
                     (add(101, "event:3"),))
    old.conn.commit()
    # the code reads a database from before the migration as it always did
    assert effective_scopes(old.conn, [m0])[m0] == ("open", 0)
    assert not scopes.available(old.conn)
    old.close()

    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db_mod.latest_version()
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT id, scope, scope_with, derived_scope, derived_with, derived_at IS NOT NULL "
            "FROM memory")}
        assert rows[m0] == ("open", 0, "secret", HUMAN, 1)
        assert rows[m1] == ("open", 0, "secret", HUMAN, 1)
        assert rows[m2] == ("secret", HUMAN, "secret", HUMAN, 1)
        assert rows[plain] == ("open", 0, "open", 0, 1)
        assert conn.execute("SELECT count(*) FROM memory WHERE derived_at IS NULL").fetchone()[0] == 0
        assert {tuple(r) for r in conn.execute("SELECT src, kind, dst FROM memory_cite")} == {
            (m2, "chat", whisper), (m1, "memory", m2), (m0, "memory", m1)}
        assert effective_scopes(conn, [m0, plain]) == {m0: ("secret", HUMAN), plain: ("open", 0)}
        assert conn.execute("SELECT count(*) FROM memory_rescope").fetchone()[0] == 0
        done = scopes.apply(conn, now=clock.now())
        assert {c[0] for c in done.changes} == {m0, m1} and done.receipt == 1
        # the triggers keep the index: an edit, a delete
        conn.execute("UPDATE memory SET evidence = ? WHERE id = ?", (json.dumps(["event:9"]), m1))
        conn.execute("DELETE FROM memory WHERE id = ?", (plain,))
        assert {tuple(r) for r in conn.execute("SELECT src, kind, dst FROM memory_cite")} == {
            (m2, "chat", whisper), (m0, "memory", m1)}
        assert [r[0] for r in conn.execute("SELECT id FROM memory WHERE derived_at IS NULL "
                                           "ORDER BY id")] == sorted((m0, m1))
    finally:
        conn.close()
