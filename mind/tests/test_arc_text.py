"""Every built-in arc string passes the romance's no-reproach scan (talk.romance.REPROACH_RE,
B3): plans, the read, the weekend, milestones, the affair's endings, the noticed facts, and live
talk's cooling, partner and event lines. Longing never turns into keeping score or a summons."""
from leeroy_mind.arc import after, live, night, weekend
from leeroy_mind.arc.texts import arc_texts
from leeroy_mind.human import THEY_FORMS, _FORMS
from leeroy_mind.talk.romance import COOLING_TEXT, REPROACH_RE


def fill(s: str, forms) -> str:
    """A built-in arc string with its fields filled: his called name and pronoun forms (as
    {he}/{him}/{his} and as {they}/{them}/{their}), her name, a like from her card; every other
    field empty."""
    class Fields(dict):
        def __missing__(self, key):
            return ""
    return s.format_map(Fields(called="Testy", name="Arkon", he=forms[0], him=forms[1],
                               his=forms[2], they=forms[0], them=forms[1], their=forms[2],
                               like="quiet runs", nth="second", cover="a work trip",
                               where="at a hotel halfway between us"))


def test_arc_texts_never_reproach():
    texts = arc_texts()
    for s in (*after.MILESTONE_TEXT.values(), *after.MILESTONE_AGAIN.values(),
              *after.FOUND_TEXT.values(), after.HOUSEHOLD_SEPARATED,
              after.HOUSEHOLD_SEPARATED_KIDS, weekend.PLAIN_WEEKEND, weekend.PLAIN_VISIT,
              weekend.AFFAIR, weekend.WEEKEND_PROMPT, *night.NOTICED_TEXT.values(),
              *COOLING_TEXT, *live.PARTNER_TEXT.values(), *live.EVENT_TEXT.values()):
        assert s in texts, s
    assert "It's happening: BlizzCon, about 12 days off." in texts      # the plan lines
    assert any("you leave it with {him}" in s for s in texts)
    hits = [(s, f) for f in (_FORMS["he"], _FORMS["she"], THEY_FORMS) for s in texts
            if REPROACH_RE.search(fill(s, f))]
    assert hits == []
    assert len(texts) == len(set(texts))


def test_the_scan_catches_what_it_is_for():
    for bad in ("you never push it", "where were you last night", "i'm still waiting",
                "come over already", "you forgot again"):
        assert REPROACH_RE.search(bad), bad
