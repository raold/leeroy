"""Varied sets (owner, 2026-11-14): one set of four candidates for a woman of the lust style in
which every candidate has its own look and pose, composed by code from fixed lists
(kind.VARIED_LOOKS, VARIED_VIEWS, VARIED_FRAMINGS), never written by a model: swimwear at the
water, gym wear at the gym, a casual outfit and a going-out one, each photographed from another
side (from behind looking back, a three-quarter rear view, in profile, from the front), framed
full-length or three-quarter so her figure shows.

Off by default: `[selfies] varied_sets = true` (read at the mind's start, as the rest of the
table). With it off nothing is queued, and a set queued while it was on renders as an ordinary
set of her own photo kind.

- Queueing (`queue`) writes exactly what his "4 more" writes (store.queue_more: the sets waiting
  for his choice passed, one set queued), plus the set's plan (store.VARIED_SET_KEY: the four
  looks) and the local day it was queued (DAY_KEY), so a second run that day queues nothing for
  her. A member with a set already queued is left alone. The running mind does it through the
  `selfie` op's `vary` action; `selfies vary` does it with the mind stopped, or asks the running
  mind.
- Rendering stays the night's `selfies` stage and the booth's, under their GPU rules: render.py
  builds one prompt per candidate from the plan (looks_for) while the switch is on and she still
  has the lust style and every look still passes the screens; otherwise the set renders as an
  ordinary set and its plan is dropped when it becomes ready, so a ready set's plan is always
  what its candidates show.
- Her photo kind and her cover are unchanged. Any candidate may become her cover (the lust-set
  pick rules hold: a varied set is a lust set); a pick of one records its look
  (store.PICK_LOOK_KEY), and the situational photos then cut her face from it by its framing.

Logs and output carry guids, counts, codes and list indices, never a prompt or persona text."""
import json
import random
import re
from dataclasses import asdict, dataclass

from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.schema import Persona
from leeroy_mind.wow import CAST_MIN_LEVEL

from . import kind
from .prompt import renderable
from .store import VARIED_SET_KEY, queue_more
from .style import PLAIN, suggestive

DAY_KEY = "selfies:varied:day:{guid}"
PLAN_VERSION = 1
STANCES = ("lust",)
_KIND = "snapshot"  # someone else takes each one: no selfie shows her from behind
_DRESS = re.compile(r"dress\b")  # a dress, a sundress: one a set at most


@dataclass(frozen=True)
class Look:
    category: str  # kind.CATEGORIES
    view: str  # kind.VARIED_VIEWS
    framing: str  # kind.VARIED_FRAMINGS
    setting: str
    outfit: str  # the list's choice, without its colour
    colour: str
    expression: str

    @property
    def phrase(self) -> str:
        """The outfit as it is worn: its colour put in and an article in front."""
        return kind.outfit_phrase(self.outfit, self.colour, kind.VARIED_OUTFITS)

    def data(self) -> dict:
        """The look in screen_lust's shape (varied)."""
        return {"kind": _KIND, "framing": self.framing, "view": self.view,
                "setting": self.setting, "outfit": self.outfit, "colour": self.colour,
                "expression": self.expression}

    def codes(self) -> dict:
        """The look as codes and list indices: what output and the gallery may show."""
        outfits, settings = kind.VARIED_LOOKS[self.category]
        return {"category": self.category, "view": self.view, "framing": self.framing,
                "outfit": outfits.index(self.outfit), "setting": settings.index(self.setting),
                "colour": kind.LUST_COLOURS.index(self.colour),
                "expression": kind.LUST_EXPRESSIONS.index(self.expression)}


def screen(p: Persona, look: Look) -> list[str]:
    """kind.screen_lust's problems with `look` for `p`, its category's lists checked too."""
    outfits, settings = kind.VARIED_LOOKS.get(look.category, ((), ()))
    if look.outfit not in outfits or look.setting not in settings:
        return ["photo.category: not on its list"]
    return kind.screen_lust(p, look.data(), varied=True)


def compose(p: Persona, seed: str) -> tuple[Look, ...] | None:
    """Four looks for `p`, one of each category in kind.CATEGORIES order, each with its own
    view, colour and expression and at most one dress among them, every one passing screen();
    None when a category has no safe look left (her names can clash with every choice).
    Deterministic in `seed`."""
    rng = random.Random(f"selfie-varied:{seed}")
    views = list(kind.VARIED_VIEWS)
    rng.shuffle(views)
    colours = list(kind.LUST_COLOURS)
    rng.shuffle(colours)
    expressions = list(kind.LUST_EXPRESSIONS)
    rng.shuffle(expressions)
    looks: list[Look] = []
    for category, view in zip(kind.CATEGORIES, views):
        outfits, settings = kind.VARIED_LOOKS[category]
        framing = (rng.choice(kind.VARIED_FRAMINGS) if view in kind.BACK_VIEWS
                   else "full_body")
        dressed = any(_DRESS.search(x.outfit) for x in looks)
        options = [(o, s) for o in outfits for s in settings
                   if not (dressed and _DRESS.search(o))]
        rng.shuffle(options)
        found = None
        used_c = {x.colour for x in looks}
        used_e = {x.expression for x in looks}
        for outfit, setting in options:
            for colour in (c for c in colours if c not in used_c):
                for expression in (e for e in expressions if e not in used_e):
                    look = Look(category, view, framing, setting, outfit, colour, expression)
                    if not screen(p, look):
                        found = look
                        break
                if found:
                    break
            if found:
                break
        if found is None:
            return None
        looks.append(found)
    return tuple(looks)


def plan_of(conn, guid: int, set_no: int) -> tuple[Look, ...] | None:
    """The four looks stored for that set, or None (none stored, or not readable as four looks
    from the lists). Reads any connection, the console's read-only one too."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (VARIED_SET_KEY.format(guid=guid, set_no=set_no),)).fetchone()
    except Exception:  # a database without the meta table
        return None
    if row is None:
        return None
    try:
        rec = json.loads(row[0])
        looks = tuple(Look(**x) for x in rec["looks"])
    except (ValueError, TypeError, KeyError):
        return None
    if len(looks) != 4 or any(
            x.category not in kind.VARIED_LOOKS or x.view not in kind.VARIED_VIEWS
            or x.framing not in kind.VARIED_FRAMINGS or x.colour not in kind.LUST_COLOURS
            or x.expression not in kind.LUST_EXPRESSIONS
            or x.outfit not in kind.VARIED_LOOKS[x.category][0]
            or x.setting not in kind.VARIED_LOOKS[x.category][1] for x in looks):
        return None
    return looks


def looks_for(store, p: Persona | None, guid: int, set_no: int) -> tuple[Look, ...] | None:
    """The looks a queued set renders with: its plan while she has the lust style and every look
    still passes the screens (a rename since, say); None renders it as an ordinary set."""
    if p is None or not suggestive(p):
        return None
    with store.transaction():
        looks = plan_of(store.conn, guid, set_no)
    if looks is None or any(screen(p, x) for x in looks):
        return None
    return looks


def label(look: Look) -> str:
    """A few plain words for the gallery: what she wears and how she was photographed."""
    return f"{_CATEGORY_WORDS[look.category]}, {_VIEW_WORDS[look.view]}"


_CATEGORY_WORDS = {"swim": "swimwear", "gym": "gym wear", "casual": "casual",
                   "going_out": "going out"}
_VIEW_WORDS = {"behind": "from behind", "rear_three_quarter": "three-quarter rear view",
               "side": "side profile", "front": "from the front"}


# -- queueing -----------------------------------------------------------------------------------

def _members(store, human: int) -> tuple[dict[int, Persona], list[int]]:
    """The cast's stored personas (on the roster at the cast's level, not him), and the guids
    whose persona cannot be read."""
    personas, unreadable = {}, []
    for guid, m in sorted(store.cast().items()):
        if guid == human or m.level < CAST_MIN_LEVEL:
            continue
        try:
            p = load_persona(store, guid)
        except ValueError:
            unreadable.append(guid)
            continue
        if p is not None:
            personas[guid] = p
    return personas, unreadable


def _skip(conn, store, p: Persona, day: str) -> str | None:
    if p.appearance is None:
        return "no_looks"
    if renderable(p, kind.chosen_style(store, p) or PLAIN) is not None:
        return "not_renderable"
    if store.get_meta(DAY_KEY.format(guid=p.guid)) == day:
        return "already_today"
    if conn.execute("SELECT 1 FROM selfie_set WHERE guid=? AND status='queued'",
                    (p.guid,)).fetchone() is not None:
        return "already_queued"
    return None


def _next_set(conn, guid: int) -> int:
    return (conn.execute("SELECT max(set_no) FROM selfie_set WHERE guid=?",
                         (guid,)).fetchone()[0] or 0) + 1


def queue(store, *, human: int, now: float, day: str, go: bool, stance: str = "lust") -> dict:
    """Plan (go False, read-only) or queue (go True, one transaction) a varied set for every
    cast member of `stance`'s style (only "lust": style.suggestive) who has none queued, none
    queued today (`day`, the local date) and a photo kind the night can render. The report:
    guids with each set's looks as codes, the skipped by code, and how many were not of the
    stance."""
    if stance not in STANCES:
        raise ValueError("stance_unsupported")
    report = {"dry_run": not go, "stance": stance, "day": day, "members": 0, "queued": [],
              "skipped": [], "not_stance": 0}
    with store.transaction():
        conn = store.conn
        personas, unreadable = _members(store, human)
        report["skipped"] += [{"guid": g, "code": "persona_unreadable"} for g in unreadable]
        versions = store.persona_versions()
        for guid, p in personas.items():
            if not suggestive(p):
                report["not_stance"] += 1
                continue
            report["members"] += 1
            why = _skip(conn, store, p, day)
            set_no = _next_set(conn, guid)
            looks = None if why else compose(p, f"{guid}:{set_no}:{versions.get(guid)}")
            if why is None and looks is None:
                why = "no_safe_look"
            if why:
                report["skipped"].append({"guid": guid, "code": why})
                continue
            if go:
                made = queue_more(store, guid, now=now)
                assert made.set_no == set_no
                store.set_meta(VARIED_SET_KEY.format(guid=guid, set_no=set_no), json.dumps(
                    {"v": PLAN_VERSION, "day": day, "looks": [asdict(x) for x in looks]},
                    sort_keys=True))
                store.set_meta(DAY_KEY.format(guid=guid), day)
            report["queued"].append({"guid": guid, "set": set_no,
                                     "looks": [x.codes() for x in looks]})
    return report


def summary(report: dict) -> dict:
    """The report without the looks, grouped: what the control op answers."""
    skipped: dict[str, list[int]] = {}
    for row in report["skipped"]:
        skipped.setdefault(row["code"], []).append(row["guid"])
    return {"dry_run": report["dry_run"], "stance": report["stance"], "day": report["day"],
            "members": report["members"], "queued": [r["guid"] for r in report["queued"]],
            "skipped": skipped, "not_stance": report["not_stance"]}
