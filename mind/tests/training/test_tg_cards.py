import asyncio
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from leeroy_mind.guild.boss_calls import BOSS_CALLS, call_for
from leeroy_mind.human import HumanIdentity
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.training import protocol as p, store as ts
from leeroy_mind.training.cards import (
    CLASSES, LEVERS, Card, Directive, Targets, describe, nearest, stock_card,
    validate_card, validate_directive,
)
from leeroy_mind.training.vocab import (
    GENERIC_TRIGGERS, ClassVocab, Vocab, VocabCollector, refresh, suspend_missing,
)


HUMAN = HumanIdentity(1, "Samplechar", "Samplecaller", "Sample Person", True)
STRATEGIES = frozenset({"threat", "dps aoe", "save mana", "wait for attack", "focus heal targets"})


def vocabulary(**changes):
    cv = ClassVocab(STRATEGIES, {"frostbolt", "fireball"}, (), False, GENERIC_TRIGGERS)
    return Vocab(**(dict(classes={c: replace(cv, class_=c) for c in CLASSES}, probes={},
        dc={name: p.DcKey(name, 0, 1, 0, 4000, -1) for name, lever in LEVERS.items()
            if lever.kind == "dc"}, whitelist=STRATEGIES, at=1, complete=True,
        asked_triggers=frozenset(GENERIC_TRIGGERS)) | changes))


def card(*directives, **changes):
    return replace(stock_card("631:0:1"), directives=directives, **changes)


def event(id="g3", cls=8, probe=801, **changes):
    return p.Vocab(**(dict(ts=1000, id=id, class_=cls, probe=probe,
        strategies=tuple(STRATEGIES), actions=("frostbolt", "fireball"), spells=(),
        spells_checked=False, triggers=GENERIC_TRIGGERS, dc=(), whitelist=()) | changes))


def terminal(id="g3", **changes):
    return event(id, 0, 0, whitelist=tuple(STRATEGIES), **changes)


def rotation(conn, name="tg_frost", **changes):
    return ts.put_rotation(conn, dict(name=name, class_=8, spec_tab=2,
        lines=["often>frostbolt!6"], origin="lab", status="champion", created=1) | changes)


def test_stock_card_is_p1ds_plan():
    stock = stock_card("631:0:1")
    assert stock.call == BOSS_CALLS[(631, 0)].pre
    assert stock.directives == ()
    assert stock_card("999:0:0").call == call_for(999, "boss")[1].pre
    assert "comp" not in stock.to_row()


@pytest.mark.parametrize("directive,reason", [
    (Directive("threat", "-"), "bad_value"),
    (Directive("save mana", "+"), "bad_value"),
    (Directive("save mana", "-", Targets(roles=("ranged",))), "bad_targets"),
    (Directive("mana save level", 3), "bad_value"),
    (Directive("wait for attack time", 11), "bad_value"),
    (Directive("PartyMaxSpread", 160), "bad_value"),
    (Directive("PartyMaxSpread", True), "bad_value"),
    (Directive("mana save level", "1"), "bad_value"),
    (Directive("lust_at", 19), "bad_value"),
    (Directive("lust_at", 21), "bad_value"),
    (Directive("formation", "circle;anything"), "bad_value"),
    (Directive("behind", "+"), "unknown_lever"),
    (Directive("avoid aoe", "-"), "removes_survival"),
    (Directive("tank", "-"), "removes_survival"),
    (Directive("heal", "-"), "removes_survival"),
    (Directive("WaitAtBoss", 100), "unknown_lever"),
    (Directive("threat", "+", Targets(classes=(10,))), "bad_targets"),
    (Directive("threat", "+", Targets(classes=(True,))), "bad_targets"),
    (Directive("threat", "+", Targets(specs=((8, 3),))), "bad_targets"),
    (Directive("threat", "+", Targets(roles=("ranged", "ranged"))), "bad_targets"),
    (Directive("threat", "+", Targets(classes=(8,), specs=((5, 2),))), "bad_targets"),
    (Directive("lust_at", 25, Targets(classes=(8,))), "bad_targets"),
    (Directive("rotation", "tg_frost", Targets()), "bad_targets"),
    (Directive("rotation", "tg_frost;other", Targets(specs=((8, 2),))), "bad_value"),
])
def test_validator_drops_forbidden_and_out_of_range(directive, reason):
    assert validate_directive(directive, vocabulary()) == reason


def test_exact_levers_and_bounds():
    assert len(LEVERS) == 26
    assert {n for n, lever in LEVERS.items() if lever.kind == "strategy"} == {
        "threat", "dps aoe", "save mana"}
    assert not LEVERS["PartyMaxSpread"].pooled
    assert LEVERS["HealRepositionMaxRange"].pooled
    for name, lever in LEVERS.items():
        if lever.kind == "rotation":
            continue
        for value in lever.values:
            assert validate_directive(Directive(name, value), vocabulary()) is None
    assert validate_directive(Directive("mana save level", True), vocabulary()) == "bad_value"


@pytest.mark.parametrize("call", ["Samplecast on bombs", "Samplecast, bombs", "samplecaller: stack",
                                  "Samplechar move", "Sample Person, spread"])
def test_calls_screen_character_called_and_real_name(call):
    assert "call_names_someone" in validate_card(card(call=call), vocabulary(),
                                                names={"Samplecast"}, human=HUMAN)


def test_call_words_and_candidate_limit():
    assert not validate_card(card(call="Samplecastle spreads"), vocabulary(),
                             names={"Samplecast"}, human=HUMAN)
    assert validate_card(card(call="x" * 61), vocabulary(), names=set(), human=HUMAN) == ["call_too_long"]
    two = card(Directive("threat", "+"), Directive("dps aoe", "+"))
    assert validate_card(two, vocabulary(), names=set(), human=HUMAN) == ["too_many_directives"]
    assert not validate_card(replace(two, status="champion"), vocabulary(), names=set(), human=HUMAN)
    assert "threat +" in describe(two)


def test_card_immutable_defaults_and_sql_roundtrip(store):
    assignment = {"ranged": "spread"}
    source = card(Directive("threat", "+", Targets(("ranged",), (8,), ((8, 2),))),
        assignment=assignment, evidence=("pull:synthetic",), vetoes=("synthetic",),
        lineage=12, prev_champion=10, created=2, changed=3, embedding=pack_vec([1] * 512))
    assignment["ranged"] = "changed"
    assert source.assignment["ranged"] == "spread"
    with pytest.raises(TypeError):
        source.assignment["ranged"] = "changed"
    with pytest.raises(FrozenInstanceError):
        source.directives[0].targets.roles = ()
    assert not card().assignment
    assert Card.from_row(source.to_row()) == source
    ident = ts.put_card(store.conn, source)
    row = store.conn.execute("SELECT * FROM tg_card WHERE id=?", (ident,)).fetchone()
    assert Card.from_row(row) == replace(source, id=ident)


def test_missing_vocab_suspends_cards(store):
    ident = ts.put_card(store.conn, card(Directive("threat", "+"), status="champion"))
    vocab = vocabulary(whitelist=STRATEGIES - {"threat"})
    assert suspend_missing(store.conn, vocab, ts=5) == [ident]
    row = store.conn.execute("SELECT status,changed FROM tg_card WHERE id=?", (ident,)).fetchone()
    assert tuple(row) == ("suspended", 5)
    assert suspend_missing(store.conn, vocab, ts=6) == []


def test_incomplete_vocabulary_never_suspends(store):
    ident = ts.put_card(store.conn, card(Directive("threat", "+"), status="champion"))
    rotation(store.conn)
    assert suspend_missing(store.conn, Vocab({}, {}, {}, frozenset(), 1), ts=5) == []
    assert store.conn.execute("SELECT status FROM tg_card WHERE id=?", (ident,)).fetchone()[0] == "champion"
    assert validate_directive(Directive("threat", "+"), vocabulary(complete=False)) == "not_in_vocab"


@pytest.mark.parametrize("queried_class,expected", [(8, False), (1, True)])
def test_suspension_requires_observed_target_class_absence(store, queried_class, expected):
    directive = Directive("threat", "+", Targets(classes=(1,)))
    ident = ts.put_card(store.conn, card(directive, status="champion", changed=2))
    collector = VocabCollector("subset", classes=(queried_class,))
    collector.feed(event("subset", queried_class, queried_class * 100 + 1,
                         strategies=tuple(STRATEGIES - {"threat"})))
    collector.feed(terminal("subset"))
    vocab = collector.result(at=3)
    assert vocab.complete
    # Both absent and unknown dependencies refuse new admission. Only the
    # actually queried target class establishes a reason to suspend old cards.
    assert validate_directive(directive, vocab) == "not_in_vocab"
    assert suspend_missing(store.conn, vocab, ts=5) == ([ident] if expected else [])
    row = store.conn.execute("SELECT status,changed FROM tg_card WHERE id=?", (ident,)).fetchone()
    assert tuple(row) == (("suspended", 5) if expected else ("champion", 2))


@pytest.mark.parametrize("status", ["champion", "retired", "suspended", "absent"])
def test_rotation_suspension_uses_db_when_snapshot_registry_is_unsupplied(store, status):
    if status != "absent":
        rotation(store.conn, status=status)
    directive = Directive("rotation", "tg_frost", Targets(specs=((8, 2),)))
    ident = ts.put_card(store.conn, card(directive, status="champion", changed=2))
    collector = VocabCollector("subset", classes=(8,))
    collector.feed(event("subset"))
    collector.feed(terminal("subset"))
    vocab = collector.result(at=3)
    assert vocab.complete and not vocab.rotations
    assert validate_directive(directive, vocab) == "not_in_vocab"
    expected = status != "champion"
    assert suspend_missing(store.conn, vocab, ts=5) == ([ident] if expected else [])
    row = store.conn.execute("SELECT status,changed FROM tg_card WHERE id=?", (ident,)).fetchone()
    assert tuple(row) == (("suspended", 5) if expected else ("champion", 2))


def test_snapshot_registry_cannot_override_retired_db_rotation(store):
    rotation(store.conn, status="retired")
    directive = Directive("rotation", "tg_frost", Targets(specs=((8, 2),)))
    ident = ts.put_card(store.conn, card(directive, status="champion"))
    vocab = vocabulary(rotations={"tg_frost": (8, 2)})
    assert validate_directive(directive, vocab) is None
    assert suspend_missing(store.conn, vocab, ts=5) == [ident]


@pytest.mark.parametrize("checked,probe_class,proof,expected", [
    (True, 8, {801: (8, 2)}, True),
    (False, 8, {801: (8, 2)}, False),
    (True, 8, {}, False),
    (True, 8, {801: (8, 1)}, False),
    (True, 5, {801: (8, 2)}, False),
    (True, 8, {802: (8, 2)}, False),
])
def test_rotation_line_names_checked_against_proven_matching_spells(store, checked, probe_class, proof, expected):
    ident = rotation(store.conn)
    probe = ClassVocab(STRATEGIES, {"frostbolt", "fireball"}, {"fireball"}, checked,
                       GENERIC_TRIGGERS, probe_class)
    vocab = vocabulary(probes={801: probe}, probe_specs=proof)
    assert suspend_missing(store.conn, vocab, ts=5) == ([ident] if expected else [])
    assert store.conn.execute("SELECT status FROM tg_rotation").fetchone()[0] == (
        "suspended" if expected else "champion")


def test_conflicting_probe_spell_evidence_does_not_suspend(store):
    rotation(store.conn)
    absent = ClassVocab(STRATEGIES, {"frostbolt"}, (), True, GENERIC_TRIGGERS, 8)
    present = replace(absent, spells=frozenset({"frostbolt"}))
    vocab = vocabulary(probes={801: absent, 802: present}, probe_specs={801: (8, 2), 802: (8, 2)})
    assert suspend_missing(store.conn, vocab, ts=5) == []


def test_missing_rotation_action_suspends_dependent_card(store):
    ident = rotation(store.conn, lines=["often>absent spell!6"])
    dependent = ts.put_card(store.conn, card(Directive("rotation", "tg_frost", Targets(specs=((8, 2),)))))
    vocab = vocabulary(rotations={"tg_frost": (8, 2)})
    assert suspend_missing(store.conn, vocab, ts=5) == [ident, dependent]
    assert store.conn.execute("SELECT status FROM tg_card").fetchone()[0] == "suspended"


def test_unqueried_trigger_absence_is_unknown(store):
    ident = rotation(store.conn, lines=["new aura>frostbolt!6"])
    assert suspend_missing(store.conn, vocabulary(), ts=5) == []
    assert suspend_missing(store.conn, vocabulary(asked_triggers={"new aura"}), ts=5) == [ident]


def test_rotation_name_requires_matching_spec_registry():
    vocab = vocabulary(rotations={"tg_frost": (8, 2)})
    assert validate_directive(Directive("rotation", "tg_frost", Targets(specs=((8, 2),))), vocab) is None
    assert validate_directive(Directive("rotation", "tg_frost", Targets(specs=((8, 1),))), vocab) == "not_in_vocab"
    assert validate_directive(Directive("rotation", "tg_other", Targets(specs=((8, 2),))), vocab) == "not_in_vocab"


def test_nearest_cards_by_embedding(store):
    vec = lambda a, b: pack_vec([a, b] + [0] * 510)
    ids = [ts.put_card(store.conn, replace(card(), context=f"631:{i}:1", embedding=vec(a, b)))
           for i, (a, b) in enumerate(((1, 0), (0.8, 0.2), (0, 1)))]
    assert [c.id for c in nearest(store.conn, vec(1, 0), 2)] == ids[:2]
    assert [c.id for c in nearest(store.conn, vec(1, 0), 2, exclude="631:0:1")] == ids[1:]
    store.conn.execute("UPDATE tg_card SET status='suspended' WHERE id=?", (ids[1],))
    assert [c.id for c in nearest(store.conn, vec(1, 0), 2)] == [ids[0], ids[2]]
    assert nearest(store.conn, vec(1, 0), 0) == []
    for invalid in (b"bad", vec(float("nan"), 0), vec(0, 0)):
        with pytest.raises(ValueError):
            nearest(store.conn, invalid)


def test_vocab_collector_assembles_one_request():
    first, second = VocabCollector("g3", classes=(8,)), VocabCollector("g4", classes=(5,))
    for ev in (event("g3", 8, 801), event("g4", 5, 501), terminal("g4"), terminal("g3")):
        first.feed(ev)
        second.feed(ev)
    a, b = first.result(at=1), second.result(at=2)
    assert a.complete and b.complete
    assert set(a.classes) == {8} and set(b.classes) == {5}
    assert not a.probes and not b.probes  # class jobs also carry a bot GUID
    assert a.classes[8].class_ == 8
    with pytest.raises(TypeError):
        a.classes[1] = a.classes[8]


@pytest.mark.parametrize("events", [
    (), (event(),), (terminal(),), (event(id="other"), terminal()),
    (event(strategies=(), actions=()), terminal()),
    (event(), event(), terminal()), (event(), terminal(), terminal()),
])
def test_missing_empty_duplicate_and_partial_collections_are_incomplete(events):
    collector = VocabCollector("g3", classes=(8,))
    for ev in events:
        collector.feed(ev)
    assert not collector.result(at=1).complete


def test_collector_distinguishes_explicit_probe_and_missing_probe_from_terminal():
    collector = VocabCollector("g3", classes=(8,), probes=(801,))
    collector.feed(event())
    collector.feed(event(spells_checked=True, spells=("frostbolt",)))
    collector.feed(terminal())
    vocab = collector.result(at=1)
    assert vocab.complete and vocab.probes[801].spells_checked
    assert not vocab.probe_specs
    missing = VocabCollector("g3", classes=(8,), probes=(802,))
    for ev in (event(), event(cls=0, probe=802, strategies=(), actions=()), terminal()):
        missing.feed(ev)
    assert not missing.result(at=1).complete


def test_vocab_dc_rows_carry_raid_and_heroic():
    collector = VocabCollector("g3", classes=(8,))
    collector.feed(event())
    collector.feed(terminal(dc=(p.DcKey("PartyMaxSpread", 0, 1, 0, 4000, -1),)))
    vocab = collector.result(at=1)
    assert vocab.dc["PartyMaxSpread"].raid == 4000
    assert vocab.dc["PartyMaxSpread"].heroic == -1
    assert validate_directive(Directive("PartyMaxSpread", 150), vocab) is None


def test_strategy_backed_value_checks_actual_strategy_whitelist():
    vocab = vocabulary(whitelist=STRATEGIES - {"wait for attack", "focus heal targets"})
    assert validate_directive(Directive("wait for attack time", 6), vocab) == "not_in_vocab"
    assert validate_directive(Directive("focus tanks", "on"), vocab) == "not_in_vocab"
    assert validate_directive(Directive("formation", "circle"), vocab) is None


def test_refresh_all_classes_trigger_bounds_and_correlated_terminal(store, clock):
    calls = []

    async def request(cmd, kind, *, until, timeout_s):
        calls.append(cmd)
        assert kind == "vocab" and timeout_s == 60
        assert cmd.classes == CLASSES and cmd.probes == (801,)
        assert cmd.triggers[:len(GENERIC_TRIGGERS)] == GENERIC_TRIGGERS
        assert "caster's aura" in cmd.triggers
        assert len(cmd.triggers) == 200
        assert all(len(name.encode("utf-8")) <= 40 for name in cmd.triggers)
        assert not until(terminal("foreign"))
        assert not until(event(cmd.id, cls=0, probe=801))
        assert until(terminal(cmd.id))
        return [*(event(cmd.id, c, c * 100 + 1) for c in CLASSES),
                event(cmd.id, spells_checked=True, spells=("frostbolt",)), terminal(cmd.id)]

    def class_spells(cls):
        return [SimpleNamespace(name=name, effects=(SimpleNamespace(kind=kind),)) for name, kind in (
            ("Caster's Aura", 6), ("Direct Damage", 2), ("x" * 41, 6),
            *((f"Aura {cls} {i}", 6) for i in range(220)))]

    ports = SimpleNamespace(conn=store.conn, clock=clock, request=request)
    vocab = asyncio.run(refresh(ports, probes=(801,), book=SimpleNamespace(class_spells=class_spells)))
    assert len(calls) == 1 and vocab.complete
    assert not vocab.probe_specs  # The wire supplies no spec evidence.
    assert "direct damage" not in vocab.asked_triggers


@pytest.mark.parametrize("raises", [False, True])
def test_refresh_timeout_is_unknown_and_never_suspends(store, clock, raises):
    ts.put_card(store.conn, card(Directive("threat", "+"), status="champion"))

    async def request(cmd, kind, *, until, timeout_s):
        if raises:
            raise TimeoutError
        return [event(cmd.id, 1, 101)]

    vocab = asyncio.run(refresh(SimpleNamespace(conn=store.conn, clock=clock, request=request)))
    assert not vocab.complete
    assert suspend_missing(store.conn, vocab, ts=5) == []
