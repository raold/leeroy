"""Varied sets (owner, 2026-11-14): one set of four candidates for a woman of the lust style in
which every candidate has its own look and pose, composed by code from fixed lists
(kind.VARIED_LOOKS, VARIED_VIEWS, VARIED_FRAMINGS), never written by a model: swimwear at the
water, gym wear at the gym, a casual outfit and a going-out one, each photographed from another
side (from behind looking over her shoulder, a three-quarter rear view, in profile, from the
front), framed full-length or three-quarter so her figure shows. Swimwear from behind is always
full-length.

Since the photo audit (2026-11-17, safe.py) `[selfies] body_shot_share` says how many of the
four show her figure (safe.body_slots; all four at the default 1.0, the owner's ask): below
1.0 the views from behind keep theirs first, then the front view, then the side view, and the
others are framed waist-up. Front and side views are three-quarter rather than full-length (6%
defective against 12%). The lists say their outfits and settings as safe.py rewrote them. The
audit's standing poses were taken out again (owner, 2026-11-19): each candidate's pose is its
view, as before the audit.

Off by default: `[selfies] varied_sets = true` (read at the mind's start, as the rest of the
table). With it off nothing is queued, and a set queued while it was on renders as an ordinary
set of her own photo kind.

- Queueing (`queue`) writes exactly what his "4 more" writes (store.queue_more: the sets waiting
  for his choice passed, one set queued), plus the set's plan (store.VARIED_SET_KEY: the four
  looks) and the local day it was queued (DAY_KEY), so a second run that day queues nothing for
  her. A member with a set already queued is left alone; `guids` narrows it to named members.
  The running mind does it through the `selfie` op's `vary` action; `selfies vary` does it with
  the mind stopped, or asks the running mind; `selfies try --varied` renders a trial set into
  scratch first.
- Rendering stays the night's `selfies` stage and the booth's, under their GPU rules: render.py
  builds one prompt per candidate from the plan (looks_for) while the switch is on and she still
  has the lust style and every look still passes the screens; otherwise the set renders as an
  ordinary set and its plan is dropped when it becomes ready. A slot whose prompt the worker
  finds too long renders as her ordinary photo and loses its look (None in the plan), so a ready
  set's plan is always what its candidates show. A set the NSFW gate refuses on 2 nights has its
  flagged slot composed again once as a front view; refused again, its plan is dropped
  (`refused`).
- Her photo kind and her cover are unchanged. Any candidate may become her cover (the lust-set
  pick rules hold: a varied set is a lust set); a pick of one records its look
  (store.PICK_LOOK_KEY). The identity reference of her situational photos, guild tiles and
  photo requests is never a back view (store.face_pick, FACE_VIEWS).

Logs and output carry guids, counts, codes and list indices, never a prompt or persona text."""
import json
import random
import re
from dataclasses import asdict, dataclass

from leeroy_mind.logs import log
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.schema import Persona
from leeroy_mind.wow import CAST_MIN_LEVEL

from . import kind, wardrobe
from .prompt import renderable
from .safe import BODY_SHOT_SHARE, body_slots
from .store import FACE_VIEWS, VARIED_SET_KEY, queue_more
from .style import PLAIN, suggestive

DAY_KEY = "selfies:varied:day:{guid}"
REFUSED_KEY = "selfies:set-varied-refused:{guid}:{set_no}"
PLAN_VERSION = 1
STANCES = ("lust",)
REFUSED_NIGHTS = 2  # refusals before the flagged slot is composed again as a front view
_KIND = "snapshot"  # someone else takes each one: no selfie shows her from behind
_DRESS = re.compile(r"dress\b")  # a dress, a sundress: one a set at most
assert set(FACE_VIEWS) <= set(kind.VARIED_VIEWS) and not set(FACE_VIEWS) & set(kind.BACK_VIEWS)


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


_BODY_ORDER = ("behind", "rear_three_quarter", "front", "side")  # who shows her figure first


def _framing(rng: random.Random, category: str, view: str, body: bool = True) -> str:
    """A body shot: swimwear from behind is full-length (kind.SWIM_BACK); front and side are
    three-quarter (the audit: planned three-quarter 6% defective, full-length 12%); other back
    views either. Any other candidate is framed waist-up."""
    if not body:
        return "upper_body"
    if view in kind.BACK_VIEWS:
        return "full_body" if category == "swim" else rng.choice(kind.VARIED_BODY_FRAMINGS)
    return "three_quarter"


def _assign(rng: random.Random, share: float) -> list[tuple[str, str, bool]]:
    """(category, view, body shot) for the four candidates: the views shuffled over the
    categories, the body shots given by _BODY_ORDER (swimwear seen from behind first: it is only
    ever full-length), body_slots(share) of them. With none left for swimwear seen from behind,
    it trades views with a category seen from the front or the side."""
    views = list(kind.VARIED_VIEWS)
    rng.shuffle(views)
    pairs = dict(zip(kind.CATEGORIES, views))
    n = body_slots(share, len(pairs))

    def body_views() -> set[str]:
        order = sorted(_BODY_ORDER, key=lambda v: (
            not (v in kind.BACK_VIEWS and pairs.get("swim") == v), _BODY_ORDER.index(v)))
        return set(order[:n])
    if pairs["swim"] in kind.BACK_VIEWS and pairs["swim"] not in body_views():
        other = next(c for c in kind.CATEGORIES if pairs[c] not in kind.BACK_VIEWS)
        pairs["swim"], pairs[other] = pairs[other], pairs["swim"]
    chosen = body_views()
    return [(c, v, v in chosen) for c, v in pairs.items()]


def _pick(p: Persona, rng: random.Random, category: str, view: str, framing: str,
          colours: list[str], expressions: list[str], others, tier: int = 4) -> Look | None:
    """The first look of `category` in `rng`'s order that passes screen(), with a colour and an
    expression none of `others` has where one is left, and no second dress. `tier` ([heat]):
    below P4 only the category's outfits and settings the photo tier allows (heat/photos.py),
    an outfit no other slot wears first."""
    outfits, settings = kind.VARIED_LOOKS[category]
    if tier < 4:
        from leeroy_mind.heat import photos as heat_photos
        outfits, settings = heat_photos.varied_looks(category, tier)
        worn = {x.outfit for x in others}
        outfits = tuple(o for o in outfits if o not in worn) or outfits
    dressed = any(_DRESS.search(x.outfit) for x in others)
    options = [(o, s) for o in outfits for s in settings if not (dressed and _DRESS.search(o))]
    if tier < 4 and not options:  # a tier with dresses only: a second dress beats no look
        options = [(o, s) for o in outfits for s in settings]
    rng.shuffle(options)
    options = wardrobe.ordered(p, options, outfit=lambda x: x[0])  # her wardrobe's first
    used_c = {x.colour for x in others}
    used_e = {x.expression for x in others}
    free_c = [c for c in colours if c not in used_c] or colours
    free_e = [e for e in expressions if e not in used_e] or expressions
    for outfit, setting in options:
        for colour in free_c:
            for expression in free_e:
                look = Look(category, view, framing, setting, outfit, colour, expression)
                if not screen(p, look):
                    return look
    return None


def compose(p: Persona, seed: str, share: float = BODY_SHOT_SHARE,
            tier: int | None = None) -> tuple[Look, ...] | None:
    """Four looks for `p`, one of each category in kind.CATEGORIES order, each with its own
    view, colour and expression and at most one dress among them, every one passing screen();
    `share` of them show her whole figure (_assign), the others waist-up. None when a category
    has no safe look left (her names can clash with every choice). Deterministic in `seed`.

    `tier` ([heat]): her photo tier (None: read from heat.live; P4 with heat off). At P4 this
    is the composition as it always was. Below it the four looks come from the categories,
    outfits, settings and poses the tier allows (heat/photos.py), the categories and poses
    taken in turn when there are fewer than four, with as many body shots as `share` gives."""
    if tier is None:
        from leeroy_mind.heat import live as heat_live
        tier = heat_live.photo_tier(p)
    if tier < 4:
        return _compose_tier(p, seed, tier, share)
    rng = random.Random(f"selfie-varied:{seed}")
    slots = _assign(rng, share)
    colours = list(kind.LUST_COLOURS)
    rng.shuffle(colours)
    colours = wardrobe.colours(p, colours)  # her palette's, when she has one
    expressions = list(kind.LUST_EXPRESSIONS)
    rng.shuffle(expressions)
    looks: list[Look] = []
    for category, view, body in slots:
        found = _pick(p, rng, category, view, _framing(rng, category, view, body), colours,
                      expressions, looks)
        if found is None:
            return None
        looks.append(found)
    return tuple(looks)


def _compose_tier(p: Persona, seed: str, tier: int,
                  share: float = BODY_SHOT_SHARE) -> tuple[Look, ...] | None:
    """compose below P4: the same draws over the tier's shorter lists. The body shots are
    body_slots(share) of the four, given in _BODY_ORDER as _assign gives them (never more than
    the composition at P4 has), the others waist-up. Swimwear is P4 only, so no trade of views."""
    from leeroy_mind.heat import photos as heat_photos
    rng = random.Random(f"selfie-varied:{seed}")
    views = list(heat_photos.views(tier))
    rng.shuffle(views)
    categories = heat_photos.categories(tier)
    slots = [(categories[i % len(categories)], views[i % len(views)]) for i in range(4)]
    order = sorted(range(4), key=lambda i: (_BODY_ORDER.index(slots[i][1]), i))
    body = set(order[:body_slots(share, len(slots))])
    colours = list(kind.LUST_COLOURS)
    rng.shuffle(colours)
    colours = wardrobe.colours(p, colours)
    expressions = list(kind.LUST_EXPRESSIONS)
    rng.shuffle(expressions)
    looks: list[Look] = []
    for i, (category, view) in enumerate(slots):
        found = _pick(p, rng, category, view, _framing(rng, category, view, i in body), colours,
                      expressions, looks, tier)
        if found is None:
            return None
        looks.append(found)
    return tuple(looks)


def recompose_slot(p: Persona, looks, idx: int, seed: str,
                   share: float = BODY_SHOT_SHARE) -> Look | None:
    """Slot `idx`'s look composed again as a front view of the same category (three-quarter when
    `share` gives the front view a body shot, else waist-up), kept apart from the other slots'
    colours and expressions where it can be; None when none is safe. [heat]: inside her photo
    tier, as compose."""
    rng = random.Random(f"selfie-varied-front:{seed}")
    colours = list(kind.LUST_COLOURS)
    rng.shuffle(colours)
    colours = wardrobe.colours(p, colours)
    expressions = list(kind.LUST_EXPRESSIONS)
    rng.shuffle(expressions)
    others = [x for i, x in enumerate(looks) if i != idx and x is not None]
    framing = "three_quarter" if body_slots(share) > len(kind.BACK_VIEWS) else "upper_body"
    from leeroy_mind.heat import live as heat_live  # [heat]: inside her photo tier, as compose
    return _pick(p, rng, looks[idx].category, "front", framing, colours, expressions, others,
                 heat_live.photo_tier(p))


def _valid(x) -> bool:
    return (x.category in kind.VARIED_LOOKS and x.view in kind.VARIED_VIEWS
            and x.framing in kind.VARIED_FRAMINGS and x.colour in kind.LUST_COLOURS
            and x.expression in kind.LUST_EXPRESSIONS
            and x.outfit in kind.VARIED_LOOKS[x.category][0]
            and x.setting in kind.VARIED_LOOKS[x.category][1])


def plan_of(conn, guid: int, set_no: int) -> tuple[Look | None, ...] | None:
    """The four looks stored for that set, by slot (None for a slot that renders her ordinary
    photo), or None: none stored, not readable as four looks from the lists, or no look left.
    Reads any connection, the console's read-only one too."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (VARIED_SET_KEY.format(guid=guid, set_no=set_no),)).fetchone()
    except Exception:  # a database without the meta table
        return None
    if row is None:
        return None
    try:
        rec = json.loads(row[0])
        looks = tuple(None if x is None else Look(**x) for x in rec["looks"])
    except (ValueError, TypeError, KeyError):
        return None
    if len(looks) != 4 or all(x is None for x in looks) or any(
            x is not None and not _valid(x) for x in looks):
        return None
    return looks


def write_plan(store, guid: int, set_no: int, looks, day: str | None) -> None:
    """Store a set's plan (None slots render her ordinary photo). Joins the caller's
    transaction."""
    store.set_meta(VARIED_SET_KEY.format(guid=guid, set_no=set_no), json.dumps(
        {"v": PLAN_VERSION, "day": day,
         "looks": [None if x is None else asdict(x) for x in looks]}, sort_keys=True))


def _day(store, guid: int, set_no: int) -> str | None:
    raw = store.get_meta(VARIED_SET_KEY.format(guid=guid, set_no=set_no))
    try:
        day = json.loads(raw)["day"] if raw else None
    except (ValueError, TypeError, KeyError):
        return None
    return day if isinstance(day, str) else None


def drop_plan(store, guid: int, set_no: int) -> None:
    """The set renders as an ordinary one from now on."""
    store.conn.execute("DELETE FROM meta WHERE key IN (?, ?)",
                       (VARIED_SET_KEY.format(guid=guid, set_no=set_no),
                        REFUSED_KEY.format(guid=guid, set_no=set_no)))


def looks_for(store, p: Persona | None, guid: int,
              set_no: int) -> tuple[Look | None, ...] | None:
    """The looks a queued set renders with: its plan while she has the lust style and every look
    still passes the screens (a rename since, say); None renders it as an ordinary set."""
    if p is None or not suggestive(p):
        return None
    with store.transaction():
        looks = plan_of(store.conn, guid, set_no)
    if looks is None or any(x is not None and screen(p, x) for x in looks):
        return None
    return looks


def refused(store, p: Persona, guid: int, set_no: int, slot: int | None, *,
            night: str, share: float = BODY_SHOT_SHARE) -> str:
    """A varied set the NSFW gate refused (`slot` flagged its seeds): counted; on the
    REFUSED_NIGHTS-th refusal that slot is composed again once as a front view; refused once
    more, the plan is dropped. What was done: counted, recomposed or dropped."""
    key = REFUSED_KEY.format(guid=guid, set_no=set_no)
    with store.transaction():
        try:
            rec = json.loads(store.get_meta(key) or "{}")
            n, again = int(rec.get("n", 0)) + 1, bool(rec.get("recomposed", False))
        except (ValueError, TypeError, AttributeError):
            n, again = 1, False
        looks = plan_of(store.conn, guid, set_no)
        if looks is None:
            store.conn.execute("DELETE FROM meta WHERE key=?", (key,))
            return "counted"
        if again:
            drop_plan(store, guid, set_no)
            log("selfies_varied_dropped", night=night, guid=guid, set=set_no, reason="refused")
            return "dropped"
        done = "counted"
        if n >= REFUSED_NIGHTS and slot is not None and 0 <= slot < 4 and looks[slot] is not None:
            front = recompose_slot(p, looks, slot, f"{guid}:{set_no}:{night}", share)
            if front is None:
                drop_plan(store, guid, set_no)
                log("selfies_varied_dropped", night=night, guid=guid, set=set_no,
                    reason="refused")
                return "dropped"
            write_plan(store, guid, set_no, looks[:slot] + (front,) + looks[slot + 1:],
                       _day(store, guid, set_no))
            again, done = True, "recomposed"
            log("selfies_varied_recomposed", night=night, guid=guid, set=set_no, slot=slot)
        store.set_meta(key, json.dumps({"n": n, "recomposed": again}, sort_keys=True))
        return done


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


def _skip(conn, store, p: Persona, day: str, daily: bool = True) -> str | None:
    if p.appearance is None:
        return "no_looks"
    if renderable(p, kind.chosen_style(store, p) or PLAIN) is not None:
        return "not_renderable"
    if daily and store.get_meta(DAY_KEY.format(guid=p.guid)) == day:
        return "already_today"
    if conn.execute("SELECT 1 FROM selfie_set WHERE guid=? AND status='queued'",
                    (p.guid,)).fetchone() is not None:
        return "already_queued"
    return None


def _next_set(conn, guid: int) -> int:
    return (conn.execute("SELECT max(set_no) FROM selfie_set WHERE guid=?",
                         (guid,)).fetchone()[0] or 0) + 1


def _waiting(conn, guid: int) -> int:
    """Her sets waiting for his choice: what queue_more passes."""
    return conn.execute("SELECT count(*) FROM selfie_set WHERE guid=? AND status='ready' "
                        "AND decided IS NULL", (guid,)).fetchone()[0]


def queue(store, *, human: int, now: float, day: str, go: bool, stance: str = "lust",
          guids=None, daily: bool = True, share: float = BODY_SHOT_SHARE) -> dict:
    """Plan (go False, read-only) or queue (go True, one transaction) a varied set for every
    cast member of `stance`'s style (only "lust": style.suggestive), or only those of `guids`,
    who has none queued, none queued today (`day`, the local date; not asked with `daily` False:
    his weight or bust edit, personas.looks_edit) and a photo kind the night can render. The
    report: per member the set, its looks as codes and how many sets waiting for his choice it
    passes; the skipped by code (a named guid not in the cast: not_cast); how many were not of
    the stance."""
    if stance not in STANCES:
        raise ValueError("stance_unsupported")
    wanted = None if guids is None else set(guids)
    report = {"dry_run": not go, "stance": stance, "day": day, "members": 0, "queued": [],
              "skipped": [], "not_stance": 0}
    with store.transaction():
        conn = store.conn
        personas, unreadable = _members(store, human)
        report["skipped"] += [{"guid": g, "code": "persona_unreadable"} for g in unreadable
                              if wanted is None or g in wanted]
        if wanted is not None:
            report["skipped"] += [{"guid": g, "code": "not_cast"}
                                  for g in sorted(wanted - personas.keys() - set(unreadable))]
        versions = store.persona_versions()
        for guid, p in personas.items():
            if wanted is not None and guid not in wanted:
                continue
            if not suggestive(p):
                report["not_stance"] += 1
                continue
            report["members"] += 1
            why = _skip(conn, store, p, day, daily)
            set_no = _next_set(conn, guid)
            looks = None if why else compose(p, f"{guid}:{set_no}:{versions.get(guid)}",
                                             share)
            if why is None and looks is None:
                why = "no_safe_look"
            if why:
                report["skipped"].append({"guid": guid, "code": why})
                continue
            passes = _waiting(conn, guid)
            if go:
                made = queue_more(store, guid, now=now)
                assert made.set_no == set_no
                write_plan(store, guid, set_no, looks, day)
                store.set_meta(DAY_KEY.format(guid=guid), day)
            report["queued"].append({"guid": guid, "set": set_no, "passes": passes,
                                     "looks": [x.codes() for x in looks]})
    return report


def summary(report: dict) -> dict:
    """The report without the looks, grouped: what the control op answers."""
    skipped: dict[str, list[int]] = {}
    for row in report["skipped"]:
        skipped.setdefault(row["code"], []).append(row["guid"])
    return {"dry_run": report["dry_run"], "stance": report["stance"], "day": report["day"],
            "members": report["members"], "queued": [r["guid"] for r in report["queued"]],
            "passes": {str(r["guid"]): r["passes"] for r in report["queued"] if r["passes"]},
            "skipped": skipped, "not_stance": report["not_stance"]}
