"""Correlated vocabulary snapshots; incomplete responses never retire tactics."""

from collections.abc import Sequence
from dataclasses import dataclass, field
import json
import re
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

from .cards import CLASSES, Card, directive_vocab_missing
from .protocol import DcKey, GetVocab, Vocab as VocabEvent

if TYPE_CHECKING:
    from .spells import SpellBook


GENERIC_TRIGGERS = ("often", "very often", "timer", "high mana", "enough mana", "medium mana",
                    "high energy available", "combo points 5 available", "light rage available",
                    "medium rage available", "high rage available", "behind target")


@dataclass(frozen=True, slots=True)
class ClassVocab:
    strategies: frozenset[str] = frozenset()
    actions: frozenset[str] = frozenset()
    spells: frozenset[str] = frozenset()
    spells_checked: bool = False
    triggers: frozenset[str] = frozenset()
    class_: int = 0

    def __post_init__(self):
        for name in ("strategies", "actions", "spells", "triggers"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class Vocab:
    classes: dict[int, ClassVocab]
    probes: dict[int, ClassVocab]
    dc: dict[str, DcKey]
    whitelist: frozenset[str]
    at: float
    complete: bool = False
    # These must be authoritative class/spec receipts for this checked probe,
    # captured by the native probe with its checked spells. A roster is not proof.
    probe_specs: dict[int, tuple[int, int]] = field(default_factory=dict)
    rotations: dict[str, tuple[int, int]] = field(default_factory=dict)
    asked_triggers: frozenset[str] = frozenset()

    def __post_init__(self):
        for name in ("classes", "probes", "dc", "probe_specs", "rotations"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        for name in ("whitelist", "asked_triggers"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))


class VocabCollector:
    """One command's ordered class jobs, probe jobs, then class-0 server row.

    The server puts the selected bot's GUID in a *class* response too. Match
    requested slots, never infer job type from a nonzero GUID. Other request IDs
    are ignored. Unexpected, duplicate, absent or empty jobs fail completeness.
    """

    def __init__(self, id: str, *, classes=CLASSES, probes=(), triggers=GENERIC_TRIGGERS):
        if type(id) is not str or not id:
            raise ValueError("invalid vocabulary request id")
        self.id = id
        self.classes = tuple(classes)
        self.probes = tuple(probes)
        if (any(type(c) is not int or c not in CLASSES for c in self.classes)
                or len(self.classes) != len(set(self.classes))
                or len(self.probes) > 10 or any(type(g) is not int or not 1 <= g <= 0xFFFFFFFF
                                               for g in self.probes)
                or len(self.probes) != len(set(self.probes))):
            raise ValueError("invalid vocabulary request targets")
        self.triggers = frozenset(triggers)
        self._classes = {}
        self._probes = {}
        self._probe_specs = {}
        self._dc = {}
        self._whitelist = frozenset()
        self._slot = 0
        self._ended = False
        self._bad = False

    @property
    def complete(self):
        return self._ended and not self._bad and self._slot == len(self.classes) + len(self.probes)

    def feed(self, ev):
        if not isinstance(ev, VocabEvent) or ev.id != self.id:
            return
        if self._ended:
            self._bad = True
            return
        if ev.class_ == 0 and ev.probe == 0:
            self._ended = True
            self._dc = {key.key: key for key in ev.dc}
            self._whitelist = frozenset(ev.whitelist)
            if len(self._dc) != len(ev.dc):
                self._bad = True
            return
        cv = ClassVocab(ev.strategies, ev.actions, ev.spells, ev.spells_checked, ev.triggers, ev.class_)
        if self._slot < len(self.classes):
            expected = self.classes[self._slot]
            if ev.class_ != expected:
                self._bad = True
                return
            self._classes[expected] = cv
        else:
            index = self._slot - len(self.classes)
            if index >= len(self.probes) or ev.probe != self.probes[index]:
                self._bad = True
                return
            self._probes[ev.probe] = cv
            if (type(ev.class_) is int and ev.class_ in CLASSES
                    and ev.spells_checked is True
                    and type(ev.spec_tab) is int and 0 <= ev.spec_tab <= 2):
                self._probe_specs[ev.probe] = (ev.class_, ev.spec_tab)
        self._slot += 1
        if ev.class_ not in CLASSES or not ev.strategies or not ev.actions:
            self._bad = True

    def result(self, *, at: float, rotations=None) -> Vocab:
        return Vocab(self._classes, self._probes, self._dc, self._whitelist, at,
                     complete=self.complete, probe_specs=self._probe_specs,
                     rotations=rotations or {}, asked_triggers=self.triggers)


async def refresh(ports, *, probes: Sequence[int] = (), book: "SpellBook | None" = None) -> Vocab:
    triggers = list(GENERIC_TRIGGERS)
    if book is not None:
        for cls in CLASSES:
            for spell in book.class_spells(cls):
                name = spell.name.lower()
                if (any(effect.kind == 6 for effect in spell.effects)
                        and 1 <= len(name.encode("utf-8")) <= 40 and name not in triggers
                        and len(triggers) < 200):
                    triggers.append(name)
    collector = VocabCollector(uuid4().hex, probes=probes, triggers=triggers)
    cmd = GetVocab(collector.id, CLASSES, collector.probes, tuple(triggers))
    try:
        events = await ports.request(cmd, "vocab", until=lambda ev: (
            isinstance(ev, VocabEvent) and ev.id == cmd.id and ev.class_ == 0 and ev.probe == 0),
            timeout_s=60)
    except TimeoutError:
        events = []
    for ev in events:
        collector.feed(ev)
    snapshot = collector.result(at=ports.clock.now())
    rotations = {row["name"]: (row["class"], row["spec_tab"]) for row in ports.conn.execute(
        "SELECT * FROM tg_rotation WHERE status NOT IN ('retired','suspended')")
        if not snapshot.complete or not _rotation_missing(row, snapshot)}
    # Older producers leave spec unknown. Never substitute the roster's spec.
    return collector.result(at=snapshot.at, rotations=rotations)


_LINE = re.compile(r"([a-z0-9 ]{1,40})>([a-z0-9 ]{1,40})!([5-9](?:\.[0-9])?)")


def _rotation_missing(row, vocab):
    cv = vocab.classes.get(row["class"])
    if cv is None or not cv.actions or not cv.strategies:
        return False
    try:
        lines = json.loads(row["lines"])
    except (ValueError, TypeError):
        return True
    if not isinstance(lines, list) or not 1 <= len(lines) <= 12:
        return True
    checked = [probe for guid, probe in vocab.probes.items()
               if probe.spells_checked and probe.class_ == row["class"]
               and probe.strategies and probe.actions
               and vocab.probe_specs.get(guid) == (row["class"], row["spec_tab"])]
    for line in lines:
        match = _LINE.fullmatch(line) if isinstance(line, str) and len(line) <= 120 else None
        if match is None:
            return True
        trigger, action, _ = match.groups()
        if action not in cv.actions:
            return True
        if trigger in vocab.asked_triggers and trigger not in cv.triggers:
            return True
        # Multiple equally authoritative probes must agree on absence. A spell
        # known to one matching probe disproves the claim that this spec lost it.
        if checked and all(action in probe.actions and action not in probe.spells for probe in checked):
            return True
    return False


def suspend_missing(conn, vocab: Vocab, *, ts) -> list[int]:
    """Suspend on proven vocabulary drift, atomically. IDs retain table identity
    only at the caller: cards and rotations have independent integer sequences.
    A partial refresh is unknown, never evidence of mass deletion.
    """
    if not vocab.complete:
        return []
    suspended = []
    with conn:
        # This DB query is authoritative even when the optional snapshot
        # registry was never populated. Exclude newly proven invalid rows too.
        rotations = {}
        for row in conn.execute("SELECT * FROM tg_rotation WHERE status NOT IN ('retired','suspended')").fetchall():
            if _rotation_missing(row, vocab):
                conn.execute("UPDATE tg_rotation SET status='suspended' WHERE id=?", (row["id"],))
                suspended.append(row["id"])
            else:
                rotations[row["name"]] = (row["class"], row["spec_tab"])
        for row in conn.execute("SELECT * FROM tg_card WHERE status NOT IN ('retired','suspended')").fetchall():
            card = Card.from_row(row)
            if any(directive_vocab_missing(d, vocab, rotations=rotations) for d in card.directives):
                conn.execute("UPDATE tg_card SET status='suspended',changed=? WHERE id=?", (ts, card.id))
                suspended.append(card.id)
    return suspended
