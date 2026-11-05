"""Deterministic training callouts."""

TEMPLATES = {
    "new_plan": "new plan on {boss}, tested it {when}: {call}",
    "old_plan_ok": "ok, old plan on {boss}",
    "offline_summary": "training today: {what}",
    "lust_now": "lust now",
}


def template(key, **facts):
    return TEMPLATES[key].format(**facts)
