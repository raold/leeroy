"""Each member's night of reflection (P1c Task 11): the digest, the questions it raises, the
evidence, one reasoning pass and one JSON pass, grounded outputs (beliefs, self-insights,
relationship moves, goals, theory of mind, a nickname, feelings and the diary) and one commit.
Every output takes the scope of what it rests on, and of what the prompt showed when the judge
does not hold it to its citations (controller ruling B2; pre-flight PR7). Fakes only: a FakeLLM
answers for the reflection model. Every name and line here is invented."""
import asyncio
import json
import logging
import re
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.memory.privacy import stricter
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.knowledge import knowers, learn, stage_knows
from leeroy_mind.nightly.llm import check_schema
from leeroy_mind.nightly.reflect import (EVIDENCE_CHARS, GOAL_QUESTIONS, OCC, PRIVATE_OVERLAP,
                                         QUESTIONS_SCHEMA, REFLECTION_SCHEMA, build_digest,
                                         reflect_member, stage_reflect)
from leeroy_mind.nightly.store import ledger_rows
from leeroy_mind.text import estimate_tokens
from nightly.conftest import no_real_name, yes

DAY = 86400.0
RUN = 5 * 3600.0                      # the night's run: 02:00, five hours after h.T (21:00)
Q = {"questions": ["q1", "q2", "q3"]}
_TITLE = re.compile(r'"title":\s*"([^"]+)"')
_ALIAS = re.compile(r"\[(\d+)\] \(")


# -- test-local helpers --------------------------------------------------------------------------

def seed_day(h, owner: int = 101) -> dict[str, int]:
    """The brief's day: five memories of `owner` (Arkon) in tonight's window, aliased in this
    order by the digest. [1] "Belra held Sindragosa for me." (actors (102,), importance 6) rests
    on Belra's raid line, which names Coros; [2] "Testy pulled us through the last pull."
    (actors (1,)) on his raid line, and puts him among digest.people; [3]-[5] are generic and
    name nobody. The clock is left at the night's run (02:00). Returns the line and memory ids."""
    hold = h.say(h.T - 3600, 102, "coros keep dps on her, i got sindragosa", channel="raid")
    pull = h.say(h.T - 3000, 1, "nice one all, that last pull was close", channel="raid")
    ids = {"hold": hold, "pull": pull,
           "belra": h.mem(owner, "Belra held Sindragosa for me.", actors=(102,), importance=6,
                          evidence=(f"chat:{hold}",), t=h.T - 3000),
           "testy": h.mem(owner, "Testy pulled us through the last pull.", actors=(1,),
                          evidence=(f"chat:{pull}",), t=h.T - 2400),
           "flasks": h.mem(owner, "Repaired and restocked flasks before the raid.", t=h.T - 7200),
           "bags": h.mem(owner, "Sorted my bags after the raid.", t=h.T - 7800),
           "tea": h.mem(owner, "Made tea between the pulls.", t=h.T - 8400)}
    h.clock.set(h.T + RUN)
    return ids


def by_title(**replies):
    """A scripted default that answers a call by its schema's title: each reply a dict, or a
    callable given the prompt."""
    def answer(text: str) -> dict:
        title = _TITLE.search(text.rsplit("matching this schema:", 1)[-1]).group(1)
        reply = replies[title]
        return reply(text) if callable(reply) else reply
    return answer


DIARY = {"text": "Long night, good kill.", "evidence": [2]}
generic_reflection = by_title(judge=yes, reflection={"outputs": [], "emotions": [], "diary": {**DIARY, "evidence": [1]}})


def alias_of(text: str, needle: str) -> int:
    """The alias of the evidence line in a prompt that holds `needle`."""
    for line in text.splitlines():
        m = _ALIAS.match(line)
        if m and needle in line:
            return int(m.group(1))
    raise AssertionError(f"no evidence line holds {needle!r}")


def out(kind, about, text, evidence, *, dim=None, delta=None, valence=0.0) -> dict:
    return {"kind": kind, "about": about, "text": text, "dim": dim, "delta": delta, "valence": valence,
            "evidence": evidence}


def reflection(outputs=(), emotions=(), diary=DIARY) -> dict:
    return {"outputs": list(outputs), "emotions": list(emotions), "diary": diary}


def think_prompt(ctx) -> tuple[str, str]:
    (call,) = [c for c in ctx.reasoner.llm.calls if c.get("think")]
    return call["messages"][0]["content"], call["messages"][-1]["content"]


def calls_titled(ctx, title: str) -> list[dict]:
    return [c for c in ctx.reasoner.llm.calls if (c.get("format") or {}).get("title") == title]


def grounding(h, kind: str | None = None) -> list[tuple[str, str, str, str]]:
    return [tuple(r) for r in h.store.conn.execute(
        "SELECT kind, text, verdict, reason FROM grounding_log WHERE stage = 'reflect'"
        + ("" if kind is None else " AND kind = ?") + " ORDER BY id", () if kind is None else (kind,))]


def reflections(h, owner: int = 101):
    return [m for m in h.rows(owner) if m.source == "reflection"]


def judge_by(partly=()):
    """The judge: "partly" for the items whose text is in `partly`, "yes" for every other."""
    def verdicts(prompt: str) -> dict:
        items = re.findall(r"ITEM (\d+): \[\w+\] (.*)", prompt)
        return {"verdicts": [{"i": int(i), "support": "partly" if text in partly else "yes",
                              "invariants_ok": True, "why": ""} for i, text in items]}
    return verdicts


# -- the plan's tests ----------------------------------------------------------------------------

def test_digest_window_and_top_n(h):
    for i in range(70): h.mem(101, f"Ran a dungeon, take {i}.", importance=i % 10 + 1)
    h.mem(101, "Old thing.", t=h.T - 3 * 86400, importance=10)
    d = build_digest(h.store, 101, h.ctx().window)
    assert len(d.memories) == 60 and d.memories[0].importance == 10 and "Old thing." not in [m.text for m in d.memories]
    assert build_digest(h.store, 102, h.ctx().window) is None


def test_reflect_member_writes_grounded_outputs(h):
    seed_day(h)
    ctx = h.ctx({
        "questions": [{"questions": ["q1", "q2", "q3"]}],
        "reflection": [{"outputs": [
            {"kind": "belief", "about": "Belra", "text": "Belra is steady under pressure.", "dim": None, "delta": None, "valence": 0.6, "evidence": [1]},
            {"kind": "rel_delta", "about": "Belra", "text": "She saved me.", "dim": "trust", "delta": 4, "valence": 0.5, "evidence": [1]},
            {"kind": "plan", "about": "Belra", "text": "Ask Belra how she kites.", "dim": None, "delta": None, "valence": 0.2, "evidence": [1]}],
            "emotions": [{"type": "gratitude", "target": "Belra", "intensity": 2, "why": "she held Sindragosa for me", "evidence": [1]}],
            "diary": {"text": "Long night, good kill.", "evidence": [2]}}],
        "judge": [yes]})
    before = h.store.get_relationship(101, 102).trust
    run(reflect_member(ctx, 101))
    new = [m for m in h.rows(101) if m.source == "reflection"]
    assert sorted((m.kind, m.key or "") for m in new) == [("plan", ""), ("reflection", ""), ("reflection", "diary:2026-10-29")]
    assert all(m.evidence for m in new) and h.store.get_relationship(101, 102).trust == before + 4
    plan = [m for m in new if m.kind == "plan"][0]
    assert plan.actors == (102,) and h.store.get_state(101).goals == ("Ask Belra how she kites.",) and ctx.done("reflect", "101")
    assert tuple(h.store.conn.execute("SELECT type, target, intensity FROM emotion").fetchone()) == ("gratitude", 102, 2)


def test_reflect_prompt_names_him_by_his_pronouns_not_his_real_name(h):
    seed_day(h); h.woman(101, "lust")
    ctx = h.ctx({"questions": [{"questions": ["a", "b", "c"]}]}, default=generic_reflection)   # test-local: no outputs, a diary citing [1]
    run(reflect_member(ctx, 101))
    think = [c for c in ctx.reasoner.llm.calls if c.get("think")][0]["messages"]
    system, user = think[0]["content"], think[-1]["content"]
    assert "Arkon openly wants Testy." in system and "the guild master, he/him" in system and "quillon" not in system.casefold()
    assert "Testy (the guild master): you barely know him" in user and GOAL_QUESTIONS in user   # P1b's default would say "them"


def test_emotions_checked(h):
    # an emotion targeting "Coros" (not in the cited actors) -> grounding_log "name not in evidence: coros", no emotion row;
    # one targeting the owner himself -> "about not in evidence"; an untargeted "joy" citing [2] -> a row with target 0
    ids = seed_day(h)
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection(emotions=[
        {"type": "reproach", "target": "Coros", "intensity": 2, "why": "Coros stood in the fire again", "evidence": [1]},
        {"type": "pride", "target": "Arkon", "intensity": 1, "why": "kept the raid together", "evidence": [1]},
        {"type": "joy", "target": None, "intensity": 3, "why": "a good night", "evidence": [2]}])]})
    run(reflect_member(ctx, 101))
    assert grounding(h, "emotion") == [("emotion", "Coros stood in the fire again", "rejected", "name not in evidence: coros"),
                                       ("emotion", "kept the raid together", "rejected", "about not in evidence"),
                                       ("emotion", "a good night", "accepted", "")]
    rows = h.store.conn.execute("SELECT guid, night, type, target, intensity, why, source, evidence, ts FROM emotion").fetchall()
    assert [tuple(r) for r in rows] == [(101, "2026-10-29", "joy", 0, 3, "a good night", "reflect",
                                         json.dumps([f"memory:{ids['testy']}"]), h.T + RUN)]
    assert calls_titled(ctx, "judge") and all("[emotion]" not in c["messages"][-1]["content"] for c in calls_titled(ctx, "judge"))


def test_tom_needs_someone_who_does_not_know(h):
    # stage_knows over seed_day: 101 knows the root of alias [1]; 103 (in digest.people through one shared memory) does not;
    # the "Who knows what" section lists "- Coros doesn't know [n]"; a tom output about Coros citing [n] is written with
    # key "tom:103:2026-10-29"; the same output about Belra (a knower) is rejected "tom not supported"
    ids = seed_day(h)
    h.mem(101, "Coros asked me how the raid went.", actors=(103,), t=h.T - 1800)
    h.say(h.T - 3500, 105, "on it", channel="raid")                  # Ebbo typed in the raid: he saw Belra's line
    h.mem(101, "Ebbo kept dps on her.", actors=(105,), evidence=(f"chat:{ids['hold']}",), t=h.T - 2700)
    h.clock.set(h.T + RUN)
    run(stage_knows(h.ctx()))
    hold = f"chat:{ids['hold']}"
    assert 101 in knowers(h.store, hold) and 103 not in knowers(h.store, hold)

    def toms(text: str) -> dict:
        n = alias_of(text, "i got sindragosa")
        said = "Coros doesn't know Belra held Sindragosa."
        return reflection([out("tom", "Coros", said, [n]), out("tom", "Belra", said, [n]),
                           out("tom", "Ebbo", "Ebbo doesn't know how close it was.", [alias_of(text, "Ebbo kept dps")])])
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [toms]})
    run(reflect_member(ctx, 101))
    _, user = think_prompt(ctx)
    n = alias_of(user, "i got sindragosa")
    assert "Who knows what:\n" in user and f"- Coros doesn't know [{n}]" in user
    # beyond the brief: nobody is listed who could see the line (Belra said it), and never him, whom the ledger never holds
    assert "- Belra doesn't know" not in user and "- Testy doesn't know" not in user and "- Ebbo doesn't know" not in user
    (tom,) = [m for m in reflections(h) if (m.key or "").startswith("tom:")]
    assert (tom.key, tom.text, tom.kind, tom.actors, tom.importance, tom.evidence) == (
        "tom:103:2026-10-29", "Coros doesn't know Belra held Sindragosa.", "reflection", (103,), 5, (hold,))
    assert ("tom", "Coros doesn't know Belra held Sindragosa.", "rejected", "tom not supported") in grounding(h, "tom")
    # review-task11 I2: rule 12 agrees with the section: Ebbo was in the line's audience, so he may know it
    assert ("tom", "Ebbo doesn't know how close it was.", "rejected", "tom not supported") in grounding(h, "tom")


def test_nickname_once_a_week(h):
    # a nickname output "Kitey" about Belra -> a lexicon row (nickname, "Kitey", target 102, originator 101), 101 its adopter,
    # and the memory "I call Belra 'Kitey'."; the next night (same ISO week) a nickname about Coros is grounded but not
    # stored; a nickname for Testy the week after is stored with target 1
    ids = seed_day(h)
    run(reflect_member(h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection([out("nickname", "Belra", "Kitey", [1, 2])])]}), 101))
    (row,) = h.store.conn.execute("SELECT id, kind, term, target, originator, origin_ref, born, last_used FROM lexicon").fetchall()
    assert tuple(row)[1:] == ("nickname", "Kitey", 102, 101, f"memory:{ids['belra']}", h.T + RUN, h.T + RUN)   # the first cited
    assert [tuple(r) for r in h.store.conn.execute("SELECT term, guid, first_use, via FROM lexicon_adopter")] == [
        (row["id"], 101, h.T + RUN, 101)]
    (nick,) = [m for m in reflections(h) if m.key == "nick:102"]
    assert (nick.text, nick.kind, nick.importance, nick.actors, nick.source, nick.evidence) == (          # PR29
        "I call Belra 'Kitey'.", "reflection", 4, (102,), "reflection", (f"memory:{ids['belra']}", f"memory:{ids['testy']}"))

    for i, text in enumerate(("Coros pulled the whole room again.", "Ran a heroic after dinner.", "Fixed my keybinds.")):
        h.mem(101, text, actors=(103,) if i == 0 else (), t=h.T + DAY - 600 * (i + 1))
    h.clock.set(h.T + DAY + RUN)
    second = h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection([out("nickname", "Coros", "Pully", [1])])]},
                   night="2026-10-30")
    run(reflect_member(second, 101))
    assert ("nickname", "Pully", "accepted", "") in grounding(h, "nickname") and h.count("lexicon") == 1

    for i, text in enumerate(("Testy took the raid lead tonight.", "Farmed ore for an hour.", "Fixed the addon again.")):
        h.mem(101, text, actors=(1,) if i == 0 else (), t=h.T + 4 * DAY - 600 * (i + 1))
    h.clock.set(h.T + 4 * DAY + RUN)

    def week_after(text: str) -> dict:
        testy = alias_of(text, "Testy took the raid lead")
        belra = alias_of(text, "I call Belra 'Kitey'.")
        return reflection([out("nickname", "Belra", "Kites", [belra]), out("nickname", "Testy", "Captain T", [testy])])
    run(reflect_member(h.ctx({"questions": [{"questions": ["Kitey?", "lead?", "ore?"]}], "judge": [yes],
                              "reflection": [week_after]}, night="2026-11-02"), 101))
    assert [tuple(r) for r in h.store.conn.execute("SELECT term, target, originator FROM lexicon ORDER BY id")] == [
        ("Kitey", 102, 101), ("Captain T", 1, 101)]                     # beyond the brief: one per person, so no "Kites"
    assert [m.text for m in reflections(h) if (m.key or "").startswith("nick:")] == ["I call Belra 'Kitey'.", "I call Testy 'Captain T'."]


def test_who_knows_what_reads_the_week_before_the_window(h):
    """unknown_to reads what the member learned since a week before tonight's window: a root
    learned six days before it is listed, one learned eight days before is not."""
    seed_day(h)
    h.mem(101, "Coros asked me how the raid went.", actors=(103,), t=h.T - 1800)
    start = h.ctx().window[0]
    for days, text in ((6, "my sister got engaged"), (8, "my old car finally died")):
        line = h.say(start - days * DAY, 101, text, channel="guild")
        learn(h.store, f"chat:{line}", 101, "witnessed", ts=start - days * DAY)
    h.clock.set(h.T + RUN)
    ctx = h.ctx({"questions": [Q]}, default=generic_reflection)
    run(reflect_member(ctx, 101))
    _, user = think_prompt(ctx)
    assert f"- Coros doesn't know [{alias_of(user, 'sister got engaged')}]" in user and "car finally died" not in user


@pytest.mark.parametrize("relation", ["contradicts", "duplicates"])
def test_supersession_acts_on_what_the_model_decides(h, relation):              # Task 10, through the real path
    ids = seed_day(h)
    old = h.mem(101, "Belra is steady.", kind="reflection", source="reflection", actors=(102,), importance=5,
                evidence=("event:9",), t=h.T - 3 * DAY)
    h.clock.set(h.T + RUN)
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection([out("belief", "Belra", "Belra is steady under pressure.", [1])])],
                 "supersede": [{"decisions": [{"id": old, "relation": relation}]}]})
    run(reflect_member(ctx, 101))
    (asked,) = calls_titled(ctx, "supersede")
    assert f"[{old}] Belra is steady." in asked["messages"][-1]["content"]
    o = h.store.get_memories([old])[0]
    new = [m for m in reflections(h) if m.text == "Belra is steady under pressure."]
    if relation == "contradicts":
        assert (o.status, o.superseded_by) == ("superseded", new[0].id)
    else:                                                                       # folded in: nothing new stored
        assert new == [] and (o.status, o.evidence, o.merged_n, o.importance) == (
            "active", ("event:9", f"memory:{ids['belra']}"), 2, 7)


def test_ungrounded_outputs_logged_not_written(h):
    # reflection belief names "Coros" (not in evidence) -> grounding_log rejected row, no belief memory; diary still written
    seed_day(h)
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection([out("belief", "Belra", "Coros and Belra carried the raid.", [1])])]})
    run(reflect_member(ctx, 101))
    assert grounding(h, "belief") == [("belief", "Coros and Belra carried the raid.", "rejected", "name not in evidence: coros")]
    assert [(m.kind, m.key) for m in reflections(h)] == [("reflection", "diary:2026-10-29")]


def test_cancel_mid_member_writes_nothing(h):
    seed_day(h); gate = asyncio.Event(); ctx = h.ctx({"questions": [{"questions": ["a", "b", "c"]}]}, gate=gate)
    before = h.count("memory")
    async def go():
        t = asyncio.create_task(reflect_member(ctx, 101)); await asyncio.sleep(0.01); t.cancel()
        with pytest.raises(asyncio.CancelledError): await t
    run(go())
    assert h.count("memory") == before and not ctx.done("reflect", "101")


def test_skips_and_isolates_failures(h):
    # 102 has 2 memories -> skipped without calls; 101's reflection reply invalid twice -> failures["reflect"] == 1;
    # 103 (seeded like seed_day) is still reflected by stage_reflect
    seed_day(h, 101)
    h.mem(101, "Lost my keys before the raid.", t=h.T - 600)
    seed_day(h, 103)
    for text in ("Healed the first pull.", "Logged off early."):
        h.mem(102, text, t=h.T - 900)
    with h.store.transaction():                                       # beyond the brief: a member on a break still reflects
        h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at) VALUES (103, 'exams', ?, ?)",
                             (h.T - DAY, h.T + 2 * DAY))
    bad = {"outputs": [], "emotions": []}                             # no diary: invalid
    ctx = h.ctx(default=by_title(questions=Q, judge=yes,
                                 reflection=lambda text: bad if "Lost my keys" in text else reflection()))
    run(stage_reflect(ctx))
    assert ctx.failures["reflect"] == 1 and not ctx.done("reflect", "101")
    assert all(ctx.done("reflect", str(g)) for g in (102, 103, 104, 105))
    thinkers = sorted(re.match(r"These are (\w+)'s", c["messages"][0]["content"]).group(1)
                      for c in ctx.reasoner.llm.calls if c.get("think"))
    assert thinkers == ["Arkon", "Coros"] and len(calls_titled(ctx, "reflection")) == 3   # 101's twice, 103's once
    assert [m.key for m in reflections(h, 103)] == ["diary:2026-10-29"] and reflections(h, 101) == []


def test_reflection_evidence_fits_the_context(h):
    # 101 with 60 new memories of 190 characters and 30 more retrieved -> the think prompt's evidence section is at most
    # 14,000 characters, keeps the who-knows-what items and the digest, and drops retrieved ones first; a reflection
    # claim citing a dropped alias fails "unknown evidence"; with questions_min_new 6 and 4 new memories -> no
    # "questions" call at all
    def sized(text: str) -> str:
        return (text + " " + "and then we carried on with the evening " * 6)[:190]
    for i in range(60):
        h.mem(101, sized(f"Tonight number {i} went fine with Belra" if i == 0 else f"Tonight number {i} went fine"),
              actors=(102,) if i == 0 else (), t=h.T - 60 * i)
    for i, word in enumerate(["quarry", "harbor", "orchard"] * 10):
        h.mem(101, sized(f"Old {word} story {i}"), t=h.T - 3 * DAY - 60 * i)
    told = h.say(h.T - 7200, 101, "my brother finally passed his driving test", channel="guild")
    learn(h.store, f"chat:{told}", 101, "witnessed", ts=h.T - 7200)      # Belra does not know it
    h.clock.set(h.T + RUN)
    cited: list[int] = []

    def citing_a_dropped_alias(text: str) -> dict:
        shown = {int(a) for a in _ALIAS.findall(text)}
        cited.append(min(set(range(61, 91)) - shown))
        return reflection([out("belief", None, "Some nights just go fine.", [cited[0]])])
    ctx = h.ctx({"questions": [{"questions": ["quarry", "harbor", "orchard"]}], "judge": [yes],
                 "reflection": [citing_a_dropped_alias]})
    run(reflect_member(ctx, 101))
    _, user = think_prompt(ctx)
    lines = [ln for ln in user.splitlines() if _ALIAS.match(ln)]
    shown = [int(_ALIAS.match(ln).group(1)) for ln in lines]
    assert len("\n".join(lines)) <= EVIDENCE_CHARS == 14000
    assert set(range(1, 61)) <= set(shown)                                     # the whole digest
    told_alias = alias_of(user, "passed his driving test")
    assert told_alias == 91 and "- Belra doesn't know [91]" in user           # the who-knows-what item
    kept = sorted(set(shown) & set(range(61, 91)))
    assert kept and kept == list(range(61, 61 + len(kept))) and len(kept) < 30   # the last retrieved went first
    assert grounding(h, "belief") == [("belief", "Some nights just go fine.", "rejected", "unknown evidence")]

    small = h.ctx({"judge": [yes], "reflection": [reflection(diary={**DIARY, "evidence": [1]})]}, night="2026-10-30")
    small.settings = replace(small.settings, questions_min_new=6)
    for i in range(4):
        h.mem(103, f"Quiet night {i}.", t=h.T + DAY - 60 * i)
    h.clock.set(h.T + DAY + RUN)
    run(reflect_member(small, 103))
    assert calls_titled(small, "questions") == [] and small.done("reflect", "103")
    assert [c["input"] for c in small.reasoner.llm.calls if "input" in c] == [["Long night, good kill."]]   # no retrieval


@pytest.mark.parametrize("whispered", [True, False])
def test_outputs_take_the_scope_of_what_they_rest_on(h, whispered, caplog):         # controller ruling B2
    # seed_day plus 101's memory "Testy whispered me he is quitting the bakery." (scope private, scope_with 1) in the
    # digest; the reflection cites only open aliases: a belief "Belra is steady under pressure." judged yes -> stored
    # open; a belief "Testy is quitting the bakery soon." judged yes -> ("private", 1) through the backstop (two content
    # words shared with the private item, M7); a plan judged partly -> ("private", 1), because the prompt showed the
    # private memory, and it is not in persona_state.goals, while a plan judged yes is (M5); a diary judged yes -> open;
    # an emotion happy_for Testy with why "he is quitting the bakery" -> its emotion row has why "" and keeps its cited
    # evidence; a nickname -> no lexicon row and a nickname_not_open log line; a belief citing the private alias ->
    # ("private", 1); with that memory gone, the same partly plan is open and the emotion keeps its why
    ids = seed_day(h)
    if whispered:
        h.mem(101, "Testy whispered me he is quitting the bakery.", actors=(1,), scope="private", scope_with=1, t=h.T - 600)
    texts = {"steady": "Belra is steady under pressure.", "bakery": "Testy is quitting the bakery soon.",
             "partly": "Ask Belra how she kites.", "plan": "Bring Testy a bakery treat tomorrow.",   # one word shared: open
             "told": "Testy told me first.", "flasks": "Restocked flasks before every pull."}      # an open item's words

    def scoped(text: str) -> dict:
        belra, testy = alias_of(text, "Belra held Sindragosa"), alias_of(text, "Testy pulled us through")
        outputs = [out("belief", "Belra", texts["steady"], [belra]), out("belief", "Testy", texts["bakery"], [testy]),
                   out("plan", "Belra", texts["partly"], [belra]), out("plan", "Testy", texts["plan"], [testy]),
                   out("nickname", "Belra", "Kitey", [belra]),
                   out("belief", None, texts["flasks"], [alias_of(text, "Repaired and restocked")])]
        if whispered:
            outputs.append(out("belief", "Testy", texts["told"], [alias_of(text, "whispered me")]))
        return reflection(outputs, [{"type": "happy_for", "target": "Testy", "intensity": 2,
                                     "why": "he is quitting the bakery", "evidence": [testy]}],
                          diary={"text": "Long night, good kill.", "evidence": [belra]})
    ctx = h.ctx({"questions": [Q], "judge": [judge_by(partly=(texts["partly"],))], "reflection": [scoped]})
    ctx.settings = replace(ctx.settings, max_outputs=7)                # seven outputs in one reflection
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(reflect_member(ctx, 101))
    scope = {m.text: (m.scope, m.scope_with) for m in reflections(h)}
    private, open_ = ("private", 1), ("open", 0)
    assert scope[texts["steady"]] == open_ and scope["Long night, good kill."] == open_ and scope[texts["plan"]] == open_
    assert scope[texts["flasks"]] == open_                             # sharing an open item's words is no backstop
    assert scope[texts["bakery"]] == (private if whispered else open_)
    assert scope[texts["partly"]] == (private if whispered else open_)
    assert h.store.get_state(101).goals == ((texts["plan"],) if whispered else (texts["partly"], texts["plan"]))
    (emotion,) = h.store.conn.execute("SELECT type, target, why, evidence FROM emotion").fetchall()
    assert tuple(emotion) == ("happy_for", 1, "" if whispered else "he is quitting the bakery",
                              json.dumps([f"memory:{ids['testy']}"]))
    assert h.count("lexicon") == (0 if whispered else 1) and ("nickname_not_open" in caplog.text) == whispered
    if whispered:
        assert scope[texts["told"]] == private
    assert no_real_name(h.store) == []


PLAIN = ("Testy whispered me he is leaving the bakery.", "private", 1)
SECRET = ("Testy told me, between us, he is leaving the bakery.", "secret", 1)
HEARD = ("Belra told me Testy is leaving the bakery.", "private", 102)       # a hearer's copy (Task 16)


@pytest.mark.parametrize("cites,shows,text,stored", [
    (PLAIN, SECRET, "Testy is leaving the bakery for good.", ("secret", 1)),     # the class rises to secret
    (HEARD, SECRET, "Testy is leaving the bakery for good.", ("secret", 0)),     # two partners: nobody
    (HEARD, PLAIN, "Testy is leaving the bakery for good.", ("private", 0)),
    (SECRET, PLAIN, "Testy is leaving the bakery for good.", ("secret", 1)),     # never weakened
    (HEARD, None, "Testy is leaving the bakery for good.", ("private", 102)),    # nothing stricter shown
    (HEARD, SECRET, "Testy works the early shift.", ("private", 102))])          # no two words shared
def test_the_backstop_raises_an_output_already_scoped(h, cites, shows, text, stored):   # review-task11 I3
    """The backstop covers an output whose own evidence is already scoped too: one that shares
    two content words with a stricter shown item takes the scope of everything shown, so it is
    never left weaker than an overlapping secret thread (a private copy would let gossip carry
    it, amendment A4), and two partners give nobody. Its scope never falls below its own."""
    seed_day(h)
    for mem in (cites, shows):
        if mem is not None:
            words, scope, with_ = mem
            h.mem(101, words, actors=(1,) if with_ == 1 else (102, 1), scope=scope, scope_with=with_, t=h.T - 600)
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [
        lambda prompt: reflection([out("belief", "Testy", text, [alias_of(prompt, cites[0])])])]})
    run(reflect_member(ctx, 101))
    (m,) = [m for m in reflections(h) if m.text == text]
    assert (m.scope, m.scope_with) == stored
    own = ctx.writer.scope_of(101, m.evidence)
    assert stricter((m.scope, m.scope_with), own) == (m.scope, m.scope_with)          # never below its own


# -- pre-flight PR7: the stored scope, before supersession and self-notes -------------------------

BAKERY = "Testy is quitting the bakery soon."


def bakery_night(h, *twins):
    """seed_day, his whispered news in the digest and older beliefs of 101 from three days ago
    (the `twins`: (text, scope, scope_with)); the reflection's belief cites only his open raid
    line's memory, so only the backstop makes it private. Returns the ids and the twins' ids."""
    ids = seed_day(h)
    h.mem(101, "Testy whispered me he is quitting the bakery.", actors=(1,), scope="private", scope_with=1, t=h.T - 600)
    made = [h.mem(101, text, kind="reflection", source="reflection", actors=(1,), scope=scope, scope_with=with_,
                  evidence=(f"memory:{ids['testy']}",), t=h.T - 3 * DAY) for text, scope, with_ in twins]
    h.clock.set(h.T + RUN)
    return ids, made


def bakery_belief(text: str) -> dict:
    return reflection([out("belief", "Testy", BAKERY, [alias_of(text, "Testy pulled us through")])])


def test_a_backstopped_belief_never_merges_into_or_supersedes_an_open_belief(h):      # PR7
    ids, (open_twin,) = bakery_night(h, ("Testy is quitting the bakery.", "open", 0))
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [bakery_belief]})       # no "supersede" reply: none asked
    run(reflect_member(ctx, 101))
    (new,) = [m for m in reflections(h) if m.text == BAKERY]
    assert (new.scope, new.scope_with) == ("private", 1) and calls_titled(ctx, "supersede") == []
    (twin,) = h.store.get_memories([open_twin])
    assert (twin.status, twin.superseded_by, twin.evidence, twin.merged_n, twin.importance) == (
        "active", None, (f"memory:{ids['testy']}",), 1, 5)


def test_the_model_naming_the_open_twin_as_a_duplicate_changes_nothing(h):           # PR7, through the real path
    ids, (open_twin, private_twin) = bakery_night(h, ("Testy is quitting the bakery.", "open", 0),
                                                  ("Testy is leaving the bakery.", "private", 1))
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [bakery_belief],
                 "supersede": [{"decisions": [{"id": open_twin, "relation": "duplicates"}]}]})
    run(reflect_member(ctx, 101))
    (decide,) = calls_titled(ctx, "supersede")
    asked = decide["messages"][-1]["content"]
    assert f"[{private_twin}] Testy is leaving the bakery." in asked and f"[{open_twin}]" not in asked
    twins = {m.id: m for m in h.store.get_memories([open_twin, private_twin])}
    for t in twins.values():
        assert (t.status, t.superseded_by, t.evidence, t.merged_n, t.importance) == (
            "active", None, (f"memory:{ids['testy']}",), 1, 5)
    (new,) = [m for m in reflections(h) if m.text == BAKERY]
    assert (new.scope, new.scope_with, new.importance) == ("private", 1, 6)   # min(9, round(mean([5])) + 1)


@pytest.mark.parametrize("whispered,verdict", [(True, "consistent"), (False, "consistent"), (False, "contradicts")])
def test_a_backstopped_self_insight_is_never_a_self_note(h, whispered, verdict):    # PR7
    """A self-insight eligible by what it cites (three items over two nights, nothing private)
    that shares two content words with the whisper the prompt showed is stored private, never
    put to the core judge and never a self-note. Without the whisper it is open, judged against
    the core, and a note or a review as the judge says."""
    ids = seed_day(h)
    if whispered:
        h.mem(101, "Testy whispered me he is quitting the bakery.", actors=(1,), scope="private", scope_with=1, t=h.T - 600)
    h.mem(101, "Left the raid early for a bakery shift.", t=h.T - 2 * DAY)
    h.clock.set(h.T + RUN)
    insight = "I keep quitting raids early for bakery shifts."

    def reply(text: str) -> dict:
        refs = [alias_of(text, n) for n in ("Repaired and restocked", "Sorted my bags", "Left the raid early")]
        return reflection([out("self_insight", None, insight, refs)])
    ctx = h.ctx({"questions": [{"questions": ["bakery shift?", "q2", "q3"]}], "judge": [yes], "reflection": [reply],
                 "core_judge": [{"verdict": verdict, "why": "core says raider"}]})
    run(reflect_member(ctx, 101))
    (m,) = [m for m in reflections(h) if m.text == insight]
    assert (m.scope, m.scope_with) == (("private", 1) if whispered else ("open", 0))
    assert len(calls_titled(ctx, "core_judge")) == (0 if whispered else 1)
    notes = [n["text"] for n in h.store.get_state(101).self_notes]
    queued = [tuple(r) for r in h.store.conn.execute("SELECT kind, text, reason FROM review_queue")]
    if whispered or verdict == "contradicts":
        assert notes == [] and ledger_rows(h.store, 101) == []
        assert queued == ([] if whispered else [("self_note", insight, "core says raider")])
    else:
        assert notes == [insight] and [r["kind"] for r in ledger_rows(h.store, 101)] == ["note_added"] and queued == []


# -- beyond the plan -----------------------------------------------------------------------------

def test_the_schemas_and_texts_hold_the_plans_values(h):
    assert GOAL_QUESTIONS == "What bugged you today? What do you want tomorrow?"
    assert OCC == ("joy", "distress", "pride", "shame", "admiration", "reproach", "gratitude", "anger", "hope", "fear",
                   "happy_for", "resentment", "gloating", "pity")
    assert (EVIDENCE_CHARS, PRIVATE_OVERLAP) == (14000, 2)
    assert QUESTIONS_SCHEMA["title"] == "questions" and REFLECTION_SCHEMA["title"] == "reflection"
    check_schema(QUESTIONS_SCHEMA, {"questions": ["a", "b", "x" * 150]})
    for bad in ({"questions": ["a", "b"]}, {"questions": ["a", "b", "c", "d"]}, {"questions": ["a", "b", "x" * 151]}):
        with pytest.raises(ValueError):
            check_schema(QUESTIONS_SCHEMA, bad)
    one = out("rel_delta", "Belra", "x" * 200, [1], dim="trust", delta=-10, valence=-1)
    emotion = {"type": "pity", "target": None, "intensity": 3, "why": "x" * 80, "evidence": [1]}
    check_schema(REFLECTION_SCHEMA, reflection([one] * 5, [emotion] * 3, {"text": "x" * 200, "evidence": [1]}))
    for bad in (reflection([one] * 6), reflection([], [emotion] * 4), reflection([{**one, "kind": "life"}]),
                reflection([{**one, "dim": "attraction"}]), reflection([{**one, "delta": 11}]),
                reflection([{**one, "evidence": []}]), reflection([{**one, "valence": 1.5}]),
                reflection([], [{**emotion, "type": "awe"}]), reflection([], [{**emotion, "intensity": 0}]),
                reflection([], [{**emotion, "why": "x" * 81}]), reflection(diary={"text": "x", "evidence": []}),
                {"outputs": [], "emotions": []}):
        with pytest.raises(ValueError):
            check_schema(REFLECTION_SCHEMA, bad)
    for kind in ("belief", "self_insight", "plan", "tom", "nickname"):
        check_schema(REFLECTION_SCHEMA, reflection([out(kind, None, "x", [1])]))


def test_digest_people_and_status(h):
    """people: the six most frequent actors, never the owner or 0, ties in the digest's order;
    only active memories count."""
    for i, actors in enumerate([(102, 103), (102,), (104, 0), (101, 105), (106,), (107,), (108,), (103,)]):
        h.mem(101, f"Memory {i}.", actors=actors, importance=9 - i)
    h.mem(101, "Gone.", actors=(109,) * 1, importance=10, status="superseded")
    d = build_digest(h.store, 101, h.ctx().window)
    assert d.owner == 101 and d.people == [102, 103, 104, 105, 106, 107] and "Gone." not in [m.text for m in d.memories]
    assert build_digest(h.store, 101, h.ctx().window, size=2).people == [102, 103]
    assert build_digest(h.store, 101, h.ctx().window, min_new=9) is None


def test_the_questions_and_the_retrieval(h):
    """The questions call shows the digest alone and asks the plan's question; the think prompt
    carries the questions, and each one is retrieved by (one embedding each)."""
    seed_day(h)
    ctx = h.ctx({"questions": [{"questions": ["Who held the boss?", "b?", "c?"]}]}, default=generic_reflection)
    run(reflect_member(ctx, 101))
    (q,) = calls_titled(ctx, "questions")
    user = q["messages"][-1]["content"]
    assert ("Given only these memories, what are the 3 most salient high-level questions we can answer about the "
            "people in them?") in user
    assert "[1] (Thu 29 Oct) Belra held Sindragosa for me." in user and "[5] (Thu 29 Oct) Made tea between the pulls." in user
    assert "Arkon" in q["messages"][0]["content"] and q["format"] == QUESTIONS_SCHEMA
    _, think = think_prompt(ctx)
    assert all(f"- {x}" in think for x in ("Who held the boss?", "b?", "c?"))
    assert [c["input"] for c in ctx.reasoner.llm.calls if "input" in c][:3] == [["Who held the boss?"], ["b?"], ["c?"]]
    assert think.index("Testy (the guild master)") < think.index("- Who held the boss?") < think.index(GOAL_QUESTIONS)


@pytest.mark.parametrize("stance", [None, "drawn"])
def test_romance_only_toward_him_and_only_from_a_woman_with_an_attitude(h, stance):   # Task 9's rules
    if stance:
        h.woman(101, stance)
    seed_day(h)
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection([
        out("belief", "Testy", "I have a crush on Testy.", [2]), out("belief", "Belra", "I have a crush on her.", [1]),
        out("plan", None, "Flirt with Testy in whispers.", [2])])]})
    run(reflect_member(ctx, 101))
    verdicts = {text: reason for _, text, _, reason in grounding(h) if text != "Long night, good kill."}
    him = "" if stance else "romance without an attitude"
    assert verdicts == {"I have a crush on Testy.": him, "I have a crush on her.": "romance between members",
                        "Flirt with Testy in whispers.": him}
    assert sorted(m.text for m in reflections(h)) == sorted(
        ["Long night, good kill."] + (["I have a crush on Testy.", "Flirt with Testy in whispers."] if stance else []))


def test_a_nickname_that_is_his_real_name_is_never_coined(h):
    seed_day(h)
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection([out("nickname", "Testy", "quillon", [2])])]})
    run(reflect_member(ctx, 101))
    assert grounding(h, "nickname") == [("nickname", "quillon", "rejected", "real name")]
    assert h.count("lexicon") == 0 and h.count("lexicon_adopter") == 0 and no_real_name(h.store) == []


def test_a_rel_delta_moves_the_view_under_the_night_cap(h):
    seed_day(h)
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection([
        out("rel_delta", "Belra", "She saved me.", [1], dim="trust", delta=8),
        out("rel_delta", "Belra", "She saved me twice.", [1], dim="trust", delta=8),
        out("rel_delta", "Testy", "He kept us going.", [2], dim="respect", delta=-3),
        out("rel_delta", None, "Somebody let me down.", [2], dim="trust", delta=-2)])]})
    run(reflect_member(ctx, 101))
    assert (h.store.get_relationship(101, 102).trust, h.store.get_relationship(101, 1).respect) == (10.0, 0.0)
    rows = h.store.conn.execute("SELECT a, b, dim, delta, reason, applied FROM rel_delta ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(101, 102, "trust", 8.0, "reflection", 1), (101, 102, "trust", 2.0, "reflection", 1)]
    assert ("rel_delta", "Somebody let me down.", "rejected", "about not in evidence") in grounding(h, "rel_delta")
    assert ctx.done("reflect", "101")


def test_the_commit_holds_every_write_and_the_logs(h):
    """One commit: a failure in it (here the emotion insert) leaves no memory, no goal, no
    relationship change and no step; a clean one logs every claim."""
    seed_day(h)
    replies = {"questions": [Q, Q], "judge": [yes, yes], "reflection": [reflection([
        out("plan", "Belra", "Ask Belra how she kites.", [1]), out("rel_delta", "Belra", "She saved me.", [1], dim="trust", delta=4)],
        [{"type": "joy", "target": None, "intensity": 2, "why": "a good night", "evidence": [2]}])] * 2}
    ctx = h.ctx(replies)
    before = h.count("memory")
    with h.store.transaction():
        h.store.conn.execute("CREATE TEMP TRIGGER no_feelings BEFORE INSERT ON emotion BEGIN SELECT RAISE(ABORT, 'no'); END")
    run(reflect_member(ctx, 101))
    assert ctx.failures["reflect"] == 1 and not ctx.done("reflect", "101") and h.count("memory") == before
    assert h.store.get_state(101).goals == () and h.store.get_relationship(101, 102).trust == 0.0 and h.count("grounding_log") == 0
    with h.store.transaction():
        h.store.conn.execute("DROP TRIGGER no_feelings")
    run(reflect_member(ctx, 101))
    assert ctx.done("reflect", "101") and h.store.get_state(101).goals == ("Ask Belra how she kites.",)
    assert [r[2] for r in grounding(h)] == ["accepted"] * 4                     # plan, rel_delta, diary, emotion
    calls = len(ctx.reasoner.llm.calls)
    run(reflect_member(ctx, 101))                                               # done: nothing asked again
    assert len(ctx.reasoner.llm.calls) == calls and ctx.failures["reflect"] == 1


@pytest.mark.parametrize("asked", [True, False])
def test_a_private_memory_the_trim_dropped_still_counts_when_the_questions_read_it(h, asked):   # B2
    """The digest overflows the evidence (three long who-knows-what lines take room the digest
    needs), so its least important memories are trimmed out of the think prompt: one of them is
    his whispered news. The questions call read the whole digest, and the questions ride along
    in the think prompt, so with questions it still counts as shown and a belief sharing its
    words is backstopped; without questions nothing showed it, and the belief is open."""
    people = {0: ("with Belra", (102,)), 1: ("with Testy", (1,))}
    for i in range(59):
        said, actors = people.get(i, ("", ()))
        h.mem(101, f"Tonight number {i} {said}".strip().ljust(200, "."), actors=actors, importance=5, t=h.T - 60 * i)
    h.mem(101, "Testy whispered me he is quitting the bakery.".ljust(200, "."), actors=(1,), importance=1,
          scope="private", scope_with=1, t=h.T - 3600)
    for i in range(3):
        line = h.say(h.T - 7200 - 3600 * i, 101, f"long story {i} " + "about my week " * 30, channel="guild")
        learn(h.store, f"chat:{line}", 101, "witnessed", ts=h.T - 7200)
    h.clock.set(h.T + RUN)

    def belief(text: str) -> dict:
        return reflection([out("belief", "Testy", BAKERY, [alias_of(text, "Tonight number 1 with Testy")])],
                          diary={**DIARY, "evidence": [alias_of(text, "Tonight number 2.")]})
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [belief]})
    if not asked:
        ctx.settings = replace(ctx.settings, questions_min_new=61)
    run(reflect_member(ctx, 101))
    _, user = think_prompt(ctx)
    assert "whispered me" not in user and "- Belra doesn't know" in user          # trimmed; the tier-0 lines stay
    (q,) = calls_titled(ctx, "questions") if asked else [None]
    assert asked == (q is not None and "whispered me" in q["messages"][-1]["content"])
    (m,) = [m for m in reflections(h) if m.text == BAKERY]
    assert (m.scope, m.scope_with) == (("private", 1) if asked else ("open", 0))


def test_the_questions_call_needs_questions_min_new_memories(h):
    for g, n in ((103, 6), (104, 5)):
        for i in range(n):
            h.mem(g, f"Quiet night {i}.", t=h.T - 60 * i)
    h.clock.set(h.T + RUN)
    ctx = h.ctx(default=by_title(questions=Q, judge=yes, reflection=reflection(diary={**DIARY, "evidence": [1]})))
    ctx.settings = replace(ctx.settings, questions_min_new=6)
    run(stage_reflect(ctx))
    (q,) = calls_titled(ctx, "questions")
    assert "These are Coros's memories" in q["messages"][0]["content"]
    assert all(ctx.done("reflect", str(g)) for g in (103, 104))


def test_what_is_stored_carries_its_importance_and_vector(h):
    seed_day(h)
    top = h.mem(101, "Coros pulled the room and we lived.", actors=(103,), importance=10, t=h.T - 300)
    low = h.mem(101, "Coros wiped us once.", actors=(103,), importance=3, t=h.T - 400)
    h.clock.set(h.T + RUN)

    def reply(text: str) -> dict:
        a, b, belra = alias_of(text, "pulled the room"), alias_of(text, "wiped us once"), alias_of(text, "Belra held")
        return reflection([out("belief", "Coros", "Coros keeps pulling early.", [a]),
                           out("self_insight", None, "I forgive wipes quickly.", [belra, b]),   # mean 4.5 rounds to 4
                           out("plan", "Coros", "Talk to Coros about pulls.", [a]),
                           out("tom", "Coros", "Coros thinks nobody noticed.", [b]),
                           out("nickname", "Coros", "Pully", [a])], diary=DIARY)
    run(reflect_member(h.ctx({"questions": [Q], "judge": [yes], "reflection": [reply]}), 101))
    got = {m.text: m for m in reflections(h)}
    assert {t: got[t].importance for t in got} == {
        "Coros keeps pulling early.": 9, "I forgive wipes quickly.": 5, "Talk to Coros about pulls.": 5,
        "Coros thinks nobody noticed.": 5, "I call Coros 'Pully'.": 4, "Long night, good kill.": 3}
    assert all(m.embedding is not None for m in got.values()) and all(m.t_created == h.T + RUN for m in got.values())
    assert got["Coros keeps pulling early."].evidence == (f"memory:{top}",)
    assert got["Coros thinks nobody noticed."].evidence == (f"memory:{low}",)


@pytest.mark.parametrize("thread", ["", "saving up for a new chair"])
def test_the_think_prompt_carries_what_is_on_their_mind(h, thread):
    """persona_state.life_thread (Task 13), else the persona's own, as P1b's "On your mind
    lately" reads them."""
    seed_day(h)
    if thread:
        h.store.put_state(replace(h.store.get_state(101), life_thread=thread))
    ctx = h.ctx({"questions": [Q]}, default=generic_reflection)
    run(reflect_member(ctx, 101))
    _, user = think_prompt(ctx)
    assert f"On Arkon's mind lately: {thread or 'learning to cook'}" in user


def test_a_vetoed_nickname_is_never_coined_again(h, caplog):
    ids = seed_day(h)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO lexicon(kind, term, target, origin_ref, originator, born, last_used, status) "
                             "VALUES ('nickname', 'Kitey', 102, 'memory:1', 103, 1.0, 1.0, 'vetoed')")
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection([out("nickname", "Belra", "Kitey", [1]),
                                                                              out("nickname", "Belra", "Kites", [1])])]})
    ctx.settings = replace(ctx.settings, nicknames_per_week=2)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(reflect_member(ctx, 101))
    assert "nickname_not_coined" in caplog.text and "reason=vetoed" in caplog.text
    terms = h.store.conn.execute("SELECT l.term, l.status, a.guid FROM lexicon l LEFT JOIN lexicon_adopter a "
                                 "ON a.term = l.id ORDER BY l.id").fetchall()
    assert [tuple(r) for r in terms] == [("Kitey", "vetoed", None), ("Kites", "active", 101)]
    assert [m.text for m in reflections(h) if m.key == "nick:102"] == ["I call Belra 'Kites'."]


def test_a_self_insight_from_one_night_is_stored_but_never_a_note(h):
    seed_day(h)
    ctx = h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection([
        out("self_insight", None, "I like a tidy bag.", [3, 4, 5])])]})                # three items, one night
    run(reflect_member(ctx, 101))
    (m,) = [m for m in reflections(h) if m.text == "I like a tidy bag."]
    assert (m.scope, m.kind) == ("open", "reflection") and calls_titled(ctx, "core_judge") == []
    assert h.store.get_state(101).self_notes == () and ledger_rows(h.store, 101) == []


def test_the_json_pass_stays_inside_num_ctx(h):
    """At its fullest the evidence (EVIDENCE_CHARS) with the cards, the questions, the output
    instruction, 2,048 tokens of notes and the schema would run past num_ctx 8192, and Ollama
    would cut the prompt's head, where the system prompt and INVARIANTS are (review M16). Notes
    too long for the call lose their start (their conclusions come last); short ones go whole."""
    seed_day(h, 101)
    seed_day(h, 103)
    long_notes = "I keep thinking it over. " * 1600 + "So: Belra is steady."
    ctx = h.ctx(default=by_title(questions=Q, judge=yes, reflection=reflection()))

    async def think(system: str, user: str) -> str:
        return long_notes if "Arkon's private thoughts" in system else "short notes, the end"
    ctx.reasoner.think = think
    run(stage_reflect(ctx))
    asks = {c["messages"][0]["content"].split("'s private")[0][-5:]: c["messages"] for c in calls_titled(ctx, "reflection")}
    arkon, coros = asks["Arkon"], asks["Coros"]
    assert estimate_tokens(arkon[0]["content"]) + estimate_tokens(arkon[-1]["content"]) + 1024 <= 8192
    assert "So: Belra is steady." in arkon[-1]["content"] and long_notes not in arkon[-1]["content"]
    assert "Your notes:\nshort notes, the end" in coros[-1]["content"]


def eligible_insight_night(h, owner: int = 101) -> str:
    """seed_day for `owner` and one older memory from two nights before, which the questions
    "bakery shift?" bring back; returns the reflection's reply for a self-insight that cites two
    of tonight's items and the older one: three items over two nights, all open (eligible)."""
    seed_day(h, owner)
    h.mem(owner, "Left the raid early for a bakery shift.", t=h.T - 2 * DAY)
    h.clock.set(h.T + RUN)
    return "I keep leaving raids early for bakery shifts."


def insight(text: str, *more: str):
    """A reply with the self-insight `text` (and `more`) citing the eligible night's three items."""
    def reply(prompt: str) -> dict:
        refs = [alias_of(prompt, n) for n in ("Repaired and restocked", "Sorted my bags", "Left the raid early")]
        return reflection([out("self_insight", None, t, refs) for t in (text, *more)])
    return reply


BAKERY_Q = {"questions": ["bakery shift?", "q2", "q3"]}


def test_the_core_judge_is_priced_and_measured_on_its_own(h, monkeypatch):   # review-task11 I1
    """Task 5's budget prices the core judge: a planning cost per reflecting member (whether or
    not anyone holds supersession candidates), stage_reflect records the calls it made as the
    night's core_judge row, the measured count comes from those rows, and supersession's
    measured decide count leaves them out."""
    assert budget.UNIT_CALLS["core_judge"] == {"json": budget.CORE_JUDGE_CALLS} and budget.CORE_JUDGE_CALLS == 0.5
    assert budget.MEASURED_AS["core_judge"] == "reflect"
    text = eligible_insight_night(h)
    for g in (102, 103):
        for i in range(3):
            h.mem(g, f"Quiet raid, take {i}.", t=h.T - 60 * i)
    arkon = "Repaired and restocked"                                       # only Arkon's digest has it
    ctx = h.ctx({"core_judge": [{"verdict": "consistent", "why": ""}]}, default=by_title(
        judge=yes, questions=lambda p: BAKERY_Q if arkon in p else Q,
        reflection=lambda p: insight(text)(p) if arkon in p else reflection()))
    assert budget.COUNTERS["core_judge"](ctx) == 3 and budget.COUNTERS["supersede"](ctx) == 0
    plan = run(budget.plan_night(ctx))
    assert plan["units"]["core_judge"] == 3
    run(stage_reflect(ctx))
    assert [n["text"] for n in h.store.get_state(101).self_notes] == [text]
    row = h.store.conn.execute("SELECT calls FROM night_stage WHERE night = ? AND stage = 'core_judge'",
                               (h.night,)).fetchone()
    assert row["calls"] == 1                                                       # one core judgement tonight
    # measured on its own, once three nights have it; supersession's decide count leaves it out
    from leeroy_mind.nightly.store import run_begin, set_run_plan, stage_record
    for night, core, decides in (("2026-10-20", 4, 0), ("2026-10-21", 4, 3), ("2026-10-22", 8, 6)):
        run_begin(h.store, night, 0, 1, now=0)
        units = {"reflect": 4, "core_judge": 4} | ({"supersede": 3} if decides else {})
        set_run_plan(h.store, night, {"units": units})
        calls = 4 * 4 + decides + core
        stage_record(h.store, night, "reflect", started=0, finished=1, calls=calls, call_s=5.0 * calls)
        stage_record(h.store, night, "core_judge", started=0, finished=1, calls=core, call_s=5.0 * core)
    assert budget.measured_core_judge_calls(h.store) == 1                     # the median of 4/4, 4/4 and 8/4
    assert budget.measured_supersede_calls(h.store) is None                    # two nights with candidates only
    run_begin(h.store, "2026-10-23", 0, 1, now=0)
    set_run_plan(h.store, "2026-10-23", {"units": {"reflect": 4, "core_judge": 4, "supersede": 3}})
    stage_record(h.store, "2026-10-23", "reflect", started=0, finished=1, calls=16 + 9 + 4, call_s=5.0 * 29)
    stage_record(h.store, "2026-10-23", "core_judge", started=0, finished=1, calls=4, call_s=20.0)
    assert budget.measured_supersede_calls(h.store) == 2                      # 1, 2 and 3 decides a unit: core left out
    monkeypatch.setattr(budget, "COUNTERS", {"core_judge": lambda ctx: 10})
    assert run(budget.plan_night(h.ctx()))["estimate_s"] == pytest.approx(10 * 1 * 5.0)   # the measured count and cost


def test_a_self_note_is_never_added_twice(h):                           # review-task11 M1
    """A self-insight that restates a held note, in other case or punctuation, or that
    supersession folds into a reflection holding a note's words, is stored but never put to the
    core judge again; two alike in one reflection are judged once."""
    text = eligible_insight_night(h)
    held = "I lead when nobody else will."
    h.store.put_state(replace(h.store.get_state(101), self_notes=(
        {"text": text.upper().rstrip("."), "evidence": ["memory:1"], "added": 1.0},
        {"text": held, "evidence": ["memory:2"], "added": 2.0})))
    twin = h.mem(101, held, kind="reflection", source="reflection", t=h.T - 3 * DAY)
    h.clock.set(h.T + RUN)
    again = "I step up and lead when nobody will."
    fresh = "I keep leaving raids early for bakery shift work."
    ctx = h.ctx({"questions": [BAKERY_Q], "judge": [yes], "reflection": [insight(text, again, fresh, fresh)],
                 "supersede": [{"decisions": [{"id": twin, "relation": "duplicates"}]}],
                 "core_judge": [{"verdict": "extension", "why": ""}]}, default=by_title(supersede={"decisions": []}))
    run(reflect_member(ctx, 101))
    (judged,) = calls_titled(ctx, "core_judge")
    assert fresh in judged["messages"][-1]["content"]
    assert [n["text"] for n in h.store.get_state(101).self_notes][-1] == fresh and len(h.store.get_state(101).self_notes) == 3
    assert h.store.get_memories([twin])[0].merged_n == 2                       # the restated one folded into its twin


def test_goals_are_the_first_three_open_plans_and_stay_without_one(h):
    seed_day(h)
    goals = ("Restock flasks tomorrow.", "Sort the bank tomorrow.", "Farm herbs tomorrow.", "Fix my addons tomorrow.")
    plans = [out("plan", None, text, [3]) for text in goals]
    run(reflect_member(h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection(plans)]}), 101))
    assert h.store.get_state(101).goals == goals[:3]
    assert len([m for m in reflections(h) if m.kind == "plan"]) == 4               # every plan is a memory
    for text in ("Next day, a quiet one.", "Logged on late.", "Logged off early."):
        h.mem(101, text, t=h.T + DAY - 600)
    h.clock.set(h.T + DAY + RUN)
    run(reflect_member(h.ctx({"questions": [Q], "judge": [yes], "reflection": [reflection()]}, night="2026-10-30"), 101))
    assert h.store.get_state(101).goals == goals[:3]
