"""Member romances (`[drama] member_romance`, owner-approved 2026-11-15).

A man and a woman of the cast who like each other may start a romance; it climbs a short stretch
of the stored romance ladder through their nightly scenes, and the owner can draw the woman away.

- **Who.** A man and a woman (identity.player_gender): personas carry no orientation. Neither
  may be taken at home (her household names a spouse or partner, `taken`), already in a member
  romance, or a woman whose own romance with him has begun (banter or later): his romances are
  never broken by members. A woman drawn to or lusting after him (her stance) starts one at
  DRAWN_FACTOR of the usual chance. Exes rest EX_COOLDOWN_DAYS and a feuding pair never sparks.
- **The ladder.** LADDER, on both of their relationship rows: notices (a spark), banter
  (flirting), whispers (a crush), late_nights (dating), confession (together); ex after a
  breakup. A spark needs SPARK_AFFINITY both ways and seeds SPARK_ATTRACTION. Each warm scene of
  theirs adds FLIRT_GAIN attraction both ways and may take one step, once both rows meet that
  step's gate (GATES) and enough nights passed since the last one: his arc's pacing without his
  play days. A cold scene takes COOL_LOSS; short of dating, a romance below FIZZLE ends quietly.
- **Drawn away.** His pursuit is his deliberate attention at her (_pursuit, owner ruling
  2026-11-15): his flirts and compliments aimed at her and his whisper or DM lines to her,
  never raid or party time together nor his guild-chat replies. While he pursued her within
  PURSUIT_DAYS, her warm scenes with her partner add nothing on her side, and each night her
  pull toward her partner erodes by EROSION; once her pull to him outweighs it (MARGIN over her
  partner's pull plus HOLD), she leaves, so steady pursuit wins. From a crush on (BREAKS) that
  is a breakup: the split is public, the partner is hurt (memory, distress, capped affinity and
  trust loss), may open a feud with her (by his traits), and the guild hears it as a rumour
  about her. That she left for him is told (the partner's memory and envy, the feud cause, the
  rumour) only when some of his pursuit was public; otherwise only her own memory knows, private
  with him. Earlier stages just end. A woman whose stance toward him is none is never drawn
  away: his acts do not move her.

The nightly unit and the scene hook only write inside the caller's transaction, and only with
the night's switch on (pinned at mind start). Logs carry ids, counts and codes, never text.
"""
import json
import re
from dataclasses import replace

from leeroy_mind.discord.channels import is_public, private_sql
from leeroy_mind.logs import log
from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.nightly import knowledge
from leeroy_mind.nightly.attraction import reflection_row
from leeroy_mind.nightly.motives import trait_today
from leeroy_mind.nightly.relations import apply_capped
from leeroy_mind.storage.rows import ROMANCE_STAGES

STAGE = "couples"
LADDER = ("none", "notices", "banter", "whispers", "late_nights", "confession")
ACTIVE = LADDER[1:]
PARTNER = frozenset({"late_nights", "confession"})  # dating and together
BREAKS = frozenset({"whispers", "late_nights", "confession"})  # leaving one is a breakup
WORDS = {"notices": "noticing each other", "banter": "flirting", "whispers": "crushing on each other",
         "late_nights": "dating", "confession": "together", "ex": "broken up"}
# a couple as a relationship status on the console's cards: "dating <Name>", "with <Name>"
STATUS_WORDS = {"whispers": "dating", "late_nights": "dating", "confession": "with"}
INTENT = {
    "notices": "{a} and {b} have started to notice each other; there's a small spark.",
    "banter": "{a} and {b} flirt a little.",
    "whispers": "{a} has a crush on {b}, and it's mutual.",
    "late_nights": "{a} and {b} are dating and make time for each other.",
    "confession": "{a} and {b} are together; it's warm and easy between them.",
}
# The gate into each stage: (attraction, affinity, trust, nights since their last step), read
# on both of their rows.
GATES = {"banter": (28.0, 12.0, 0.0, 1.0), "whispers": (36.0, 15.0, 5.0, 2.0),
         "late_nights": (48.0, 20.0, 10.0, 2.0), "confession": (64.0, 30.0, 20.0, 3.0)}
SPARK_AFFINITY = 10.0
SPARK_ATTRACTION = 20.0
SPARK_P = 0.15
DRAWN_FACTOR = 0.15
SPARKS_PER_NIGHT = 2
FLIRT_WEIGHT = 0.85  # a couple's volition to spend the night's scene together
FLIRT_GAIN = 8.0
COOL_LOSS = 6.0
FIZZLE = 15.0
PURSUIT_DAYS = 3.0
# His deliberate attention at her (owner ruling 2026-11-15): his flirts at her (lines and romantic
# emotes) and compliments aimed at her, plus every whisper or DM line of his to her (_pursuit).
# Never raid or party time together (quest_together) nor his replies in guild chat (answered).
PURSUIT_ACTS = ("flirt", "public_compliment")
MARGIN = 10.0
HOLD = {"whispers": 0.0, "late_nights": 10.0, "confession": 20.0}
EROSION = 10.0  # her pull toward her partner, a night he pursues her: more than a scene adds
EX_COOLDOWN_DAYS = 30.0  # nights before two exes may spark again
BREAKUP_LOSS = -10.0  # the jilted partner's affinity and trust toward her, under the night cap
ENVY_SPIKE = 85.0
DAY_S = 86400.0
# Her stages toward him from which she is his, never a member's (romance._RIVAL_STAGES' range).
WITH_HIM = frozenset(ROMANCE_STAGES[ROMANCE_STAGES.index("banter"):ROMANCE_STAGES.index("ex")])
DRAWN = frozenset({"drawn", "lust"})
BREAKUP_ROOT = "rule:breakup:"  # a breakup's open root (the split itself is public)

_UNTAKEN = re.compile(
    r"\b(?:no|not|never|without)\s+(?:a\s+)?(?:married|spouse|partner|husband|wife|boyfriend|girlfriend)\b"
    r"|\bun-?married\b|\bex-?\s?(?:husband|wife|partner|boyfriend|girlfriend|fianc\w*)\b"
    r"|\b(?:late|former|dead|deceased|estranged|divorced|separated)\s+(?:husband|wife|partner|spouse)\b",
    re.IGNORECASE)
_TAKEN = re.compile(r"\b(?:married|husband|wife|spouse|partner|boyfriend|girlfriend|fianc[eé]e?)\b",
                    re.IGNORECASE)
# What else a household sentence says of her (the console's relationship status, owner
# 2026-11-15), read after _UNTAKEN has taken out "no partner", the exes and the late spouses.
_MARRIED = re.compile(r"\b(?:married|husband|wife|spouse)\b", re.IGNORECASE)
_ENGAGED = re.compile(r"\bfianc[eé]e?\b|\bengaged\b(?!\s+(?:in|with)\b)", re.IGNORECASE)
_NOT_ENGAGED = re.compile(r"\b(?:no|not|never|without)\s+(?:a\s+)?(?:engaged|fianc\w*)\b",
                          re.IGNORECASE)
_WIDOWED = re.compile(r"\bwidow(?:ed|er)?\b|\blate\s+(?:husband|wife|partner|spouse)\b",
                      re.IGNORECASE)
_DIVORCED = re.compile(r"\bdivorc\w*|\bex-?\s?(?:husband|wife|spouse)\b", re.IGNORECASE)
_SINGLE = re.compile(
    r"\bsingle\b(?!-|\s+(?:bed|room|storey|story|wide|family|level|car|garage)\b)"
    r"|\balone\b|\bsolo\b|\bun-?married\b"
    r"|\b(?:no|not|never|without)\s+(?:a\s+)?(?:married|spouse|partner|husband|wife|boyfriend|"
    r"girlfriend)\b", re.IGNORECASE)
TAKEN_STATUSES = frozenset({"married", "engaged", "partnered"})


def household_status(household: str | None) -> str | None:
    """What her household sentence says of her: "married", "engaged", "partnered" (a partner,
    boyfriend or girlfriend), "widowed", "divorced" or "single" (alone, single, no partner), in
    that order, so a remarried divorcee is married; None when it does not say."""
    text = household or ""
    rest = _UNTAKEN.sub(" ", _NOT_ENGAGED.sub(" ", text))
    if _TAKEN.search(rest) or _ENGAGED.search(rest):
        if _MARRIED.search(rest):
            return "married"
        return "engaged" if _ENGAGED.search(rest) else "partnered"
    if _WIDOWED.search(text):
        return "widowed"
    if _DIVORCED.search(text):
        return "divorced"
    return "single" if _SINGLE.search(text) else None


def taken(p) -> bool:
    """Her household names a spouse, a fiancé or a partner (as with the women married to
    outsiders): such a member never starts a member romance. "No partner", an ex and a late
    spouse do not count."""
    return household_status(p.real_life.household) in TAKEN_STATUSES


def _rel(store, a, b):
    return store.get_relationship(a, b)


def pair_stage(ab: str, ba: str) -> str:
    """A pair's member stage from their two rows' stages: the lower on LADDER, "ex" when either
    says ex, else "none"."""
    if "ex" in (ab, ba):
        return "ex"
    if ab in LADDER and ba in LADDER:
        return LADDER[min(LADDER.index(ab), LADDER.index(ba))]
    return "none"


def stage_of(store, a: int, b: int) -> str:
    """The pair's member stage (pair_stage of their two rows)."""
    return pair_stage(_rel(store, a, b).romance_stage, _rel(store, b, a).romance_stage)


def active(store, members, human: int) -> list[tuple[int, int, str]]:
    """Every member romance under way among `members`: (a, b, stage), a < b, both rows active."""
    members = set(members)
    with store.transaction():
        rows = store.conn.execute(
            f"SELECT a, b FROM relationship WHERE a < b AND a != ? AND b != ? "
            f"AND romance_stage IN ({','.join('?' * len(ACTIVE))})", (human, human, *ACTIVE)).fetchall()
    out = []
    for r in rows:
        if {r["a"], r["b"]} <= members:
            stage = stage_of(store, r["a"], r["b"])
            if stage in ACTIVE:
                out.append((r["a"], r["b"], stage))
    return sorted(out)


def partner(store, guid: int, human: int) -> tuple[int, str] | None:
    """The member `guid` is in a romance with now, and its stage."""
    with store.transaction():
        rows = store.conn.execute(
            f"SELECT b FROM relationship WHERE a = ? AND b != ? AND b != a "
            f"AND romance_stage IN ({','.join('?' * len(ACTIVE))}) ORDER BY b",
            (guid, human, *ACTIVE)).fetchall()
    for r in rows:
        stage = stage_of(store, guid, r["b"])
        if stage in ACTIVE:
            return r["b"], stage
    return None


def romance_of(store, guid: int, human: int) -> int | None:
    """The member `guid` is in a romance with, else the one they most recently broke up with."""
    found = partner(store, guid, human)
    if found is not None:
        return found[0]
    with store.transaction():
        row = store.conn.execute(
            "SELECT CASE WHEN woman=? THEN partner ELSE woman END AS other FROM breakup "
            "WHERE woman=? OR partner=? ORDER BY ts DESC, id DESC LIMIT 1",
            (guid, guid, guid)).fetchone()
    return None if row is None else row["other"]


def couple_names(ctx, a: int, b: int) -> dict[int, str] | None:
    """{guid: name} when the two are a member romance under way and the switch is on: their
    own scene may then be romantic about the two of them (ground.check's rule 7)."""
    from leeroy_mind.drama.config import member_romance
    if not member_romance(ctx) or stage_of(ctx.store, a, b) not in ACTIVE:
        return None
    return {a: ctx.name(a), b: ctx.name(b)}


def _gender(ctx, guid):
    return ctx.persona(guid).identity.player_gender


def _with_him(store, guid, human) -> bool:
    return _rel(store, guid, human).romance_stage in WITH_HIM


def free(ctx, guid: int) -> bool:
    """Whether `guid` may start a member romance tonight."""
    return (not taken(ctx.persona(guid)) and partner(ctx.store, guid, ctx.human) is None
            and not _with_him(ctx.store, guid, ctx.human))


def _set_stage(store, a, b, old, new, evidence, now):
    up = old in LADDER and new in LADDER and LADDER.index(new) > LADDER.index(old)
    for x, y in ((a, b), (b, a)):
        store.put_relationship(replace(_rel(store, x, y), romance_stage=new))
        store.add_rel_delta(x, y, "romance_stage", 1 if up else -1, f"member:{old}->{new}",
                            list(evidence), now, applied=True)


def _attraction(store, a, b, amount, reason, evidence, now):
    """Member attraction moves only here, by their scenes and his pursuit, both outside the
    night cap like his curve; logged as an applied rel_delta."""
    rel = _rel(store, a, b)
    new = max(0.0, min(100.0, rel.attraction + amount))
    if new != rel.attraction:
        store.put_relationship(replace(rel, attraction=new))
        store.add_rel_delta(a, b, "attraction", new - rel.attraction, reason, list(evidence), now,
                            applied=True)


def _last_step(store, a, b) -> float | None:
    return store.conn.execute(
        "SELECT max(ts) FROM rel_delta WHERE a=? AND b=? AND dim='romance_stage' "
        "AND reason LIKE 'member:%'", (a, b)).fetchone()[0]


def can_step(store, a, b, stage, now) -> str | None:
    """The next stage the pair enters tonight, or None (gates on both rows, and pace)."""
    if stage not in ACTIVE or stage == LADDER[-1]:
        return None
    target = LADDER[LADDER.index(stage) + 1]
    attraction, affinity, trust, nights = GATES[target]
    last = _last_step(store, a, b)
    if last is not None and (now - last) / DAY_S < nights:
        return None
    for x, y in ((a, b), (b, a)):
        r = _rel(store, x, y)
        if r.attraction < attraction or r.affinity < affinity or r.trust < trust:
            return None
    return target


def after_scene(store, ctx, enc, *, total: float, valence: float) -> str | None:
    """A couple's accepted scene, in its encounter's transaction: warm scenes draw them closer
    and may take one step; cold ones cool them. Returns the outcome code, or None."""
    from leeroy_mind.drama.config import member_romance
    if not member_romance(ctx):
        return None
    a, b = enc["a"], enc["b"]
    if ctx.human in (a, b):
        return None
    stage = stage_of(store, a, b)
    if stage not in ACTIVE:
        return None
    now, ref = ctx.clock.now(), [f"encounter:{enc['id']}"]
    if total >= 0 and valence >= 0:
        # While he pursues her, her warm scenes with her partner add nothing on her side and
        # take no step: his romance wins if he keeps at it.
        woman = next((g for g in (a, b) if _gender(ctx, g) == "female"), None)
        drawn = woman is not None and bool(_pursuit(store, woman, ctx.human, now))
        for x, y in ((a, b), (b, a)):
            if not (drawn and x == woman):
                _attraction(store, x, y, FLIRT_GAIN, "member:scene", ref, now)
        if drawn:
            return "distracted"
        new = can_step(store, a, b, stage, now)
        if new is None:
            return "warmer"
        _set_stage(store, a, b, stage, new, ref, now)
        log("member_romance_step", a=min(a, b), b=max(a, b), stage=new)
        return "step"
    if total < 0 or valence < -0.2:
        for x, y in ((a, b), (b, a)):
            _attraction(store, x, y, -COOL_LOSS, "member:scene", ref, now)
        if (stage not in PARTNER and stage != "whispers"
                and min(_rel(store, a, b).attraction, _rel(store, b, a).attraction) < FIZZLE):
            _set_stage(store, a, b, stage, "none", ref, now)
            log("member_romance_fizzled", a=min(a, b), b=max(a, b), stage=stage)
            return "fizzled"
        return "cooler"
    return None


# -- the night's unit ------------------------------------------------------------------------

def _public_ref(store, ref: str) -> bool:
    """Whether an act of his was public: a line on a channel the whole guild may read (guild,
    party, raid, say, yell, a public Discord channel) or a text emote in the game. Anything
    else (a whisper, a DM, an officers' or unknown line) is not."""
    if ref.startswith("event:"):
        row = store.conn.execute("SELECT type FROM game_event WHERE id=?",
                                 (int(ref[6:]) if ref[6:].isdecimal() else -1,)).fetchone()
        return row is not None and row["type"] == "text_emote"
    if ref.startswith("chat:") and ref[5:].isdecimal():
        row = store.conn.execute("SELECT channel FROM chat_line WHERE id=?", (int(ref[5:]),)).fetchone()
        return row is not None and is_public(row["channel"])
    return False


def _pursuit(store, woman, human, now) -> list[tuple[str, bool]]:
    """His pursuit of her in the last PURSUIT_DAYS, oldest first: (ref, public). His flirt and
    compliment acts at her (attraction_act), and his whisper or DM lines to her."""
    since, out = now - PURSUIT_DAYS * DAY_S, {}
    for r in store.conn.execute(
            f"SELECT ref FROM attraction_act WHERE guid=? AND ts>? AND ts<=? "
            f"AND act IN ({','.join('?' * len(PURSUIT_ACTS))}) ORDER BY ts, id",
            (woman, since, now, *PURSUIT_ACTS)):
        out.setdefault(r["ref"], _public_ref(store, r["ref"]))
    for r in store.conn.execute(
            f"SELECT id FROM chat_line WHERE from_guid=? AND to_guid=? AND origin='human' "
            f"AND {private_sql()} AND ts>? AND ts<=? ORDER BY ts, id", (human, woman, since, now)):
        out.setdefault(f"chat:{r['id']}", False)
    return list(out.items())


def _feud_chance(ctx, guid) -> float:
    traits = trait_today(ctx.store, ctx.night, guid, ctx.persona(guid))
    return max(0.1, min(0.8, 0.35 + 0.06 * (traits["neuroticism"] - 5)
                        - 0.06 * (traits["agreeableness"] - 5)))


def _envy(store, holder, human, evidence, now):
    row = store.conn.execute("SELECT level FROM jealousy WHERE holder=? AND target=?",
                             (holder, human)).fetchone()
    level = max(ENVY_SPIKE, float(row["level"]) if row else 0.0)
    store.conn.execute(
        "INSERT INTO jealousy(holder,target,kind,level,updated,evidence) VALUES (?,?,'envy',?,?,?) "
        "ON CONFLICT(holder,target) DO UPDATE SET kind='envy',level=excluded.level,"
        "updated=excluded.updated,evidence=excluded.evidence",
        (holder, human, level, now, json.dumps(list(evidence)[:3])))


def _rumour(store, ctx, ident, woman, jilted, source, canonical, now) -> int | None:
    """The guild hears it: a live rumour about her whose first spreader is the jilted partner.
    A breakup is one event: it neither waits for a live-rumour slot nor fills one
    (gossip.organic_rumors)."""
    root = f"{BREAKUP_ROOT}{ident}"
    if store.conn.execute("SELECT 1 FROM rumor WHERE root=?", (root,)).fetchone():
        return None
    rumor = store.conn.execute(
        "INSERT INTO rumor(root,about,canonical,source,created,last_told) VALUES (?,?,?,?,?,?)",
        (root, woman, canonical, source, now, now)).lastrowid
    version = store.conn.execute(
        "INSERT INTO rumor_version(rumor,teller,hearer,op,text,ts,source) "
        "VALUES (?,?,?,'level',?,?,?)", (rumor, jilted, jilted, canonical, now, source)).lastrowid
    store.conn.execute("INSERT INTO rumor_state(rumor,guid,state,tells,version,shift,updated) "
                       "VALUES (?,?,'spreader',0,?,0,?)", (rumor, jilted, version, now))
    return rumor


def breakup(store, ctx, woman: int, jilted: int, stage: str, now: float, *,
            pursuit=()) -> int:
    """She leaves `jilted` for him, inside the night unit's transaction. Returns the breakup id.

    The split is public. Why she left (him) is public only when some of his `pursuit` (ref,
    public) was (owner ruling 2026-11-15): then the partner's hurt memory, his feelings, the
    feud cause and the rumour name him, and the partner envies him. When all of it was in
    whispers or DMs, they say only that she left, and her own memory of leaving him for the
    owner is private with the owner, citing those lines."""
    from leeroy_mind.nightly import feuds
    human = ctx.human
    public = any(open_ for _, open_ in pursuit)
    ident = store.conn.execute(
        "INSERT INTO breakup(woman,partner,for_guid,stage,night,ts,public) VALUES (?,?,?,?,?,?,?)",
        (woman, jilted, human, stage, ctx.night, now, int(public))).lastrowid
    root = f"{BREAKUP_ROOT}{ident}"
    _set_stage(store, woman, jilted, stage, "ex", [root], now)
    for g, how in ((woman, "witnessed"), (jilted, "witnessed")):
        knowledge.learn(store, root, g, how, ts=now)
    her, his, him = ctx.name(woman), ctx.name(jilted), ctx.ident.called
    if public:
        hurt_text, hurt_actors = f"{her} left me for {him}.", [woman, human]
        canonical, cause = f"{her} left {his} for {him}.", f"{her} left {his} for {him}"
    else:
        hurt_text, hurt_actors = f"{her} has been distant. I think there's someone else.", [woman]
        canonical, cause = f"{her} and {his} split up.", f"{her} and {his} split up"
    hurt = ctx.writer.put(reflection_row(
        jilted, clean_memory_text(hurt_text, ctx.ident), now=now, actors=hurt_actors,
        evidence=[root], key=f"breakup:{ident}", importance=8, valence=-0.8))
    left = reflection_row(
        woman, clean_memory_text(f"Left {his} for {him}.", ctx.ident), now=now,
        actors=[jilted, human], evidence=[root] if public else [root, *(r for r, _ in pursuit)],
        key=f"breakup:{ident}", importance=7, valence=0.3)
    ctx.writer.put(left if public else replace(left, scope="private", scope_with=human))
    for dim in ("affinity", "trust"):
        apply_capped(store, night=ctx.night, a=jilted, b=woman, dim=dim, delta=BREAKUP_LOSS,
                     speed=ctx.settings.relationship_speed, reason="breakup", evidence=(root,),
                     ts=now, human=human)
    felt = ((woman, "distress", 3), (human, "resentment", 2)) if public else ((woman, "distress", 3),)
    for target, kind, intensity in felt:
        store.conn.execute(
            "INSERT INTO emotion(guid,night,type,target,intensity,why,source,evidence,ts) "
            "VALUES (?,?,?,?,?,?,'breakup',?,?)",
            (jilted, ctx.night, kind, target, intensity, "breakup", json.dumps([f"memory:{hurt}"]), now))
    if public:
        _envy(store, jilted, human, [f"memory:{hurt}"], now)
    rumor = _rumour(store, ctx, ident, woman, jilted, hurt,
                    clean_memory_text(canonical, ctx.ident), now)
    feud = None
    if ctx.rng(STAGE, "feud", ident).random() < _feud_chance(ctx, jilted):
        feud = feuds.open_drama(store, ctx, jilted, woman, root=root, cause=cause)
    store.conn.execute("UPDATE breakup SET feud=?, rumor=? WHERE id=?", (feud, rumor, ident))
    log("member_breakup", id=ident, woman=woman, partner=jilted, stage=stage,
        public=int(public), feud=feud or 0, rumor=rumor or 0)
    return ident


def _draw_away(store, ctx, members, now) -> tuple[int, int]:
    """His pursuit against each woman's member partner. Returns (breakups, quiet ends)."""
    breakups = quiet = 0
    for a, b, stage in active(store, members, ctx.human):
        woman = next((g for g in (a, b) if _gender(ctx, g) == "female"), None)
        if woman is None:
            continue
        other = b if woman == a else a
        acts = _pursuit(store, woman, ctx.human, now)
        if not acts:
            continue
        _attraction(store, woman, other, -EROSION, "member:drawn_away",
                    [r for r, _ in acts][:3], now)
        # Her pull to him against her pull to her partner (and how far the two have come).
        pull = _rel(store, woman, ctx.human).attraction
        held = _rel(store, woman, other).attraction + HOLD.get(stage, 0.0)
        if pull < held + MARGIN:
            continue
        if stage in BREAKS:
            breakup(store, ctx, woman, other, stage, now, pursuit=acts)
            breakups += 1
        else:
            _set_stage(store, woman, other, stage, "none", [f"rule:drawn:{woman}"], now)
            quiet += 1
    return breakups, quiet


def _sparks(store, ctx, members) -> int:
    attitudes = ctx.attitudes()
    eligible = sorted(g for g in members if free(ctx, g))
    women = [g for g in eligible if _gender(ctx, g) == "female"]
    men = [g for g in eligible if _gender(ctx, g) == "male"]
    now = ctx.clock.now()
    # Exes rest EX_COOLDOWN_DAYS; a feuding pair never sparks.
    barred = {frozenset((r["woman"], r["partner"])) for r in store.conn.execute(
        "SELECT woman,partner FROM breakup WHERE ts>?", (now - EX_COOLDOWN_DAYS * DAY_S,))}
    barred |= {frozenset((r["a"], r["b"])) for r in store.conn.execute(
        "SELECT a,b FROM feud WHERE ended IS NULL")}
    pairs = []
    for w in women:
        for m in men:
            if frozenset((w, m)) in barred:
                continue
            low = min(_rel(store, w, m).affinity, _rel(store, m, w).affinity)
            if low >= SPARK_AFFINITY:
                pairs.append((-low, w, m))
    used, sparks = set(), 0
    for _, w, m in sorted(pairs):
        if sparks >= SPARKS_PER_NIGHT:
            break
        if w in used or m in used:
            continue
        chance = SPARK_P * (DRAWN_FACTOR if attitudes.get(w) in DRAWN else 1.0)
        if ctx.rng(STAGE, "spark", w, m).random() >= chance:
            continue
        for x, y in ((w, m), (m, w)):
            rel = _rel(store, x, y)
            if rel.attraction < SPARK_ATTRACTION:
                _attraction(store, x, y, SPARK_ATTRACTION - rel.attraction, "member:spark",
                            [f"rule:spark:{min(w, m)}:{max(w, m)}"], now)
        _set_stage(store, w, m, _rel(store, w, m).romance_stage, "notices",
                   [f"rule:spark:{min(w, m)}:{max(w, m)}"], now)
        used.update((w, m))
        sparks += 1
    return sparks


async def stage_couples(ctx) -> None:
    """The night's deterministic unit: his pull against each partner first, then new sparks.
    Nothing at all while the night's switch is off."""
    from leeroy_mind.drama.config import member_romance
    if not member_romance(ctx) or ctx.done(STAGE, "all"):
        return

    def write(store):
        members = ctx.members()
        now = ctx.clock.now()
        breakups, quiet = _draw_away(store, ctx, members, now)
        sparks = _sparks(store, ctx, members)
        log("member_romance_night", couples=len(active(store, members, ctx.human)),
            sparks=sparks, breakups=breakups, drifted=quiet)

    ctx.commit(STAGE, "all", write)


def attach(app):
    """Startup only: with the switch off nothing is added."""
    from leeroy_mind.drama.config import load_drama_config
    if not load_drama_config(app.data_dir / "mind.toml").member_romance:
        return None
    app.night_runner.add_stage(STAGE, stage_couples, after="romance", model=None)
    return True
