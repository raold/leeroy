"""Every built-in arc string, for the scan that keeps longing from turning into reproach (the
no-reproach anti-goal). Gathered from the modules' own constants, never copied: the plan lines
(arc.plan, rendered for every kind, state, region and cover story with his pronoun forms left
as the fields {he}, {him} and {his}), the read's prompt (arc.night), the weekend writer's prompt
and plain memories (arc.weekend, the plain ones rendered for every plan kind and region), and
the milestones in both cycles' words, the affair's endings and her separated household
(arc.after), the noticed facts (arc.night), and live talk's partner and event lines (arc.live)
with the cooling variants her whisper reads (talk.romance.COOLING_TEXT). Other fields
({called}, {name}, {like}, {nth}, {them}, ...) stay for the scan to fill."""
import itertools

from leeroy_mind.arc import after, live, night, plan, weekend
from leeroy_mind.talk.romance import COOLING_TEXT

FORMS = ("{he}", "{him}", "{his}")       # his pronoun forms, left for the scan to fill
_NOW = 0.0
_DAY = 86400.0


def _plan_texts() -> list[str]:
    out = []
    for region, kind, cover, state, days in itertools.product(
            ("UK", "US"), plan.PLAN_KINDS, ("", *plan._COVERS), ("hoped", "talked", "set"),
            (12, 1, -1)):
        p = {"kind": kind, "target": _NOW + days * _DAY, "worries": list(plan._WORRIES),
             "cover": cover}
        out += plan.plan_lines(p, state, now=_NOW, forms=FORMS, region=region)
        out += [plan.plan_summary(p, region), plan.plan_planned(p, region)]
    return out


def _weekend_texts() -> list[str]:
    plain = [weekend.plain_text(kind, plan_kind, region, "{called}")
             for kind, plan_kind, region in itertools.product(
                 weekend.MOMENTS, plan.PLAN_KINDS, ("UK", "US"))]
    return [weekend.WEEKEND_PROMPT, weekend.AFFAIR, weekend.WEEKEND_ASK, weekend.PLAIN_WEEKEND,
            weekend.PLAIN_VISIT, *plain, *weekend.MOMENTS_TEXT.values(), *weekend.TONES]


def arc_texts() -> tuple[str, ...]:
    """Every built-in arc string once, in module order."""
    return tuple(dict.fromkeys([
        *_plan_texts(),
        night.READ_PROMPT, night.READ_ASK, night.NO_PLAN,
        *_weekend_texts(),
        *after.MILESTONE_TEXT.values(), *after.MILESTONE_AGAIN.values(), after.LIKE_FALLBACK,
        *after.FOUND_TEXT.values(),
        after.HOUSEHOLD_SEPARATED, after.HOUSEHOLD_SEPARATED_KIDS,
        *night.NOTICED_TEXT.values(),
        *live.PARTNER_TEXT.values(), *live.EVENT_TEXT.values(), *COOLING_TEXT,
    ]))
