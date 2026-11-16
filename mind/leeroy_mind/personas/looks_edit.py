"""The owner's weight and figure edits (owner, 2026-11-15: "I want to be able to adjust weight and
bust individually. Adjusting them triggers a new selfie render.", then "the same adjustment for
glutes, waist and hips"), from the console's Looks panel through the running mind's `looks`
control op (endpoint.looks_args, ControlEndpoint.looks).

- set_looks: one transaction. The stored persona is read again, its appearance's weight_kg,
  bust, glutes, waist and hips replaced (a shape not given is kept as stored), and it is saved
  as a new persona version with source "owner" (a real persona
  write, never a godmode lens; persona_looks_protection then keeps the variety pass off her
  looks), with a persona ledger row of kind "looks" whose private receipt (meta RECEIPT) holds
  every value before and after and the persona's source before. The same values again write
  nothing. A night writer that saves her persona later reads it again first, so it keeps them.
- restore, nightly.growth.undo's "looks" branch, so `personas undo <id>` and the console's Undo
  both take one back: the values its receipt holds as they were (a receipt from before the
  shapes holds weight and bust only), with the source before, while they are still
  what the edit left (a later edit wins: undo that first), and the persona_looks_protection row
  the edit added removed (or the one it replaced put back), unless a later owner change holds it.
- LooksService: the op's two actions, set and undo, for cast members only. After either, the
  mind's persona cache drops the member and the selfie service queues a fresh profile set
  (SelfieService.queue_for_looks: a varied one for the lust style while varied_sets is on).

Weight stays within the schema's bounds; bust, glutes, waist and hips are clothed descriptors
(schema.BUSTS, GLUTES, WAISTS, HIPS), for women only. Ledger text and logs carry numbers, the
enums' words, ids and outcomes only."""
import json
import logging
from dataclasses import dataclass, fields, replace

from leeroy_mind.logs import log
from leeroy_mind.wow import CAST_MIN_LEVEL

from .io import load_persona, save_persona
from .schema import BUSTS, GLUTES, HIPS, WAISTS, Appearance, validate_persona

KIND = "looks"
RECEIPT = "personas:looks-undo:{ledger}"
ACTIONS = ("set", "undo")
SOURCES = ("seed", "generated", "owner", "variety")  # persona_core's sources
_WEIGHT = next(f.metadata for f in fields(Appearance) if f.name == "weight_kg")
WEIGHT_MIN, WEIGHT_MAX = _WEIGHT["lo"], _WEIGHT["hi"]
SHAPES = {"bust": BUSTS, "glutes": GLUTES, "waist": WAISTS, "hips": HIPS}  # the owner's, women's
FIELDS = ("weight_kg", *SHAPES)
KEEP = object()  # a shape not given: kept as stored
_NAMED = {"bust": "Bust is", "glutes": "Glutes are", "waist": "Waist is", "hips": "Hips are"}


class LooksError(ValueError):
    """An edit the mind refuses; its text is the answer, in plain words."""


@dataclass(frozen=True)
class LooksChange:
    ledger: int
    version: int
    before: dict
    after: dict


def bust_text(bust: str | None) -> str:
    return "the build's own" if bust is None else bust.replace("_", " ")


def shape_text(name: str, value: str | None) -> str:
    if value is None:
        return "not said" if name == "glutes" else "the build's own"
    return value.replace("_", " ")


def describe(before: dict, after: dict) -> str:
    """The ledger row's text: what moved, in numbers and the enums' words."""
    parts = []
    if before["weight_kg"] != after["weight_kg"]:
        parts.append(f"weight {before['weight_kg']} -> {after['weight_kg']} kg")
    for name in SHAPES:
        if name in after and before.get(name) != after[name]:
            parts.append(f"{name} {shape_text(name, before.get(name))} -> "
                         f"{shape_text(name, after[name])}")
    return ", ".join(parts)


def _values(a: Appearance, keys=FIELDS) -> dict:
    return {k: getattr(a, k) for k in keys}


def check(weight_kg, bust, glutes=KEEP, waist=KEEP, hips=KEEP) -> None:
    """The values' own rules, in words: weight a whole number of kg in the schema's range, each
    shape one of its schema values or None (not set); KEEP leaves a shape as stored."""
    if type(weight_kg) is not int or not WEIGHT_MIN <= weight_kg <= WEIGHT_MAX:
        raise LooksError(f"Weight is a whole number of kg from {WEIGHT_MIN} to {WEIGHT_MAX}.")
    for name, value in (("bust", bust), ("glutes", glutes), ("waist", waist), ("hips", hips)):
        if value is KEEP or value is None:
            continue
        if not isinstance(value, str) or value not in SHAPES[name]:
            raise LooksError(f"{_NAMED[name]} one of "
                             + ", ".join(v.replace("_", " ") for v in SHAPES[name])
                             + ", or not set.")


def _race_class(store, guid: int) -> tuple[str, str]:
    from leeroy_mind.nightly.enrich import _race_class as race_class
    return race_class(store, guid)


def set_looks(store, guid: int, *, weight_kg: int, bust: str | None, now: float,
              glutes=KEEP, waist=KEEP, hips=KEEP) -> LooksChange | None:
    """Her weight and shapes as given (a shape left at KEEP stays as stored), saved as an owner
    persona version with its ledger row and receipt; None when they already are (nothing
    written). LooksError otherwise."""
    check(weight_kg, bust, glutes, waist, hips)
    given = {"glutes": glutes, "waist": waist, "hips": hips}
    with store.transaction():
        stored = load_persona(store, guid)
        if stored is None:
            raise LooksError("No persona is stored for this member.")
        if stored.appearance is None:
            raise LooksError("No looks yet: the night's looks pass gives them first.")
        before = _values(stored.appearance)
        after = {**before, "weight_kg": weight_kg, "bust": bust,
                 **{k: v for k, v in given.items() if v is not KEEP}}
        if before == after:
            return None
        changed = replace(stored, appearance=replace(stored.appearance, **after))
        if stored.identity.player_gender != "female":
            for name in SHAPES:
                if after[name] is not None:
                    raise LooksError(f"{_NAMED[name]} for women only.")
        errs = validate_persona(changed)
        if errs:
            raise LooksError("This persona does not save: " + "; ".join(errs)[:200])
        source = store.conn.execute("SELECT source FROM persona_core WHERE guid=?",
                                    (guid,)).fetchone()[0]
        protection = _protection(store, guid)
        race, class_name = _race_class(store, guid)
        version = save_persona(store, changed, source="owner", ts=now, race=race,
                               class_name=class_name)
        from leeroy_mind.nightly.store import ledger_add
        ledger = ledger_add(store, guid=guid, kind=KIND, text=describe(before, after),
                            evidence=[], now=now)
        store.set_meta(RECEIPT.format(ledger=ledger), json.dumps(
            {"before": before, "after": after, "source": source, "version": version,
             "protection": protection}, sort_keys=True))
    return LooksChange(ledger, version, before, after)


def restore(store, row, *, now: float) -> bool:
    """nightly.growth.undo of a "looks" ledger row, inside its transaction: her weight and bust
    as they were before the edit, with the persona's source before it, while both are still
    what the edit left. False (nothing written) when the receipt is gone or a later change
    moved them."""
    raw = store.get_meta(RECEIPT.format(ledger=row["id"]))
    if raw is None:
        return False
    receipt = json.loads(raw)
    p = load_persona(store, row["guid"])
    if (p is None or p.appearance is None
            or _values(p.appearance, receipt["after"].keys()) != receipt["after"]):
        return False  # a later change wins
    restored = replace(p, appearance=replace(p.appearance, **receipt["before"]))
    source = receipt.get("source") if receipt.get("source") in SOURCES else "owner"
    race, class_name = _race_class(store, p.guid)
    version = save_persona(store, restored, source=source, ts=now, race=race,
                           class_name=class_name)
    if "version" in receipt:  # the protection the edit added goes, unless a later one replaced it
        held = _protection(store, p.guid)
        if held is not None and held[1] in (receipt["version"], version):
            prior = receipt.get("protection")
            if prior is None:
                store.conn.execute("DELETE FROM persona_looks_protection WHERE guid=?", (p.guid,))
            else:
                store.conn.execute("INSERT OR REPLACE INTO persona_looks_protection "
                                   "VALUES (?, ?, ?)", (p.guid, prior[0], prior[1]))
    return True


def _protection(store, guid: int) -> list | None:
    """Her persona_looks_protection row as [reason, version] (the variety pass leaves protected
    looks alone; the owner-save trigger of migration 0028 adds it), or None."""
    row = store.conn.execute("SELECT reason, version FROM persona_looks_protection WHERE guid=?",
                             (guid,)).fetchone()
    return None if row is None else [row[0], row[1]]


class LooksService:
    """The running mind's side of the `looks` op: set {guid, weight_kg, bust[, glutes, waist,
    hips]} and undo
    {ledger}, each answered {"ok", "guid", "ledger", "set", "message"} or {"ok": False,
    "error"}. Only cast members (on the stored roster at the cast's level, not the owner) with a
    stored persona and looks."""

    def __init__(self, app):
        self.app = app

    async def handle(self, args: dict) -> dict:
        action = args.get("action")
        if action not in ACTIONS:
            return {"ok": False, "error": "a looks action is one of " + ", ".join(ACTIONS)}
        try:
            reply = self._set(args) if action == "set" else self._undo(args["ledger"])
        except LooksError as e:
            log("looks_change", action=action, outcome="refused")
            return {"ok": False, "error": str(e)}
        log("looks_change", action=action, guid=reply["guid"], ledger=reply["ledger"],
            set=reply["set"], outcome="unchanged" if reply["ledger"] is None else "ok")
        return reply

    def _cast_member(self, guid: int) -> None:
        app = self.app
        m = app.store.cast().get(guid)
        if guid == app.human_guid or m is None or m.level < CAST_MIN_LEVEL:
            raise LooksError("No such member in the cast.")

    def _set(self, args: dict) -> dict:
        app, guid = self.app, args["guid"]
        self._cast_member(guid)
        made = set_looks(app.store, guid, weight_kg=args["weight_kg"], bust=args["bust"],
                         now=app.clock.now(),
                         **{k: args[k] for k in ("glutes", "waist", "hips") if k in args})
        if made is None:
            return {"ok": True, "guid": guid, "ledger": None, "set": None,
                    "message": "Nothing changed: those are already their looks."}
        return self._after(guid, made.ledger, "Saved (ledger #%d)." % made.ledger)

    def _undo(self, ledger: int) -> dict:
        from leeroy_mind.nightly.growth import undo
        app = self.app
        with app.store.transaction():
            row = app.store.conn.execute("SELECT guid, kind FROM persona_ledger WHERE id=?",
                                         (ledger,)).fetchone()
        if row is None or row["kind"] != KIND:
            raise LooksError(f"Ledger #{ledger} is not a weight or figure change.")
        self._cast_member(row["guid"])
        if not undo(app.store, ledger, now=app.clock.now(), personas=app.personas):
            raise LooksError(f"Ledger #{ledger} was undone already, or a later change moved "
                             "the weight or figure: undo that first.")
        return self._after(row["guid"], ledger, "Undone.")

    def _after(self, guid: int, ledger: int, said: str) -> dict:
        """The persona cache drops the member; then a fresh set is queued (its words added)."""
        app = self.app
        personas = getattr(app, "personas", None)
        if personas is not None:
            personas.invalidate(guid)
        selfies = getattr(app, "selfies", None)
        set_no = None
        if selfies is not None:
            try:
                set_no, words = selfies.queue_for_looks(guid)
            except Exception as e:  # the looks are saved; the set can be asked for with "4 more"
                log("looks_queue_failed", level=logging.WARNING, guid=guid,
                    error=type(e).__name__)
                words = "No new set was queued; ask for four more."
            said = f"{said} {words}"
        return {"ok": True, "guid": guid, "ledger": ledger, "set": set_no, "message": said}
