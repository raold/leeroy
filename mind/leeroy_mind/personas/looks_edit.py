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
enums' words, ids and outcomes only.

The same edit carries his hair, wardrobe, makeup and accessories (owner, 2026-11-17; STYLE: the
schema's owner_hair_colour, owner_hair_length, owner_hair_style, wardrobe, palette, makeup,
glasses, jewellery, tattoos, piercings), for women and men: each one of its schema values or
None (not set: the looks' own text stands), the wardrobe and palette lists of distinct values
(empty: not set). Makeup is a woman's, three hair styles are a man's (schema.MEN_HAIR_STYLES),
and the swim tag is for a woman who has the lust style. One save is still one persona version,
one ledger row and one queued set."""
import json
import logging
from dataclasses import dataclass, fields, replace

from leeroy_mind.logs import log
from leeroy_mind.wow import CAST_MIN_LEVEL

from .io import load_persona, save_persona
from .schema import (BUSTS, GLASSES, GLUTES, HAIR_COLOURS, HAIR_LENGTHS, HAIR_STYLES, HIPS,
                     JEWELLERY, MAKEUPS, MEN_HAIR_STYLES, OWNER_SETS, OWNER_STYLE, PALETTE,
                     PIERCINGS, TATTOOS, WAISTS, WARDROBES, Appearance, validate_persona)

KIND = "looks"
RECEIPT = "personas:looks-undo:{ledger}"
ACTIONS = ("set", "undo")
SOURCES = ("seed", "generated", "owner", "variety")  # persona_core's sources
_WEIGHT = next(f.metadata for f in fields(Appearance) if f.name == "weight_kg")
WEIGHT_MIN, WEIGHT_MAX = _WEIGHT["lo"], _WEIGHT["hi"]
SHAPES = {"bust": BUSTS, "glutes": GLUTES, "waist": WAISTS, "hips": HIPS}  # the owner's, women's
# his hair, wardrobe, makeup and accessories, women's and men's: name -> its values
STYLE = {"owner_hair_colour": HAIR_COLOURS, "owner_hair_length": HAIR_LENGTHS,
         "owner_hair_style": HAIR_STYLES, "wardrobe": WARDROBES, "palette": PALETTE,
         "makeup": MAKEUPS, "glasses": GLASSES, "jewellery": JEWELLERY, "tattoos": TATTOOS,
         "piercings": PIERCINGS}
assert tuple(STYLE) == OWNER_STYLE
_MAX = {f.name: f.metadata["max_items"] for f in fields(Appearance) if f.name in OWNER_SETS}
FIELDS = ("weight_kg", *SHAPES, *STYLE)
KEEP = object()  # a shape not given: kept as stored
_NAMED = {"bust": "Bust is", "glutes": "Glutes are", "waist": "Waist is", "hips": "Hips are",
          "owner_hair_colour": "Hair colour is", "owner_hair_length": "Hair length is",
          "owner_hair_style": "Hair style is", "wardrobe": "Wardrobe is",
          "palette": "Palette is", "makeup": "Makeup is", "glasses": "Glasses are",
          "jewellery": "Jewellery is", "tattoos": "Tattoos are", "piercings": "Piercings are"}
_LABEL = {"owner_hair_colour": "hair colour", "owner_hair_length": "hair length",
          "owner_hair_style": "hair style"}


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
    for name in STYLE:
        if name in after and before.get(name) != after[name]:
            parts.append(f"{_LABEL.get(name, name)} {style_text(before.get(name))} -> "
                         f"{style_text(after[name])}")
    return ", ".join(parts)


def style_text(value) -> str:
    """A style value in the enums' words: "dark brown", "casual, sporty", "not set"."""
    if value is None or value == [] or value == ():
        return "not set"
    if isinstance(value, (list, tuple)):
        return ", ".join(v.replace("_", " ") for v in value)
    return value.replace("_", " ")


def _values(a: Appearance, keys=FIELDS) -> dict:
    """The looks' values by name as JSON holds them (a wardrobe or palette as a list), so a
    receipt read back compares equal."""
    return {k: list(v) if isinstance(v := getattr(a, k), tuple) else v for k in keys}


def _applied(a: Appearance, values: dict) -> Appearance:
    return replace(a, **{k: tuple(v) if isinstance(v, list) else v for k, v in values.items()})


def check(weight_kg, bust, glutes=KEEP, waist=KEEP, hips=KEEP, **style) -> None:
    """The values' own rules, in words: weight a whole number of kg in the schema's range, each
    shape one of its schema values or None (not set); KEEP leaves a shape as stored. `style`:
    any of STYLE by name, each one of its values or None, a wardrobe or palette a list of
    distinct values of its own, no longer than the schema allows."""
    unknown = set(style) - set(STYLE)
    if unknown:
        raise LooksError("Not a looks value: " + ", ".join(sorted(unknown)) + ".")
    for name, value in style.items():
        options = ", ".join(v.replace("_", " ") for v in STYLE[name])
        if name in OWNER_SETS:
            if (not isinstance(value, (list, tuple)) or len(value) > _MAX[name]
                    or not all(isinstance(v, str) and v in STYLE[name] for v in value)
                    or len(set(value)) != len(value)):
                raise LooksError(f"{_NAMED[name]} up to {_MAX[name]} of {options}.")
        elif value is not None and (not isinstance(value, str) or value not in STYLE[name]):
            raise LooksError(f"{_NAMED[name]} one of {options}, or not set.")
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
              glutes=KEEP, waist=KEEP, hips=KEEP, **style) -> LooksChange | None:
    """Her weight and shapes as given (a shape left at KEEP stays as stored) and any of STYLE
    (one not given stays as stored), saved as an owner persona version with its ledger row and
    receipt; None when they already are (nothing written). LooksError otherwise."""
    check(weight_kg, bust, glutes, waist, hips, **style)
    given = {"glutes": glutes, "waist": waist, "hips": hips,
             **{k: list(v) if isinstance(v, (list, tuple)) else v for k, v in style.items()}}
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
        changed = replace(stored, appearance=_applied(stored.appearance, after))
        woman = stored.identity.player_gender == "female"
        if not woman:
            for name in (*SHAPES, "makeup"):
                if after[name] is not None:
                    raise LooksError(f"{_NAMED[name]} for women only.")
        if stored.identity.player_gender != "male" and after["owner_hair_style"] in MEN_HAIR_STYLES:
            raise LooksError("That hair style is a man's: pick another.")
        if "swim" in after["wardrobe"] and "swim" not in before["wardrobe"]:
            from leeroy_mind.selfies.style import suggestive
            if not suggestive(stored):
                raise LooksError("Swim and beach wear is for the lust style only.")
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
    restored = replace(p, appearance=_applied(p.appearance, receipt["before"]))
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
                         **{k: args[k] for k in ("glutes", "waist", "hips", *STYLE)
                            if k in args})
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
            raise LooksError(f"Ledger #{ledger} is not a looks change.")
        self._cast_member(row["guid"])
        if not undo(app.store, ledger, now=app.clock.now(), personas=app.personas):
            raise LooksError(f"Ledger #{ledger} was undone already, or a later change moved "
                             "the same looks: undo that first.")
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
