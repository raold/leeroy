"""heat.resolve: one member's heat, as the small record every reader takes.

Pure: no I/O, no clock, no config object. The fixed limits are not here and nothing here reaches
them: the photo tier only selects among the vetted lists (heat/photos.py), whether she has the
lust photo style at all is decided before heat (`Member.lust_style`: selfies.style.suggestive,
the age rules), and no value read here is the NSFW gate's or a screen's.

Order: the world value, her offset (or the dial she has of her own while no offset is pinned),
the group offsets, clamped 0-10; then the session override ("clean" caps it, "open" sets 10,
never her stance); then how far his own lines have led; and last, with `public_cap`, the cap on
every channel other people read."""
from dataclasses import dataclass

from .table import (CLEAN_CAP, DIAL_TO_HEAT, EVERY_TURN, LATE_SELF, OFFSET_HI, OFFSET_LO, OPEN_HEAT,
                    P0, PHOTO_TIERS, PUBLIC_TIER, ROWS, TIERS, TODAY, clamp, lower, tier_index)

ATTITUDES = ("fond", "drawn", "lust")
SESSIONS = ("clean", "open")

# Why a value is what it is, in the page's words (explain). Codes go to logs, words to him.
REASONS = {
    "man": "men stay friends: heat does not apply",
    "stance_none": "stance none: heat does not apply",
    "own_dial": "her own dial counts as her offset on the world value",
    "clean": "clean for this session",
    "open": "no limits for this session",
    "lead": "she follows your lead: not at her ceiling until your own lines turn it up",
    "public": "other people read this channel: flirty at most",
    "plain_style": "plain photo style: the photo tiers do not apply",
    "age_guard": "age guard: plain photos",
    "voice": "an explicit line is typed, not spoken",
}


@dataclass(frozen=True)
class Member:
    """What resolve reads of her. `own_dial`: persona.romance.dial where it counts (a woman
    with an attitude). `offset`: the offset pinned for her, None when none is. `groups`: the sum
    of her groups' offsets. `lust_style`: she has the lust photo style (the age rules passed,
    before heat); `photo_reason`: why not ("plain_style" or "age_guard")."""
    female: bool = True
    stance: str = "none"
    own_dial: int | None = None
    offset: int | None = None
    groups: int = 0
    lust_style: bool = False
    photo_reason: str = "plain_style"


@dataclass(frozen=True)
class Resolved:
    applies: bool  # she has a romance toward him at all (a woman with an attitude)
    heat: int  # her effective heat, 0-10
    world: int
    offset: int  # hers as shown: the pinned one, or her own dial's distance from the world
    groups: int
    tier: str  # the text ceiling on this channel, now
    ceiling: str  # the heat's own tier, before his lead and the public cap
    dial: int  # the legacy romance dial the older readers take
    flirt: float
    reassure: bool
    lead: int  # his heat lines still needed before she reaches her ceiling
    starts: float
    every_turn: str  # "": by share; "private": every private turn to him; "all": every turn
    voice: str  # the highest tier a voice note may speak
    photo: int
    offers: float
    session: str | None
    reasons: tuple[tuple[str, str], ...] = ()  # (what was capped, why: a REASONS code)

    def reason(self, what: str) -> str:
        return next((code for w, code in self.reasons if w == what), "")

    def speaks(self, tier: str | None) -> bool:
        """Whether a voice note may speak a line of this tier (None: a line with no romance)."""
        return tier is None or tier == "none" or tier_index(tier) <= tier_index(self.voice)

    @property
    def speaks_explicit(self) -> bool:
        """A voice note may speak a line that is itself explicit (heat 10, or a no-limits
        evening). Otherwise such a line is typed; every other line is spoken as before."""
        return self.voice == "explicit"


OWN_BASE = 9  # a woman's own dial counts as an offset from this: own 10 is +1, own 9 is +0


def own_offset(own_dial: int) -> int:
    """A dial she has of her own (persona.romance.dial) as an offset on the world heat: its
    distance from OWN_BASE, within the offset range (ruling 2b: the world value reaches her)."""
    return max(OFFSET_LO, min(OFFSET_HI, int(own_dial) - OWN_BASE))


def resolve(world: int, member: Member, *, private: bool = True, session: str | None = None,
            public_cap: bool = False, late: bool = False, his_heat: int = 0,
            legacy: int | None = None) -> Resolved:
    """`world`: the world heat. `private`: a whisper or his one-to-one DM. `session`: "clean",
    "open" or None. `late`: late at night. `his_heat`: his own heat lines in this conversation
    so far. `legacy`: the romance dial the world heat was derived from, when it was (so the
    older readers get that very dial back).

    Her heat is the world's plus her offset plus her groups'. With no offset pinned, a dial she
    has of her own counts as her offset (own_offset), so the world value reaches her too. The
    rates (how often she starts it, how often a photo is offered) follow the heat the owner set
    (world, a pinned offset, groups, the session), never that stored dial: at world 9 a woman
    at her own 10 starts and offers as she did before the dial."""
    world = clamp(world)
    reasons: list[tuple[str, str]] = []
    applies = member.female and member.stance in ATTITUDES
    own = member.own_dial if applies and member.offset is None else None
    if own is not None:
        offset = own_offset(own)
        if offset:
            reasons.append(("heat", "own_dial"))
    else:
        offset = member.offset or 0
    heat = clamp(world + offset + member.groups)
    rate = clamp(world + (member.offset or 0) + member.groups)  # what the owner set
    session = session if session in SESSIONS and applies else None  # never her stance
    if session == "clean":
        if heat > CLEAN_CAP:
            reasons.append(("heat", "clean"))
        heat, rate = min(heat, CLEAN_CAP), min(rate, CLEAN_CAP)
    elif session == "open":
        heat = rate = OPEN_HEAT
        reasons.append(("heat", "open"))
    row, rates = ROWS[heat], ROWS[rate]
    plain = legacy is not None and own is None and heat == DIAL_TO_HEAT[legacy]
    if not applies:
        reasons.append(("heat", "man" if not member.female else "stance_none"))
        reasons.append(("photo", member.photo_reason))
        return Resolved(False, heat, world, 0, member.groups, "none", "none",
                        legacy if plain else row.dial, 1.0, True, 0, 1.0, "", "none", P0, 1.0,
                        None, tuple(reasons))
    dial = legacy if plain else row.dial
    ceiling = tier = row.tier
    need = 0 if (heat == LATE_SELF and late) else row.lead
    lead = max(0, need - max(0, his_heat))
    if lead and tier_index(tier) > tier_index("warm"):
        tier = TIERS[tier_index(tier) - 1]
        reasons.append(("tier", "lead"))
    if public_cap and not private and tier_index(tier) > tier_index(PUBLIC_TIER):
        tier = PUBLIC_TIER  # last, and nothing above can lift it
        reasons.append(("tier", "public"))
    voice = ceiling if session == "open" else lower(row.voice, ceiling)
    if voice != "explicit" and ceiling == "explicit":
        reasons.append(("voice", "voice"))
    if member.lust_style:
        photo = row.photo
    else:
        photo = P0  # the age rules and the style are decided before heat, never by it
        reasons.append(("photo", member.photo_reason))
    every = ""
    if heat >= EVERY_TURN:  # her own dial 10 carries it on every turn, as it always did
        every = "all" if own == 10 else "private"
    return Resolved(True, heat, world, offset, member.groups, tier, ceiling, dial, row.flirt,
                    row.reassure, lead, rates.starts, every, voice, photo, rates.offers, session,
                    tuple(reasons))


# -- in words -----------------------------------------------------------------------------------

_WORDING = {
    "none": "no romance wording at all",
    "warm": "warm",
    "flirty": "flirty",
    "suggestive": "suggestive",
    "frank": "frank (direct about wanting, nothing graphic)",
    "explicit": "explicit, in your own words",
}
_PHOTOS = (
    "the most covered outfits at everyday places; no going-out wear, gym wear or swimwear",
    "covered, dressed outfits; cafes, parks and streets too; no going-out wear, gym wear or "
    "swimwear",
    "going-out outfits, bars and terraces; no gym wear or swimwear",
    "going-out outfits and gym wear at the gym; no swimwear",
    "every outfit on the lists, swimwear at the water included",
)


def _lead_words(heat: int) -> str:
    row = ROWS[heat]
    if row.tier == "none":
        return "She never starts or follows a flirt."
    if row.lead >= 2:
        return "She follows your lead, after two flirts of your own."
    if heat == LATE_SELF:
        return "She follows your lead; late at night she may take one step herself."
    if row.lead == 1:
        return "She follows your lead."
    if heat >= EVERY_TURN:
        return "She may open at her top tier without your lead, in private."
    return "She goes as far as her stance takes her, as before the dial."


def _starts_words(heat: int) -> str:
    row = ROWS[heat]
    if row.starts <= 0:
        return "She never starts it."
    if row.starts == 1.0:
        return "She starts it as often as before the dial."
    return f"She starts it x{row.starts:g} as often."


def _voice_words(heat: int) -> str:
    row = ROWS[heat]
    if row.voice == "explicit":
        return "Voice notes: any line may be spoken."
    return "Voice notes: a plainly explicit line is typed, not spoken."


def explain_row(heat: int) -> str:
    """The table's row for a heat value in plain words (the Tuning page's line under the
    slider), built from the same ROWS the code reads."""
    heat = clamp(heat)
    row = ROWS[heat]
    parts = [f"Wording: {_WORDING[row.tier]}.", _lead_words(heat), _starts_words(heat),
             _voice_words(heat),
             f"Photos ({PHOTO_TIERS[row.photo]}): {_PHOTOS[row.photo]}; offered "
             f"x{row.offers:g} as often."]
    if heat == TODAY:
        parts.append("This is how it was before the dial.")
    return " ".join(parts)


def what_changes(before: int, after: int) -> str:
    """"8 to 6: ..." for a slider that moved: only what differs between the two rows."""
    before, after = clamp(before), clamp(after)
    if before == after:
        return ""
    a, b = ROWS[before], ROWS[after]
    out = []
    if a.tier != b.tier:
        verb = "rises" if tier_index(b.tier) > tier_index(a.tier) else "drops"
        out.append(f"wording {verb} from {a.tier} to {b.tier}")
    if a.photo != b.photo:
        verb = "gain" if b.photo > a.photo else "lose"
        names = [PHOTO_TIERS[i] + (" wear" if PHOTO_TIERS[i] in ("gym", "swim") else " outfits")
                 for i in range(min(a.photo, b.photo) + 1, max(a.photo, b.photo) + 1)]
        out.append(f"photos {verb} {', '.join(names)}")
    if (a.voice == "explicit") != (b.voice == "explicit"):
        out.append("voice notes may speak an explicit line" if b.voice == "explicit"
                   else "an explicit line is typed, not spoken")
    if a.starts != b.starts:
        out.append(f"she starts it x{b.starts:g} as often")
    if a.lead != b.lead or (before == LATE_SELF) != (after == LATE_SELF):
        out.append(_lead_words(after)[:-1].lower())
    if a.offers != b.offers:
        out.append(f"photo offers x{b.offers:g}")
    return f"{before} to {after}: " + ("; ".join(out) if out else "nothing you would notice") + "."


def explain(r: Resolved) -> dict:
    """One member's resolved record in the page's words: her card's lines, with why each thing
    is capped. Words only: no persona text, no overlay text."""
    if not r.applies:
        return {"applies": False, "title": REASONS[r.reason("heat")], "lines": [],
                "caps": [REASONS[r.reason("heat")]]}
    title = f"Heat {r.heat} (world {r.world}, hers {r.offset:+d}"
    if any(c == "own_dial" for _, c in r.reasons):
        title += " from her own dial"
    if r.groups:
        title += f", groups {r.groups:+d}"
    title += ")"
    lines = [f"Wording: {_WORDING[r.tier]}.",
             "Voice notes: any line may be spoken." if r.speaks_explicit
             else "Voice notes: a plainly explicit line is typed, not spoken.",
             f"Photos: {PHOTO_TIERS[r.photo]} ({_PHOTOS[r.photo]})."]
    caps = [REASONS[code] for _, code in r.reasons if code in REASONS and code != "voice"]
    return {"applies": True, "title": title, "lines": lines, "caps": list(dict.fromkeys(caps))}
