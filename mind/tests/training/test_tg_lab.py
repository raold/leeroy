"""Pure proposals with synthetic spell rows and explicit coverage receipts."""

from dataclasses import replace
import re
import struct

import pytest

from leeroy_mind.training.lab import (
    halving_rounds, paired_score, propose, value_per_cast_second,
)
from leeroy_mind.training.spells import Effect, SpellInfo, load_spellbook
from leeroy_mind.training.vocab import ClassVocab


def info(id=1, name="Test Bolt", *, base=1000, kind=2, cast=1500, gcd=1500):
    return SpellInfo(id, name, "", cast, 0, gcd, 0, 0, 0, (0, 0, 0), 1, 80,
                     (Effect(kind, 0, base, 0, 0),), False)


class Book:
    def __init__(self, spells, *, unknown_cast=(), unknown_coeff=(), coeff=None):
        self.spells = spells
        self.unknown_cast = unknown_cast
        self.unknown_coeff = unknown_coeff
        self.coeff = coeff or {}

    def class_spells(self, cls):
        return list(self.spells)

    def cast_time_known(self, id):
        return id not in self.unknown_cast

    def coefficient_known(self, id):
        return id not in self.unknown_coeff and self.coefficient(id)[0] >= 0

    def coefficient(self, id):
        return self.coeff.get(id, (50, 0))


def vocab(spells, *, cls=8, triggers=("often",)):
    names = frozenset(s.name.lower() for s in spells)
    return ClassVocab(frozenset({"damage"}), names, names, True, frozenset(triggers), cls)


def test_value_per_cast_second_uses_integer_hundredths_once_per_cast():
    spell = info()
    assert value_per_cast_second(spell, (50, 0)) == pytest.approx(2000 / 1.5)
    spell = replace(spell, effects=(Effect(2, 0, 800, 100, 0), Effect(2, 0, 100, 100, 0)))
    assert value_per_cast_second(spell, (50, -100)) == pytest.approx(2000 / 1.5)


@pytest.mark.parametrize("cast,gcd,expected", [(3000, 1500, 2000 / 3),
                                                (0, 1500, 2000 / 1.5), (0, 0, 2000)])
def test_value_denominator(cast, gcd, expected):
    assert value_per_cast_second(info(cast=cast, gcd=gcd), (50, 0)) == pytest.approx(expected)


@pytest.mark.parametrize("kind", [6, 17, 31, 58])
def test_unmodelled_effects_do_not_become_absolute_damage(kind):
    assert value_per_cast_second(info(kind=kind), (50, 0)) == 0
    if kind in {17, 31, 58}:
        mixed = replace(info(), effects=info().effects + (Effect(kind, 0, 100, 0, 0),))
        assert value_per_cast_second(mixed, (50, 0)) == 0


@pytest.mark.parametrize("coeff,power", [((-100, 0), 2000), ((0.5, 0), 2000),
                                         ((50, 0), -1), ((50, 0), True)])
def test_invalid_heuristic_inputs_are_not_negative_bonus_or_damage(coeff, power):
    with pytest.raises(ValueError):
        value_per_cast_second(info(), coeff, power=power)


def test_negative_cast_time_is_unknown_not_instant():
    with pytest.raises(ValueError):
        value_per_cast_second(info(cast=-1), (50, 0))


@pytest.mark.parametrize("cls,tab", [(1, 2), (2, 0), (2, 1), (5, 0), (5, 1),
                                     (6, 0), (7, 2), (11, 2), (11, 1), (8, -1), (8, 3), (0, 0)])
def test_propose_only_unambiguous_damage_specs(cls, tab):
    spells = [info()]
    assert propose(cls, tab, Book(spells), vocab(spells, cls=cls)) == []


@pytest.mark.parametrize("changes", [{"spells_checked": False}, {"spells_checked": "yes"}, {"class_": 0},
                                      {"class_": 9}, {"actions": frozenset()},
                                      {"spells": frozenset()}, {"triggers": frozenset()}])
def test_propose_requires_current_checked_class_vocabulary(changes):
    spells = [info()]
    assert propose(8, 1, Book(spells), replace(vocab(spells), **changes)) == []


def test_proposals_pass_grammar_and_are_capped_deduplicated_and_deterministic():
    spells = [info(i, f"bolt {i}", base=100 * i) for i in range(1, 9)]
    cv = vocab(spells, triggers=("often", "hot streak", "sudden death"))
    first = propose(8, 1, Book(spells), cv, k=100)
    assert len(first) == 12
    assert first == propose(8, 1, Book(list(reversed(spells))), cv, k=100)
    assert len({c.lines for c in first}) == len(first)
    assert len({c.name for c in first}) == len(first)
    for candidate in first:
        assert re.fullmatch(r"tg_8_1_[0-9a-f]{16}", candidate.name)
        assert 1 <= len(candidate.lines) <= 12
        for line in candidate.lines:
            match = re.fullmatch(r"([a-z0-9 ]{1,40})>([a-z0-9 ]{1,40})!([6789])", line)
            assert match
            trigger, action, _ = match.groups()
            assert trigger in cv.triggers and trigger != "sudden death"
            assert action in cv.actions & cv.spells
            assert action not in {"bolt 1", "bolt 2"}
    assert first[0].lines == ("often>bolt 8!8", "often>bolt 7!7")
    assert first[1].lines == ("often>bolt 7!8", "often>bolt 8!7")
    assert propose(8, 1, Book(spells), cv, k=2) == first[:2]
    assert propose(8, 1, Book(spells), cv, k=0) == []


def test_proc_triggers_require_verified_class_spec_and_current_name():
    spells = [info()]
    triggers = ("often", "hot streak", "fingers of frost", "missile barrage", "sudden death", "brain freeze")
    for cls, tab, expected in [(1, 0, "sudden death"), (8, 0, "missile barrage"),
                                (8, 1, "hot streak"), (8, 2, "fingers of frost")]:
        proposals = propose(cls, tab, Book(spells), vocab(spells, cls=cls, triggers=triggers))
        procs = {line.split(">")[0] for c in proposals for line in c.lines if line.endswith("!9")}
        assert procs == {expected}
    assert not any("!9" in line for c in propose(9, 0, Book(spells), vocab(spells, cls=9, triggers=triggers))
                   for line in c.lines)


def test_known_spell_without_action_and_unsafe_names_are_excluded():
    spells = [info(), info(2, "Bad!Bolt"), info(3, "x" * 41)]
    cv = replace(vocab(spells), actions=frozenset({"bad!bolt", "x" * 41}))
    assert propose(8, 1, Book(spells), cv) == []


def test_missing_rows_and_negative_sentinels_do_not_rank_as_instant_or_zero_bonus():
    spells = [info(1, "missing cast"), info(2, "missing bonus"), info(3, "negative bonus"),
              info(4, "negative cast", cast=-1), info(5, "known")]
    book = Book(spells, unknown_cast={1}, unknown_coeff={2}, coeff={3: (-100, 0), 5: (0, -100)})
    proposals = propose(8, 1, book, vocab(spells))
    assert proposals
    assert {line.split(">")[1].split("!")[0] for c in proposals for line in c.lines} == {"known"}


def test_champion_is_preserved_bounded_and_lineage_is_content_addressed():
    spells = [info(1, "bolt"), info(2, "other", base=100)]
    cv = vocab(spells, triggers=("often", "high mana"))
    champion = ("high mana>other!6", "often>bolt!6")
    candidates = propose(8, 1, Book(spells), cv, champion=champion)
    assert candidates
    assert all(c.lines[0] == champion[0] for c in candidates)
    assert all(c.lines != champion and len(c.lines) == len(set(c.lines)) for c in candidates)
    assert len({c.parent for c in candidates}) == 1
    parent = candidates[0].parent
    original = next(c for c in propose(8, 1, Book(spells), cv) if c.lines == ("often>bolt!6",))
    assert parent != original.name
    changed = next(c for c in candidates if c.lines == ("high mana>other!6", "often>bolt!8"))
    assert changed.name != parent
    assert all(c.parent is None for c in propose(8, 1, Book(spells), cv))
    descendants = propose(8, 1, Book(spells), cv, champion=original.lines)
    assert descendants and all(c.parent == original.name for c in descendants)
    assert all(c.name != original.name for c in descendants)


@pytest.mark.parametrize("champion", [("often>test bolt!10",), ("missing>test bolt!6",),
                                       ("often>missing!6",), ("often>test bolt!5",),
                                       ("often>test bolt!6",) * 13])
def test_invalid_champion_cannot_leak_into_candidates(champion):
    spells = [info()]
    assert propose(8, 1, Book(spells), vocab(spells), champion=champion) == []


def test_full_champion_only_allows_edits_that_fit():
    spells = [info(i, f"bolt {i}", base=i * 100) for i in range(1, 14)]
    champion = tuple(f"often>bolt {i}!6" for i in range(1, 13))
    candidates = propose(8, 1, Book(spells), vocab(spells), champion=champion)
    assert candidates and all(len(c.lines) <= 12 for c in candidates)
    assert all(not any(">bolt 13!" in line for line in c.lines) for c in candidates)


@pytest.mark.parametrize("n,expected", [(0, []), (1, []), (2, [(2, 60)]),
                                        (3, [(3, 60), (2, 120)]),
                                        (12, [(12, 60), (6, 120), (3, 180)]),
                                        (13, [(13, 60), (7, 120), (4, 180)])])
def test_halving_rounds(n, expected):
    assert halving_rounds(n) == expected


@pytest.mark.parametrize("n", [-1, 1.5, True])
def test_invalid_counts(n):
    with pytest.raises(ValueError):
        halving_rounds(n)
    with pytest.raises(ValueError):
        propose(8, 1, Book([info()]), vocab([info()]), k=n)


def test_paired_score_cancels_gear_for_caller_supplied_same_basis_pairs():
    assert paired_score([1050, 10500], [1000, 10000]) == pytest.approx(0.05)
    assert paired_score([100, 800], [200, 400]) == pytest.approx(0.25)


@pytest.mark.parametrize("candidate,stock", [([], []), ([1], []), ([1, 2], [1]),
    ([0], [1]), ([-1], [1]), ([1], [0]), ([1], [-1]), ([True], [1]),
    ([1.0], [1]), ([1], [float("nan")]), ([1], [float("inf")])])
def test_paired_score_rejects_unscoreable_pairs(candidate, stock):
    with pytest.raises(ValueError):
        paired_score(candidate, stock)


def test_spellbook_row_proof_is_distinct_from_loaded_tables(tmp_path):
    def write(name, width, rows, strings=b"\0"):
        payload = b"".join(struct.pack(f"<{width}I", *row) for row in rows)
        (tmp_path / name).write_bytes(struct.pack("<4s4I", b"WDBC", len(rows), width, width * 4,
                                                  len(strings)) + payload + strings)

    rows = []
    for id, cast in [(1, 7), (2, 8), (3, 0)]:
        row = [0] * 234
        row[0], row[28], row[136] = id, cast, 1
        rows.append(row)
    write("Spell.dbc", 234, rows, b"\0Synthetic Bolt\0")
    write("SpellCastTimes.dbc", 4, [(7, 0, 0, 0)])
    write("SkillLineAbility.dbc", 14, [])
    write("SpellRuneCost.dbc", 5, [])
    book = load_spellbook(tmp_path, world={1: (0, -100), 2: (-100, 0)})
    assert "SpellCastTimes.dbc" in book.loaded
    assert "spell_bonus_data" in book.loaded
    assert book.info(1).cast_ms == book.info(2).cast_ms == book.info(3).cast_ms == 0
    assert book.cast_time_known(1)
    assert not book.cast_time_known(2)
    assert not book.cast_time_known(3)
    assert not book.cast_time_known(999)
    assert book.coefficient_known(1)
    assert not book.coefficient_known(2)
    assert not book.coefficient_known(3)
    assert not book.coefficient_known(999)
