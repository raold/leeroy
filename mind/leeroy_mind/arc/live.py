"""The arc in live talk (Task S8): what of her arc a turn of hers may show, and who is away.

arc_view reads one woman's arc for one turn. Everything private in it (her plan's lines, the
trouble at home, the arc's recent events as scene items, the rival who has got closer to him and
a cooling spark) is filled only for a whisper to him or a Discord DM to him, which is private
exactly as a whisper (discord.channels.is_private); any other turn gets her stage and plan
state alone, and her romance text shows only the stage's passage there (whisper scope). Her plan
speaks only while it is live (hoped, talked, set) at "planning" or, for a visit, at "after", and
only for a woman on the full arc. A summons stands in her whisper or DM to him only inside a plan
he has talked about or said yes to at "planning" (summons_ok, read by the pipeline's summons
check).

One whisper carries at most ARC_CAP_TOKENS of her arc's lines (her plan, home, the rival line
and a cooling variant), so the live prompt's budget holds: where her plan stands always, then
the rest in an order drawn for the turn, each as long as it fits. What does not fit waits for
another whisper, and no whisper retells the whole story.

away_now is every member on an open away row, whoever wrote it: the pipeline never voices them.

The texts are templates over {name} (his called name), {they}, {them} and {their} (his forms),
filled by talk.romance as the scene notes are. Built-in, non-explicit, naming no place; her guilt
about home is hers and never turned on him (arc.texts scans them). Nothing here logs."""
import random
from dataclasses import dataclass

from leeroy_mind.arc.ladder import FULL_ARC, NOTICED
from leeroy_mind.arc.plan import has_partner, plan_lines
from leeroy_mind.arc.state import get_arc
from leeroy_mind.discord.channels import is_private
from leeroy_mind.personas.schema import Persona
from leeroy_mind.storage.rows import ROMANCE_STAGES
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.romance import COOLING_TEXT, rival_text
from leeroy_mind.text import estimate_tokens

AWAY_CACHE_S = 60.0             # the pipeline reads away_now at most this often
ARC_CAP_TOKENS = 54             # her arc's lines in one whisper, estimated, at most
RIVAL_LINE_LEVEL = 40           # her rivalry level that makes the rival line
EVENT_RECENT_S = 7 * 86400.0    # an arc event this recent is a scene item
PLAN_STAGES = ("planning", "after")   # a plan for the weekend, or for a visit after it
LIVE_PLANS = ("hoped", "talked", "set")
SUMMONS_PLANS = ("talked", "set")     # he has engaged with it
HOME_STAGES = ROMANCE_STAGES[ROMANCE_STAGES.index("late_nights"):ROMANCE_STAGES.index("ex")]
# The trouble at home: a partner who doesn't know (plan.has_partner), or the end of that
# relationship once the affair came out (arc.after). Hers to carry, never his.
PARTNER_TEXT = {
    "together": "There's a partner at home who doesn't know about {name}: your secret, never "
                "{their} burden.",
    "separated": "Things at home are over now: that part is yours to carry, never {their} "
                 "burden.",
}
# The after-weekend milestones (arc.after.MILESTONES) as scene items, second person like
# talk.romance.NOTE_TEXT. "found_out" ends it (an ex shows none) and "separated" is the partner
# line's.
EVENT_TEXT = {
    "missing": "you've missed {them} more since you met",
    "inside_joke": "you two share a joke from your time together",
    "month": "it's a month since you last saw {them}",
    "gift": "you sent {them} a little something by post",
    "friends_on_voice": "{they} met your friends on voice",
    "fight_and_make_up": "you two had a silly argument and made up",
    "next_visit": "you floated another trip",
}


@dataclass(frozen=True)
class ArcView:
    """One turn's view of her arc: her stage and plan state, and for a whisper or DM to him only the
    arc lines (her plan, home), the scene items (recent arc events, newest first), the rival's
    name, whether her spark is cooling and whether a summons may stand."""
    stage: str
    plan_state: str
    arc_lines: tuple[str, ...] = ()
    scene: tuple[str, ...] = ()
    rival: str = ""
    cooling: bool = False
    summons_ok: bool = False


def away_now(store: Store, now: float) -> frozenset[int]:
    """Every member on an open away row (any owner's) who is not due back yet: returned_at
    unset, and returns_at in the future or not set."""
    with store.transaction():
        rows = store.conn.execute(
            "SELECT guid FROM away WHERE returned_at IS NULL "
            "AND (returns_at IS NULL OR returns_at > ?)", (now,)).fetchall()
    return frozenset(r[0] for r in rows)


def arc_view(store: Store, p: Persona, human: int, *, now: float, channel: str, to_guid: int,
             forms: tuple[str, str, str], names: dict[int, str],
             rng: random.Random | None = None) -> ArcView:
    """Her arc for a turn of hers on `channel` (a whisper going to `to_guid`). `forms` are his
    pronoun forms; `names` the cast's names (his called name among them), for the rival's and
    for measuring the lines. `rng` draws the order the lines after her plan's first are tried
    in (ARC_CAP_TOKENS); without it they go in order: the plan's second line, home, the rival,
    cooling. An ex, and every turn but a whisper or DM to him, gets her stage and plan state
    alone."""
    stage = store.get_relationship(p.guid, human).romance_stage
    arc = get_arc(store, p.guid, human)
    if not (is_private(channel) and to_guid == human) or stage == "ex":
        return ArcView(stage, arc.plan_state)
    full = p.romance.stance in FULL_ARC
    plan = (plan_lines(arc.plan, arc.plan_state, now=now, forms=forms, region=p.identity.region)
            if full and stage in PLAN_STAGES and arc.plan_state in LIVE_PLANS
            and arc.plan.get("kind") else [])
    home = ""
    if stage in HOME_STAGES:
        home = (PARTNER_TEXT["separated"] if arc.partner == "separated"
                else PARTNER_TEXT["together"] if has_partner(p, arc.partner) else "")
    lines, rival, cooling = _fit(plan, home, _rival(store, p.guid, human, names),
                                 _cooling(store, p.guid), forms, names.get(human, ""), rng)
    scene = tuple(EVENT_TEXT[kind] for kind, ts in sorted(arc.events, key=lambda e: -e[1])
                  if kind in EVENT_TEXT and now - EVENT_RECENT_S <= ts <= now)
    return ArcView(stage, arc.plan_state, lines, scene, rival, cooling,
                   full and stage == "planning" and arc.plan_state in SUMMONS_PLANS)


def _fit(plan: list[str], home: str, rival: str, cooling: bool, forms: tuple[str, str, str],
         called: str, rng: random.Random | None) -> tuple[tuple[str, ...], str, bool]:
    """Her arc's lines within ARC_CAP_TOKENS: her plan's first line, then the others in `rng`'s
    order (drawn only when there are two or more), each kept while it fits. The kept lines in
    their own order, the rival's name and whether a cooling variant goes in (counted at the
    longest variant)."""
    fill = {"name": called, "they": forms[0], "them": forms[1], "their": forms[2]}

    def cost(text: str) -> int:
        for k, v in fill.items():
            text = text.replace("{" + k + "}", v)
        return estimate_tokens(text)
    rest = [("line", x) for x in (*plan[1:], home) if x]
    if rival:
        rest.append(("rival", rival))
    if cooling:
        rest.append(("cooling", ""))
    if rng is not None and len(rest) > 1:
        rest = rng.sample(rest, len(rest))
    used = sum(cost(x) for x in plan[:1])
    kept, shown_rival, shown_cooling = set(plan[:1]), "", False
    for kind, x in rest:
        need = (cost(rival_text(x, forms)) if kind == "rival"
                else max(cost(v) for v in COOLING_TEXT) if kind == "cooling" else cost(x))
        if used + need > ARC_CAP_TOKENS:
            continue
        used += need
        if kind == "rival":
            shown_rival = x
        elif kind == "cooling":
            shown_cooling = True
        else:
            kept.add(x)
    return tuple(x for x in (*plan, home) if x in kept), shown_rival, shown_cooling


def _rival(store: Store, guid: int, human: int, names: dict[int, str]) -> str:
    """The woman she minds most (her strongest rivalry row at RIVAL_LINE_LEVEL or more) whose
    own arc with him the guild can tell (a NOTICED stage), or ""."""
    from leeroy_mind.tuning.edits import feeling_overlay
    with store.transaction():
        rows = store.conn.execute(
            "SELECT j.target, r.romance_stage FROM jealousy j JOIN relationship r "
            "ON r.a = j.target AND r.b = ? "
            "WHERE j.holder = ? AND j.kind = 'rivalry' AND j.level >= ? AND j.target != ? "
            "ORDER BY j.level DESC, j.target", (human, guid, RIVAL_LINE_LEVEL, human)).fetchall()
        lens = feeling_overlay(store.conn)  # her stage as the guild sees it (godmode lens)
    return next((names[r[0]] for r in rows if names.get(r[0])
                 and lens.get((r[0], human), {}).get("romance_stage", r[1]) in NOTICED), "")


def _cooling(store: Store, guid: int) -> bool:
    """Her last night's attraction (attraction_log) was neglected or lost more than it gained."""
    with store.transaction():
        row = store.conn.execute(
            "SELECT neglected, gained, decayed FROM attraction_log WHERE guid = ? "
            "ORDER BY night DESC LIMIT 1", (guid,)).fetchone()
    return row is not None and (bool(row[0]) or row[2] > row[1])

