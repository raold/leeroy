"""The owner's weight and bust edits (owner, 2026-11-15: "I want to be able to adjust weight and
bust individually. Adjusting them triggers a new selfie render."), from the console's Looks panel
through the running mind's `looks` control op (endpoint.looks_args, ControlEndpoint.looks).

- set_looks: one transaction. The stored persona is read again, its appearance's weight_kg and
  bust replaced, and it is saved as a new persona version with source "owner" (a real persona
  write, never a godmode lens; persona_looks_protection then keeps the variety pass off her
  looks), with a persona ledger row of kind "looks" whose private receipt (meta RECEIPT) holds
  both values before and after and the persona's source before. The same values again write
  nothing. A night writer that saves her persona later reads it again first, so it keeps them.
- restore, nightly.growth.undo's "looks" branch, so `personas undo <id>` and the console's Undo
  both take one back: both values as they were, with the source before, while they are still
  what the edit left (a later edit wins: undo that first).
- LooksService: the op's two actions, set and undo, for cast members only. After either, the
  mind's persona cache drops the member and the selfie service queues a fresh profile set
  (SelfieService.queue_for_looks: a varied one for the lust style while varied_sets is on).

Weight stays within the schema's bounds; the bust is a clothed descriptor (schema.BUSTS), for
women only. Ledger text and logs carry numbers, the enum's words, ids and outcomes only."""
import json
import logging
from dataclasses import dataclass, fields, replace

from leeroy_mind.logs import log
from leeroy_mind.wow import CAST_MIN_LEVEL

from .io import load_persona, save_persona
from .schema import BUSTS, Appearance, validate_persona

KIND = "looks"
RECEIPT = "personas:looks-undo:{ledger}"
ACTIONS = ("set", "undo")
SOURCES = ("seed", "generated", "owner", "variety")  # persona_core's sources
_WEIGHT = next(f.metadata for f in fields(Appearance) if f.name == "weight_kg")
WEIGHT_MIN, WEIGHT_MAX = _WEIGHT["lo"], _WEIGHT["hi"]


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


def describe(before: dict, after: dict) -> str:
    """The ledger row's text: what moved, in numbers and the enum's words."""
    parts = []
    if before["weight_kg"] != after["weight_kg"]:
        parts.append(f"weight {before['weight_kg']} -> {after['weight_kg']} kg")
    if before["bust"] != after["bust"]:
        parts.append(f"bust {bust_text(before['bust'])} -> {bust_text(after['bust'])}")
    return ", ".join(parts)


def _values(a: Appearance) -> dict:
    return {"weight_kg": a.weight_kg, "bust": a.bust}


def check(weight_kg, bust) -> None:
    """The values' own rules, in words: weight a whole number of kg in the schema's range, bust
    one of BUSTS or None."""
    if type(weight_kg) is not int or not WEIGHT_MIN <= weight_kg <= WEIGHT_MAX:
        raise LooksError(f"Weight is a whole number of kg from {WEIGHT_MIN} to {WEIGHT_MAX}.")
    if bust is not None and bust not in BUSTS:
        raise LooksError("Bust is one of " + ", ".join(b.replace("_", " ") for b in BUSTS)
                         + ", or not set.")


def _race_class(store, guid: int) -> tuple[str, str]:
    from leeroy_mind.nightly.enrich import _race_class as race_class
    return race_class(store, guid)


def set_looks(store, guid: int, *, weight_kg: int, bust: str | None,
              now: float) -> LooksChange | None:
    """Her weight and bust as given, saved as an owner persona version with its ledger row and
    receipt; None when they already are (nothing written). LooksError otherwise."""
    check(weight_kg, bust)
    with store.transaction():
        stored = load_persona(store, guid)
        if stored is None:
            raise LooksError("No persona is stored for this member.")
        if stored.appearance is None:
            raise LooksError("No looks yet: the night's looks pass gives them first.")
        before, after = _values(stored.appearance), {"weight_kg": weight_kg, "bust": bust}
        if before == after:
            return None
        changed = replace(stored, appearance=replace(stored.appearance, **after))
        if bust is not None and stored.identity.player_gender != "female":
            raise LooksError("Bust is for women only.")
        errs = validate_persona(changed)
        if errs:
            raise LooksError("This persona does not save: " + "; ".join(errs)[:200])
        source = store.conn.execute("SELECT source FROM persona_core WHERE guid=?",
                                    (guid,)).fetchone()[0]
        race, class_name = _race_class(store, guid)
        version = save_persona(store, changed, source="owner", ts=now, race=race,
                               class_name=class_name)
        from leeroy_mind.nightly.store import ledger_add
        ledger = ledger_add(store, guid=guid, kind=KIND, text=describe(before, after),
                            evidence=[], now=now)
        store.set_meta(RECEIPT.format(ledger=ledger), json.dumps(
            {"before": before, "after": after, "source": source}, sort_keys=True))
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
    if p is None or p.appearance is None or _values(p.appearance) != receipt["after"]:
        return False  # a later change wins
    restored = replace(p, appearance=replace(p.appearance, **receipt["before"]))
    source = receipt.get("source") if receipt.get("source") in SOURCES else "owner"
    race, class_name = _race_class(store, p.guid)
    save_persona(store, restored, source=source, ts=now, race=race, class_name=class_name)
    return True


class LooksService:
    """The running mind's side of the `looks` op: set {guid, weight_kg, bust} and undo
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
                         now=app.clock.now())
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
            raise LooksError(f"Ledger #{ledger} is not a weight or bust change.")
        self._cast_member(row["guid"])
        if not undo(app.store, ledger, now=app.clock.now(), personas=app.personas):
            raise LooksError(f"Ledger #{ledger} was undone already, or a later change moved "
                             "the weight or bust: undo that first.")
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
