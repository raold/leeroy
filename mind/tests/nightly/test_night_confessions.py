"""Earned member-to-member confidences stay private through prompts and atomic commits."""
import pytest

from conftest import run
from leeroy_mind.nightly import encounters as enc, feuds, gossip, knowledge
from leeroy_mind.nightly.ground import Claim
from leeroy_mind.nightly.evidence import Evidence, load_items
from leeroy_mind.nightly.growth import eligible
from nightly.conftest import no_real_name, yes
from nightly.test_night_encounters import encounter, probe, prompt, retrieved, secret_memory


LAYERS = (("surface", "I worry about the new job"),
          ("middle", "I miss the friends I grew up with"),
          ("deep", "I have never felt at home anywhere"))


def prepare(h, *, other=102, trust=70, ext=3, reciprocal=True, night_owl=False, layers=LAYERS,
            night="2026-10-29"):
    h.persona(101, disclosures=layers, night_owl=night_owl)
    h.rel(101, other, trust=trust)
    h.rel(other, 101, trust=10)
    with h.store.transaction():
        h.store.conn.execute("INSERT OR REPLACE INTO trait_state(night,guid,trait,value) "
                            "VALUES (?,101,'extraversion',?)", (night, ext))
    if reciprocal:
        h.store.add_rel_delta(other, 101, "affinity", 5, "a good conversation", (),
                             h.clock.now() - 86400, applied=True)


def answer(h, other=102, **changes):
    name = h.ctx().name(other)
    return {"scene": f"Arkon opened up to {name} quietly.",
        "memories": [{"who": "a", "text": f"I opened up to {name}.", "importance": 9, "valence": 0.5},
                     {"who": "b", "text": "Arkon trusted me enough to open up.", "importance": 9, "valence": 0.5}],
        "deltas": [], "bystanders": [], "gossip": None} | changes


def scene(h, data=None, *, other=102, night="2026-10-29", idx=0,
          setting="voice chat late at night", volition=None, judge=yes):
    ctx = h.ctx({"encounter": [answer(h, other) if data is None else data], "judge": [judge]}, night=night)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,volition) "
                            "VALUES (?,?,101,?,?,'planned',?)", (night, idx, other, setting, volition))
    if not ctx.done("encounters", "plan"):
        ctx.skip("encounters", "plan")
    return ctx


def opened(h, other, *layers):
    with h.store.transaction():
        for layer in layers:
            h.store.conn.execute("INSERT INTO layer_open(guid,other,layer,opened,ref) VALUES (101,?,?,?,?)",
                                 (other, layer, h.clock.now() - 86400, "rule:old-disclosure"))


def reverse_layer(h, other=102):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO layer_open(guid,other,layer,opened,ref) VALUES (?,101,0,?,?)",
                            (other, h.clock.now() - 86400, "rule:reciprocal"))


def test_confession_is_private_and_builds_trust(h):
    prepare(h)
    probe(h)
    ctx = scene(h)
    run(enc.stage_encounters(ctx))
    row = encounter(h)
    root = f"encounter:{row['id']}"
    assert row["status"] == "done" and row["private"] == 1
    assert ("It's late. Arkon opens up to Belra about this: I worry about the new job. "
            "Belra listens. It stays between the two of them.") in prompt(ctx)
    assert "Nobody else is around" in prompt(ctx)
    assert h.store.get_relationship(101, 102).trust == 72
    assert h.store.get_relationship(102, 101).trust == 12
    for owner, other in ((101, 102), (102, 101)):
        memory = h.rows(owner)[0]
        assert (memory.importance, memory.scope, memory.scope_with) == (7, "secret", other)
        assert memory.evidence == (root,)
        assert not gossip.source_allowed(ctx, memory, owner, 103)
    disclosure = h.store.conn.execute("SELECT * FROM disclosure").fetchone()
    assert (disclosure["guid"], disclosure["other"], disclosure["depth"], disclosure["layer"],
            disclosure["response"], disclosure["ref"]) == (101, 102, 1, 0, "responsive", root)
    fact = h.store.conn.execute("SELECT * FROM fact WHERE kind='confession'").fetchone()
    assert fact["text"] == LAYERS[0][1] and fact["secret"] == 1
    ledger = h.store.conn.execute("SELECT guid,how,may_tell FROM knows WHERE root=? ORDER BY guid", (root,)).fetchall()
    assert [tuple(r) for r in ledger] == [(101, "witnessed", 0), (102, "confided", 0)]
    assert knowledge.believes_knows(h.store, 101, 102, root) == "yes"
    assert knowledge.believes_knows(h.store, 102, 101, root) == "yes"
    assert knowledge.scope_leaks(h.store) == [] and no_real_name(h.store) == []
    assert not h.rows(0)


@pytest.mark.parametrize("passed", [False, True])
def test_listener_memory_waits_for_probe_and_preserves_its_exact_text(h, passed):
    prepare(h)
    if passed:
        probe(h)
    data = answer(h)
    text = "Arkon trusted me. " + "a" * (200 - len("Arkon trusted me. "))
    data["memories"][1]["text"] = text
    ctx = scene(h, data)
    run(enc.stage_encounters(ctx))
    assert len(h.rows(101)) == 1
    assert len(h.rows(102)) == int(passed)
    if passed:
        assert h.rows(102)[0].text == text
    else:
        assert not h.store.conn.execute("SELECT 1 FROM emotion WHERE guid=102").fetchone()
    assert h.count("disclosure") == 1 and h.count("layer_open") == 1
    assert h.store.get_relationship(102, 101).trust == 12


def test_either_participant_can_confess_and_only_listener_waits_for_probe(h):
    prepare(h, layers=(), ext=7)
    h.persona(102, disclosures=LAYERS)
    h.rel(102, 101, trust=70)
    h.store.add_rel_delta(101, 102, "affinity", 5, "reciprocity", (), h.clock.now(), applied=True)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO trait_state(night,guid,trait,value) VALUES ('2026-10-29',102,'extraversion',3)")
    data = answer(h, scene="Belra opened up to Arkon.")
    data["memories"][0]["text"] = "I listened to Belra."
    data["memories"][1]["text"] = "I opened up to Arkon."
    ctx = scene(h, data)
    run(enc.stage_encounters(ctx))
    assert not h.rows(101) and h.rows(102)[0].text == "I opened up to Arkon."
    assert h.rows(102)[0].scope_with == 101
    row = h.store.conn.execute("SELECT guid,other FROM disclosure").fetchone()
    assert tuple(row) == (102, 101)


@pytest.mark.parametrize("changed", ["trust", "extraversion", "reciprocity", "layers", "late"])
def test_all_confession_prerequisites_are_required(h, changed):
    prepare(h, trust=15 if changed == "trust" else 70,
            ext=7 if changed == "extraversion" else 3,
            reciprocal=changed != "reciprocity", layers=() if changed == "layers" else LAYERS)
    ctx = scene(h, setting="the Orgrimmar bank steps" if changed == "late" else "voice chat late at night")
    run(enc.stage_encounters(ctx))
    assert encounter(h)["private"] == 0 and h.count("disclosure") == 0
    assert "opens up to Belra about this:" not in prompt(ctx)
    assert len(h.rows(101)) == len(h.rows(102)) == 1


def test_confide_volition_and_reverse_layer_supply_late_reciprocity(h):
    prepare(h, reciprocal=False)
    reverse_layer(h)
    ctx = scene(h, setting="the Orgrimmar bank steps", volition="confide")
    run(enc.stage_encounters(ctx))
    assert encounter(h)["private"] == 1 and h.count("disclosure") == 1


@pytest.mark.parametrize("age,applied", [(8 * 86400, True), (-1, True), (86400, False)])
def test_old_future_or_unapplied_affinity_is_not_reciprocity(h, age, applied):
    prepare(h, reciprocal=False)
    h.store.add_rel_delta(102, 101, "affinity", 5, "not recent", (), h.clock.now() - age, applied=applied)
    ctx = scene(h)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["private"] == 0 and h.count("disclosure") == 0


def test_next_layer_deep_cap_and_next_iso_week(h):
    prepare(h)
    opened(h, 102, 0, 1)
    reverse_layer(h)
    first = scene(h)
    run(enc.stage_encounters(first))
    assert LAYERS[2][1] in prompt(first)
    assert h.store.conn.execute("SELECT depth FROM disclosure").fetchone()[0] == 3
    h.rel(101, 105, trust=70)
    opened(h, 105, 0, 1)
    reverse_layer(h, 105)
    second = scene(h, other=105, idx=1)
    run(enc.stage_encounters(second))
    assert encounter(h)["private"] == 0 and h.count("disclosure") == 1
    h.clock.advance(7 * 86400)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO trait_state(night,guid,trait,value) VALUES ('2026-11-05',101,'extraversion',3)")
    third = scene(h, other=105, night="2026-11-05")
    run(enc.stage_encounters(third))
    assert encounter(h)["private"] == 1 and h.count("disclosure") == 2


def test_night_owl_needs_ten_less_trust_for_deep_layer(h):
    prepare(h, trust=55, night_owl=True)
    opened(h, 102, 0, 1)
    ctx = scene(h)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["private"] == 1
    assert h.store.conn.execute("SELECT depth FROM disclosure").fetchone()[0] == 3


def test_delayed_sunday_still_spends_original_week_deep_allowance(h):
    prepare(h, night="2026-11-01", reciprocal=False)
    opened(h, 102, 0, 1)
    opened(h, 105, 0, 1)
    reverse_layer(h)
    reverse_layer(h, 105)
    h.rel(101, 105, trust=70)
    h.clock.advance(4 * 86400)  # Monday evening, rendering the interrupted Sunday night.
    first = scene(h, night="2026-11-01")
    run(enc.stage_encounters(first))
    assert encounter(h)["private"] == 1 and h.count("disclosure") == 1
    second = scene(h, other=105, night="2026-11-01", idx=1)
    run(enc.stage_encounters(second))
    assert encounter(h)["private"] == 0 and h.count("disclosure") == 1
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO trait_state(night,guid,trait,value) VALUES ('2026-11-02',101,'extraversion',3)")
    next_week = scene(h, other=105, night="2026-11-02")
    run(enc.stage_encounters(next_week))
    assert encounter(h)["private"] == 1 and h.count("disclosure") == 2


def test_confession_omits_private_gossip_evidence_and_ignores_payload(h, monkeypatch):
    prepare(h)
    line = h.say(h.T, h.human, "The bakery manager job is mine.", channel="whisper", to=101)
    mid = h.mem(101, "Testy got the bakery manager job.", actors=(h.human,), importance=9,
                evidence=(f"chat:{line}",), scope="private", scope_with=h.human)
    retrieved(monkeypatch, h, mid)
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: 0.99)
    ctx = scene(h, answer(h, gossip={"teller": "a", "about": "Testy", "source": 2,
        "text": "Testy got the bakery manager job."},
        bystanders=[{"who": "Coros", "noticed": "their quiet conversation"}]))
    run(enc.stage_encounters(ctx))
    assert "bakery manager" not in prompt(ctx)
    assert "return gossip null" in prompt(ctx)
    assert not h.rows(103) and h.count("rumor") == 0
    assert not any(m.source == "hearsay" for m in h.rows(102))
    assert all(f"memory:{mid}" not in m.evidence for m in h.rows(101) if m.id != mid)


@pytest.mark.parametrize("partner,shown", [(102, True), (103, False)])
@pytest.mark.parametrize("wrapper", [False, True])
def test_shared_secret_must_keep_the_same_two_partners(h, monkeypatch, partner, shown, wrapper):
    prepare(h)
    mid, _ = secret_memory(h, scope="secret")
    with h.store.transaction():
        h.store.conn.execute("UPDATE memory SET scope_with=? WHERE id=?", (partner, mid))
    if wrapper:
        mid = h.mem(101, "Belra keeps the silver locket hidden.", evidence=(f"memory:{mid}",))
    probe(h)
    retrieved(monkeypatch, h, mid)
    ctx = scene(h)
    run(enc.stage_encounters(ctx))
    assert ("silver locket" in prompt(ctx)) == shown
    assert encounter(h)["private"] == 1
    assert (h.rows(101)[-1].scope, h.rows(101)[-1].scope_with) == ("secret", 102)
    assert (h.rows(102)[-1].scope, h.rows(102)[-1].scope_with) == ("secret", 101)


def test_legacy_open_secret_root_with_third_party_is_not_confession_context(h, monkeypatch):
    prepare(h)
    with h.store.transaction():
        previous = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,private) "
            "VALUES ('2026-10-28',0,101,103,'quiet corner','done',1)").lastrowid
    root = f"encounter:{previous}"
    for who in (101, 102, 103):
        knowledge.learn(h.store, root, who, "confided", ts=h.T)
    mid = h.mem(101, "Coros keeps a silver locket hidden.", actors=(103,), evidence=(root,))
    retrieved(monkeypatch, h, mid)
    probe(h)
    ctx = scene(h)
    run(enc.stage_encounters(ctx))
    assert "silver locket" not in prompt(ctx)
    assert encounter(h)["private"] == 1
    assert h.rows(101)[-1].scope_with == 102 and h.rows(102)[-1].scope_with == 101


def test_confession_rejects_before_any_disclosure_effect(h):
    prepare(h)
    def reject(text):
        verdicts = yes(text)
        verdicts["verdicts"][0]["invariants_ok"] = False
        return verdicts
    ctx = scene(h, judge=reject)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "rejected" and encounter(h)["private"] == 0
    assert all(h.count(t) == 0 for t in ("memory", "disclosure", "layer_open", "knows", "fact"))
    assert h.store.get_relationship(101, 102).trust == 70


@pytest.mark.parametrize("change", ["trust", "layer", "deep_cap"])
def test_disclosure_eligibility_changed_during_model_call_aborts(h, change):
    prepare(h)
    if change == "deep_cap":
        opened(h, 102, 0, 1)
    initial_open = h.count("layer_open")
    def changed(_):
        if change == "trust":
            h.rel(101, 102, trust=0)
        elif change == "layer":
            h.persona(101, disclosures=(("surface", "An entirely different disclosure"), *LAYERS[1:]))
        else:
            with h.store.transaction():
                h.store.conn.execute("INSERT INTO disclosure(guid,other,depth,response,layer,ts,ref) "
                    "VALUES (101,105,3,'responsive',2,?,'rule:other-confession')", (h.clock.now(),))
        return answer(h)
    ctx = scene(h, changed)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "planned" and encounter(h)["private"] == 0
    assert h.count("memory") == h.count("knows") == h.count("fact") == 0
    assert h.count("layer_open") == initial_open and not ctx.done("encounters", "enc:0")


def test_probe_expiring_during_model_call_withholds_listener_only(h):
    prepare(h, reciprocal=False)
    reverse_layer(h)
    probe(h)
    def expired(_):
        h.clock.advance((knowledge.PROBE_MAX_AGE_DAYS + 1) * 86400)
        return answer(h)
    ctx = scene(h, expired)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "done" and encounter(h)["private"] == 1
    assert len(h.rows(101)) == 1 and not h.rows(102)
    assert h.count("disclosure") == 1


def test_late_hook_failure_rolls_back_confession_and_retry_is_once(h, monkeypatch):
    prepare(h)
    probe(h)
    ctx = scene(h)
    original = feuds.maybe_open
    def failed(*args, **kw):
        raise RuntimeError("synthetic failure after disclosure writes")
    monkeypatch.setattr(feuds, "maybe_open", failed)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "planned" and encounter(h)["private"] == 0
    assert all(h.count(t) == 0 for t in ("memory", "disclosure", "layer_open", "knows", "fact", "emotion"))
    assert h.store.get_relationship(101, 102).trust == 70
    monkeypatch.setattr(feuds, "maybe_open", original)
    retry = h.ctx({"encounter": [answer(h)], "judge": [yes]})
    run(enc.stage_encounters(retry))
    run(enc.stage_encounters(retry))
    assert h.count("disclosure") == h.count("layer_open") == 1
    assert h.store.get_relationship(101, 102).trust == 72


def test_confession_never_qualifies_for_a_public_self_note(h):
    prepare(h)
    probe(h)
    ctx = scene(h)
    run(enc.stage_encounters(ctx))
    private = h.rows(101)[0]
    public = h.mem(101, "Belra listened yesterday.", t=h.T - 86400, actors=(102,),
                   evidence=("rule:ordinary",))
    refs = [f"memory:{private.id}", f"memory:{public}"]
    ev = Evidence()
    for item in load_items(h.store, refs, ident=h.ident):
        ev.add(item)
    claim = Claim("profile", 101, "I find it easier to open up these days.", refs)
    assert not eligible(claim, ev, owner_has_attitude=False, human=h.human, store=h.store)


def test_disclosure_text_redacts_real_name_and_markup(h):
    prepare(h, layers=(("surface", "|cffff0000Quillon|r helped me feel welcome"), *LAYERS[1:]))
    ctx = scene(h)
    run(enc.stage_encounters(ctx))
    assert "Testy helped me feel welcome" in prompt(ctx)
    assert "Quillon" not in prompt(ctx)
    fact = h.store.conn.execute("SELECT text FROM fact WHERE kind='confession'").fetchone()
    assert fact[0] == "Testy helped me feel welcome"
