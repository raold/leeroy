"""Synthetic retellings: the model supplies words, never permissions or ledger state."""
import pytest

from leeroy_mind.nightly import gossip, knowledge


@pytest.fixture(autouse=True)
def average_traits(monkeypatch):
    monkeypatch.setattr(gossip, "trait_today", lambda *args: {
        "extraversion": 5.5, "neuroticism": 5.5, "conscientiousness": 5.5})


def scene(h, a=101, b=102, **values):
    idx = h.count("encounter")
    ident = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,private) "
        "VALUES (?,?,?,?,?,'done',?)", (h.night, idx, a, b, "the bank steps",
                                       values.pop("private", 0))).lastrowid
    return {**dict(h.store.conn.execute("SELECT * FROM encounter WHERE id=?", (ident,)).fetchone()),
            **values}


def source(h, owner=101, text="Coros took my bracers.", **values):
    ident = h.mem(owner, text, **{"importance": 8, "valence": -0.6,
                                  "actors": (103,), **values})
    return h.store.get_memories([ident])[0]


def tell(h, memory, *, ctx=None, enc=None, shown=None, about="Coros", text=None, who="a"):
    ctx = ctx or h.ctx()
    enc = enc or scene(h, a=memory.owner)
    data = {"teller": who, "about": about, "source": memory.id,
            "text": memory.text if text is None else text}
    with h.store.transaction():
        return gossip.after_gossip(h.store, ctx, enc, data, shown or [memory], now=h.T)


def state(h, guid, rumor=1):
    return h.store.conn.execute("SELECT * FROM rumor_state WHERE rumor=? AND guid=?",
                                (rumor, guid)).fetchone()


def test_pure_rules_and_stable_discretion():
    low = {"extraversion": 6, "neuroticism": 6, "conscientiousness": 5.5}
    high = {**low, "extraversion": 7}
    assert gossip.choose_op(high, -30) == "assimilate"
    assert gossip.choose_op(high, 29) == "sharpen"
    assert gossip.choose_op({**low, "neuroticism": 7}, 0) == "sharpen"
    assert gossip.choose_op(low, 29) == "level"
    assert gossip.tell_score(8, 1, low) == pytest.approx(0.44)
    assert gossip.discretion(None, {**low, "extraversion": 9, "conscientiousness": 2}) == pytest.approx(0.45)
    assert gossip.discretion(None, {**low, "extraversion": 2, "conscientiousness": 9}) == 0.95
    assert gossip.discretion(None, {**low, "extraversion": 50, "conscientiousness": 0}) == 0.2
    assert gossip.discretion_roll(101, "chat:5") == gossip.discretion_roll(101, "chat:5")
    assert gossip.discretion_roll(101, "chat:5") != gossip.discretion_roll(102, "chat:5")


def test_fresh_source_without_ledger_is_told_and_tracks_actual_shift(h):
    m = source(h)
    h.rel(102, 101, trust=60)
    mid = tell(h, m)
    heard = h.store.get_memories([mid])[0]
    assert heard.source == "hearsay" and heard.confidence == 0.5
    assert heard.actors == (101, 103) and heard.importance == 5
    assert heard.evidence == (f"memory:{m.id}", "encounter:1")
    assert h.store.get_relationship(102, 103).affinity == -3
    assert state(h, 102)["shift"] == -3
    learned = h.store.conn.execute("SELECT * FROM knows WHERE root='event:1' AND guid=102").fetchone()
    assert learned["how"] == "told" and learned["via"] == 101
    assert knowledge.believes_knows(h.store, 101, 102, "event:1") == "yes"
    assert knowledge.believes_knows(h.store, 102, 101, "event:1") == "yes"
    assert h.rows(0) == []


@pytest.mark.parametrize("trust,about,actors,text", [
    (20, "Coros", (103,), "Coros took my bracers."),
    (60, "Testy", (1,), "Testy took my bracers."),
])
def test_untrusted_and_human_gossip_is_remembered_without_shift(h, trust, about, actors, text):
    m = source(h, text=text, actors=actors)
    h.rel(102, 101, trust=trust)
    assert tell(h, m, about=about) is not None
    assert state(h, 102)["shift"] == 0
    assert h.count("rel_delta") == 0


def test_records_clamped_shift_instead_of_requested_shift(h):
    m = source(h)
    h.rel(102, 101, trust=60)
    h.rel(102, 103, affinity=-99)
    assert tell(h, m)
    assert state(h, 102)["shift"] == -1
    assert h.store.get_relationship(102, 103).affinity == -100


def test_other_owners_source_and_participant_about_are_rejected(h):
    m = source(h, owner=102)
    enc = scene(h)
    assert tell(h, m, enc=enc) is None
    mine = source(h, actors=(102,), text="Belra took my bracers.")
    assert tell(h, mine, about="Belra") is None
    assert h.count("rumor") == 0
    assert not [m for m in h.rows(102) if m.source == "hearsay"]


@pytest.mark.parametrize("gate", ["secret_root", "secret_scope", "may_tell", "nested_secret"])
def test_secret_and_explicit_no_telling_never_gossiped(h, gate):
    if gate == "secret_root":
        root = f"encounter:{scene(h, private=1)['id']}"
        m = source(h, evidence=(root,))
        knowledge.learn(h.store, root, 102, "confided", ts=h.T)
    elif gate == "secret_scope":
        m = source(h, scope="secret", scope_with=102)
    elif gate == "nested_secret":
        ancestor = source(h, scope="secret", scope_with=102)
        m = source(h, evidence=(f"memory:{ancestor.id}",))
    else:
        m = source(h)
        knowledge.learn(h.store, "event:1", 101, "told", via=103, ts=h.T, may_tell=0)
    before = h.count("knows")
    assert tell(h, m) is None
    assert h.count("knows") == before and h.count("rumor") == 0


@pytest.mark.parametrize("text,accepted,reason", [
    ("Coros wiped us like 9 times.", True, ""),
    ("Coros wiped us like 10 times.", False, "number outside"),
    ("Coros wiped us on The Lich King.", False, "boss/item"),
    ("Coros wiped us; Ebbo said so.", False, "name not in evidence"),
    ("Coros wiped us; |cffffffffEbbo|r said so.", False, "name not in evidence"),
])
def test_numbers_and_invented_details_are_checked_after_markup(h, text, accepted, reason):
    m = source(h, text="Coros wiped us 6 times.")
    assert (tell(h, m, text=text) is not None) is accepted
    if not accepted:
        assert any(reason in r[0] for r in h.store.conn.execute("SELECT reason FROM grounding_log"))


def test_operation_uses_view_before_telling_and_today_traits(h, monkeypatch):
    m = source(h)
    h.rel(102, 103, affinity=-29)
    h.rel(102, 101, trust=60)
    monkeypatch.setattr(gossip, "trait_today", lambda *args: {
        "extraversion": 8, "neuroticism": 5, "conscientiousness": 5})
    tell(h, m)
    assert h.store.conn.execute("SELECT op FROM rumor_version").fetchone()[0] == "sharpen"
    assert h.store.get_relationship(102, 103).affinity == -32


def test_retelling_preserves_root_halves_confidence_and_cites_previous_memory(h):
    original = source(h)
    first = h.store.get_memories([tell(h, original)])[0]
    second = tell(h, first, enc=scene(h, a=102, b=104))
    heard = h.store.get_memories([second])[0]
    assert heard.confidence == 0.25
    assert heard.evidence[0] == f"memory:{first.id}"
    assert gossip.source_root(h.store, heard) == "event:1"
    assert h.count("rumor") == 1
    assert knowledge.hops(h.store, "event:1", 104) == 2


def test_fourth_telling_rejected_before_any_side_effects(h):
    m = source(h, source="hearsay", confidence=0.5)
    for guid, how, via in [(103, "witnessed", None), (105, "told", 103),
                           (104, "told", 105), (101, "told", 104)]:
        knowledge.learn(h.store, "event:1", guid, how, via=via, ts=h.T - 1)
    assert knowledge.hops(h.store, "event:1", 101) == 3
    assert tell(h, m) is None
    assert h.rows(102) == [] and h.count("rumor") == 0


def test_three_tellings_and_already_known_hearer_stifle_teller(h):
    m = source(h)
    for hearer in (102, 104, 105):
        assert tell(h, m, enc=scene(h, b=hearer))
    assert state(h, 101)["state"] == "stifler" and state(h, 101)["tells"] == 3
    assert tell(h, m, enc=scene(h, b=102)) is None
    other = source(h, evidence=("event:2",))
    knowledge.learn(h.store, "event:2", 102, "witnessed", ts=h.T - 1)
    assert tell(h, other)
    assert state(h, 101, 2)["state"] == "stifler"


def test_hearer_who_used_all_tellings_stays_stifled(h):
    m = source(h)
    tell(h, m)
    h.store.conn.execute("UPDATE rumor_state SET state='stifler',tells=? WHERE guid=102",
                        (gossip.MAX_TELLS,))
    heard = h.store.get_memories([tell(h, m)])[0]
    assert state(h, 102)["state"] == "stifler"
    assert state(h, 102)["tells"] == gossip.MAX_TELLS
    assert not gossip.source_allowed(h.ctx(), heard, 102, 104)


def test_correction_supersedes_matching_hearsay_leaves_half_shift_and_is_idempotent(h):
    m = source(h)
    h.rel(102, 101, trust=60)
    previous = tell(h, m)
    unrelated = source(h, owner=102, source="hearsay", evidence=("event:99",))
    ctx, enc = h.ctx(), scene(h, a=103)
    enc["_shown_refs"] = (f"memory:{m.id}",)
    with h.store.transaction():
        corrected = gossip.after_correction(h.store, ctx, enc, 1, 102, now=h.T)
    row = h.store.get_memories([corrected])[0]
    assert row.text == "Turns out Coros took my bracers."
    assert row.key == "correct:1" and row.confidence == 0.9 and row.importance == 4
    old = h.store.get_memories([previous])[0]
    assert old.status == "superseded" and old.superseded_by == corrected
    assert h.store.get_memories([unrelated.id])[0].status == "active"
    assert h.store.get_relationship(102, 103).affinity == -1.5
    assert state(h, 102)["state"] == "stifler" and state(h, 102)["shift"] == -1.5
    assert gossip.after_correction(h.store, ctx, enc, 1, 102, now=h.T + 1) is None
    assert h.store.get_relationship(102, 103).affinity == -1.5
    assert h.count("rumor_version") == 2


def test_live_cap_and_fade_never_resurrect_original_root(h):
    originals = [source(h, evidence=(f"event:{i}",)) for i in range(1, 5)]
    for m in originals[:3]:
        assert tell(h, m)
    assert len(gossip.live_rumors(h.store)) == 3
    assert tell(h, originals[3]) is None
    h.store.conn.execute("UPDATE rumor SET last_told=? WHERE id=1", (h.T - 15 * 86400,))
    gossip.fade(h.store, now=h.T)
    assert len(gossip.live_rumors(h.store)) == 2
    assert tell(h, originals[0]) is None
    assert tell(h, originals[3])
    assert h.count("rumor") == 4 and len(gossip.live_rumors(h.store)) == 3


def test_corrected_stifler_does_not_relearn_wrong_version_or_reverse_residue_again(h):
    m = source(h)
    h.rel(102, 101, trust=60)
    tell(h, m)
    ctx, enc = h.ctx(), scene(h, a=103)
    corrected = gossip.after_correction(h.store, ctx, enc, 1, 102, now=h.T)
    before = dict(state(h, 102))
    count = len(h.rows(102))
    chat = h.say(h.T, 1, "Coros collected spare bracers.", channel="whisper", to=101)
    private = source(h, evidence=(f"chat:{chat}",), scope="private", scope_with=1)
    assert tell(h, m, shown=[m, private]) is None
    assert state(h, 101)["state"] == "spreader"  # private telling changes no rumor rows
    assert tell(h, m) is None
    assert len(h.rows(102)) == count
    assert dict(state(h, 102)) == before
    assert state(h, 101)["state"] == "stifler"
    assert h.store.get_relationship(102, 103).affinity == -1.5
    assert gossip.after_correction(h.store, ctx, scene(h, a=103), 1, 102, now=h.T + 1) is None
    assert h.store.get_memories([corrected])[0].status == "active"
    assert h.count("rumor_version") == 2


def test_last_to_know_counts_only_old_knowledge_before_telling(h):
    m = source(h)
    for guid in (101, 103, 104, 105, 106):
        knowledge.learn(h.store, "event:1", guid, "witnessed", ts=h.T - 2 * 86400)
    heard = h.store.get_memories([tell(h, m)])[0]
    assert heard.text == "Last to hear: Coros took my bracers." and heard.importance == 6
    fresh = source(h, evidence=("event:2",))
    for guid in (101, 103, 104, 105, 106):
        knowledge.learn(h.store, "event:2", guid, "witnessed", ts=h.T - 1)
    assert not h.store.get_memories([tell(h, fresh)])[0].text.startswith("Last to hear:")


def test_already_informed_hearer_is_not_mislabelled_last_to_know(h):
    m = source(h)
    for guid in (101, 102, 103, 104, 105):
        knowledge.learn(h.store, "event:1", guid, "witnessed", ts=h.T - 2 * 86400)
    heard = h.store.get_memories([tell(h, m)])[0]
    assert heard.text == m.text and heard.importance == 5


@pytest.mark.parametrize("speaker,scope", [(1, "private"), (101, "private"), (1, "open")])
def test_private_original_party_can_tell_once_but_hearer_cannot(h, monkeypatch, speaker, scope):
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: 1.0)
    chat = h.say(h.T, speaker, "Coros took my bracers.", channel="whisper",
                 to=101 if speaker == 1 else 1)
    m = source(h, evidence=(f"chat:{chat}",), scope=scope,
               scope_with=1 if scope == "private" else 0)
    heard = h.store.get_memories([tell(h, m)])[0]
    assert (heard.scope, heard.scope_with) == ("private", 101)
    assert not knowledge.may_tell(h.store, 102, f"chat:{chat}")
    assert tell(h, heard, enc=scene(h, a=102, b=104)) is None
    assert h.count("rumor") == h.count("rumor_version") == h.count("rumor_state") == 0
    # A deliberately legacy open seed is already a leak; none of the new copies is.
    assert knowledge.scope_leaks(h.store) == ([m.id] if scope == "open" else [])


def test_private_discretion_and_open_source_tainted_by_another_shown_memory(h, monkeypatch):
    chat = h.say(h.T, 1, "Coros took my bracers.", channel="whisper", to=101)
    private = source(h, evidence=(f"chat:{chat}",), scope="private", scope_with=1)
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: 0.5)
    assert not gossip.source_allowed(h.ctx(), private, 101, 102)
    monkeypatch.setattr(gossip, "trait_today", lambda *args: {
        "extraversion": 9, "neuroticism": 5, "conscientiousness": 2})
    assert gossip.source_allowed(h.ctx(), private, 101, 102)
    open_source = source(h, evidence=("event:3",))
    heard = h.store.get_memories([tell(h, open_source, shown=[open_source, private])])[0]
    assert (heard.scope, heard.scope_with) == ("private", 101)
    assert h.count("rumor") == 0
    # Even though its canonical event is open, its inherited private context is not
    # the hearer's own original whisper and cannot become a fresh gossip source.
    assert not gossip.source_allowed(h.ctx(), heard, 102, 104)


def test_private_tell_ignores_public_rumor_cap(h, monkeypatch):
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: 1.0)
    for i in range(3):
        assert tell(h, source(h, evidence=(f"event:{i + 1}",)))
    chat = h.say(h.T, 1, "Coros took my bracers.", channel="whisper", to=101)
    private = source(h, evidence=(f"chat:{chat}",), scope="private", scope_with=1)
    assert tell(h, private)
    assert h.count("rumor") == 3


def test_scope_only_private_branch_cannot_borrow_another_whispers_permission(h, monkeypatch):
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: 1.0)
    chat = h.say(h.T, 1, "Coros took my bracers.", channel="whisper", to=101)
    hidden = source(h, scope="private", scope_with=104, evidence=("event:99",))
    wrapper = source(h, evidence=(f"memory:{hidden.id}", f"chat:{chat}"))
    assert not gossip.source_allowed(h.ctx(), wrapper, 101, 102)
    assert tell(h, wrapper) is None
    assert h.count("rumor") == 0 and h.rows(102) == []


def test_rumor_offer_supplies_held_copy_in_addition_to_original_sources(h):
    m = source(h)
    heard = h.store.get_memories([tell(h, m)])[0]
    enc = scene(h, a=102, b=104, volition="gossip", ref="rule:rumor:1")
    offer = gossip.rumor_offer(h.ctx(), enc)
    assert offer is not None
    assert {row.id for row in offer["sources"]} == {m.id, heard.id}
    assert offer["version"]["text"] == m.text
    assert offer == gossip.rumor_offer(h.ctx(), enc)
    h.store.conn.execute("UPDATE rumor_state SET state='stifler' WHERE guid=102")
    assert gossip.rumor_offer(h.ctx(), enc) is None


def test_correcting_middle_teller_preserves_later_hearers_historical_provenance(h):
    original = source(h)
    middle = h.store.get_memories([tell(h, original)])[0]
    last_id = tell(h, middle, enc=scene(h, a=102, b=104))
    ctx = h.ctx()
    assert gossip.after_correction(h.store, ctx, scene(h, a=103), 1, 102, now=h.T)
    assert h.store.get_memories([middle.id])[0].status == "superseded"
    offer = gossip.rumor_offer(ctx, scene(h, a=104, b=105, volition="gossip", ref="rule:rumor:1"))
    assert offer is not None and middle.id in {m.id for m in offer["sources"]}
    assert not gossip.source_allowed(ctx, h.store.get_memories([middle.id])[0], 102, 105)
    assert gossip.after_correction(h.store, ctx, scene(h, a=103, b=104), 1, 104, now=h.T)
    assert h.store.get_memories([last_id])[0].status == "superseded"


@pytest.mark.parametrize("status", ["superseded", "archived", "rejected"])
def test_historical_canonical_admits_only_active_or_superseded_rows(h, status):
    original = source(h)
    heard = h.store.get_memories([tell(h, original)])[0]
    h.store.conn.execute("UPDATE memory SET status=? WHERE id=?", (status, original.id))
    enc = scene(h, a=102, b=104, volition="gossip", ref="rule:rumor:1")
    assert (gossip.rumor_offer(h.ctx(), enc) is not None) is (status == "superseded")
    assert (tell(h, heard, enc=scene(h, a=102, b=104)) is not None) is (status == "superseded")


def test_correction_keeps_private_scene_context_and_supersedes_open_misinformation(h):
    m = source(h)
    previous = tell(h, m)
    unrelated = source(h, owner=102, text="Dessa collected herbs.", actors=(104,), source="hearsay")
    chat = h.say(h.T, 1, "Coros collected spare bracers.", channel="whisper", to=103)
    private = source(h, owner=103, text="Coros collected spare bracers.",
                     evidence=(f"chat:{chat}",), scope="private", scope_with=1)
    enc = scene(h, a=103, _shown_refs=(f"memory:{m.id}", f"memory:{private.id}"))
    mid = gossip.after_correction(h.store, h.ctx(), enc, 1, 102, now=h.T)
    row = h.store.get_memories([mid])[0]
    assert (row.scope, row.scope_with) == ("private", 103)
    old = h.store.get_memories([previous])[0]
    assert old.scope == "open" and old.status == "superseded" and old.superseded_by == mid
    assert h.store.get_memories([unrelated.id])[0].status == "active"
    assert h.rows(0) == []


def test_correction_cannot_publish_newly_secret_canonical(h):
    m = source(h)
    tell(h, m)
    h.store.conn.execute("UPDATE memory SET scope=?,scope_with=? WHERE id=?", ("secret", 102, m.id))
    enc = scene(h, a=103)
    assert gossip.after_correction(h.store, h.ctx(), enc, 1, 102, now=h.T) is None
    assert h.count("rumor_version") == 1


def test_markup_and_legacy_human_name_are_not_persisted_in_rumor(h):
    m = source(h, text="|cffffffffQuillon|r saw Coros take my bracers.", actors=(1, 103))
    assert tell(h, m, text="Coros took the bracers.")
    canonical = h.store.conn.execute("SELECT canonical FROM rumor").fetchone()[0]
    assert "Quillon" not in canonical and "|c" not in canonical


def test_missing_or_cyclic_provenance_cannot_produce_a_rumor(h):
    missing = source(h, evidence=("memory:999999",))
    assert tell(h, missing) is None
    cyclic = source(h, evidence=())
    h.store.conn.execute("UPDATE memory SET evidence=? WHERE id=?",
                        (f'["memory:{cyclic.id}"]', cyclic.id))
    cyclic = h.store.get_memories([cyclic.id])[0]
    assert tell(h, cyclic) is None
