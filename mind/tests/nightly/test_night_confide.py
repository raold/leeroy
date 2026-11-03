"""The nightly re-pass over whispers (P1c Task 8): what was said in confidence becomes the ledger's
facts, how far a member opened up to him and how he answered become disclosure rows and trust,
and a guild line that asks for secrecy stays public (controller ruling B2). Fakes only: a FakeLLM
answers for the reflection model. Every name and line here is invented."""
import json
from dataclasses import replace

import pytest

from conftest import make_mem, run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.confide import (CONFIDE_SCHEMA, DEPTHS, DISMISSED_DAYS, DISMISSED_TRUST,
                                         HEARD_TRUST, confidential_threads, depth_with,
                                         stage_confide)
from leeroy_mind.nightly.knowledge import (believes_knows, is_private, is_secret, knowers,
                                           may_tell)
from leeroy_mind.nightly.llm import check_schema
from nightly.conftest import no_real_name, yes

DAY = 86400.0
NONE = {"disclosure": {"depth": "none", "evidence": []}, "response": {"kind": "none", "evidence": []}}


def thread(h, lines, *, t=None, status="extracted"):
    """The (from, to, channel, text) lines stored one minute apart from `t` (h.T by default),
    made one chat_thread closed in the window with `status`; the clock is left at its close.
    Returns (tid, line_ids)."""
    t0 = h.T if t is None else t
    ids = [h.say(t0 + 60 * i, frm, text, channel=channel, to=to)
           for i, (frm, to, channel, text) in enumerate(lines)]
    frm, to, channel, _ = lines[0]
    key = f"whisper:{min(frm, to)}:{max(frm, to)}" if channel == "whisper" else channel
    tid = h.store.open_thread(key, t0)
    h.store.set_thread(ids, tid)
    closed = t0 + 60 * len(lines) + 90
    h.store.close_thread(tid, closed, status)
    h.clock.set(closed)
    return tid, ids


def whisper_thread(h, lines, *, member=None, t=None, status="extracted"):
    """The plan's helper: the (guid, text) lines as whispers between Testmage and the member (the
    one guid in `lines` that is not his, or `member`) at one-minute steps, one chat_thread closed
    in the window with status `extracted`. Returns (tid, line_ids)."""
    member = member or next(g for g, _ in lines if g != h.human)
    return thread(h, [(g, member if g == h.human else h.human, "whisper", text)
                      for g, text in lines], t=t, status=status)


def members_thread(h, a, b, texts, *, channel, t=None):
    """Two members' lines, a's first and then by turns, on `channel` (a whisper goes to the other
    one; any other channel to nobody)."""
    pair = (a, b)
    return thread(h, [(pair[i % 2], pair[1 - i % 2] if channel == "whisper" else 0, channel, text)
                      for i, text in enumerate(texts)], t=t)


def by_line(replies: dict[str, dict]):
    """A scripted default: each confide call is answered by the reply whose key is a line its
    prompt shows, so the order the units' calls arrive in does not matter."""
    return lambda text: next(r for line, r in replies.items() if line in text)


def knows_rows(h, root: str) -> dict[int, tuple[str, int]]:
    return {r["guid"]: (r["how"], r["may_tell"]) for r in h.store.conn.execute(
        "SELECT guid, how, may_tell FROM knows WHERE root = ?", (root,))}


def disclose_memories(h, guid: int):
    return [m for m in h.rows(guid) if (m.key or "").startswith("disclose:")]


# -- the plan's tests ----------------------------------------------------------------------------

def test_whisper_thread_yields_a_secret_fact(h):
    h.woman(101, "drawn")
    tid, ids = whisper_thread(h, [(101, "dont tell anyone but belra failed her exam again"), (1, "ouch, my lips are sealed")])
    reply = {"facts": [{"kind": "said_about", "about": "Belra", "text": "Belra failed her exam again.", "evidence": [1]}],
             "disclosure": {"depth": "none", "evidence": []}, "response": {"kind": "none", "evidence": []}}
    run(stage_confide(h.ctx({"confide": [reply], "judge": [yes]})))
    f = h.store.conn.execute("SELECT kind, root, about, secret FROM fact").fetchone()
    assert tuple(f) == ("said_about", f"chat:{ids[0]}", 102, 1)
    assert knowers(h.store, f"chat:{ids[0]}") == {101} and not may_tell(h.store, 101, f"chat:{ids[0]}")
    # beyond the plan: she said the line it cites, so she witnessed it
    assert knows_rows(h, f"chat:{ids[0]}") == {101: ("witnessed", 0)} and is_secret(h.store, f"chat:{ids[0]}")


def test_disclosure_and_his_answer(h):
    h.woman(101, "drawn")
    tid, ids = whisper_thread(h, [(101, "honestly my dad left when i was 12, raiding is kind of my family"),
                                  (1, "that's rough. glad you're here with us")])
    reply = {"facts": [], "disclosure": {"depth": "middle", "evidence": [1]}, "response": {"kind": "responsive", "evidence": [2]}}
    run(stage_confide(h.ctx({"confide": [reply]})))
    assert depth_with(h.store, 101, 1, now=h.clock.now()) == 2
    assert [tuple(r) for r in h.store.conn.execute("SELECT dim, delta, applied FROM rel_delta")] == [("trust", 3.0, 0)]
    assert [m.text for m in h.rows(101) if m.key == f"disclose:{tid}"] == ["Told Testy something real. He listened."]
    # beyond the plan: the rows' shape, and the memory private to him through its whisper evidence (N4, B2)
    refs = [f"chat:{ids[0]}", f"chat:{ids[1]}"]
    (d,) = h.store.conn.execute("SELECT guid, other, depth, response, layer, ref FROM disclosure").fetchall()
    assert tuple(d) == (101, 1, 2, "responsive", None, refs[0])
    (delta,) = h.store.conn.execute("SELECT a, b, reason, evidence FROM rel_delta").fetchall()
    assert tuple(delta) == (101, 1, "disclosure heard", f'["{refs[0]}", "{refs[1]}"]')
    (m,) = disclose_memories(h, 101)
    assert (m.kind, m.source, m.importance, m.valence, m.actors, m.evidence) == (
        "conversation", "reflection", 6, 0.5, (1,), tuple(refs))
    assert (m.scope, m.scope_with) == ("private", 1) and m.embedding is not None


def test_dismissed_disclosure_caps_depth(h):
    h.woman(101, "drawn")
    with h.store.transaction():                                      # an earlier deep disclosure he listened to
        h.store.conn.execute("INSERT INTO disclosure(guid, other, depth, response, ts, ref) "
                             "VALUES (101, 1, 3, 'responsive', ?, 'chat:999')", (h.T - 30 * DAY,))
    assert depth_with(h.store, 101, 1, now=h.T) == 3
    tid, ids = whisper_thread(h, [(101, "work was rough, my boss yelled at me again"), (1, "k. you coming to raid or not")])
    reply = {"facts": [], "disclosure": {"depth": "surface", "evidence": [1]}, "response": {"kind": "dismissive", "evidence": [2]}}
    run(stage_confide(h.ctx({"confide": [reply]})))
    assert [tuple(r) for r in h.store.conn.execute("SELECT a, b, dim, delta, reason, applied FROM rel_delta")] == [
        (101, 1, "trust", -2.0, "disclosure dismissed", 0)]
    (d,) = h.store.conn.execute("SELECT depth, response, ts, ref FROM disclosure WHERE ref != 'chat:999'").fetchall()
    assert (d["depth"], d["response"], d["ref"]) == (1, "dismissive", f"chat:{ids[0]}")
    then = d["ts"]
    assert depth_with(h.store, 101, 1, now=then) == 2                # held back a layer...
    assert depth_with(h.store, 101, 1, now=then + 14 * DAY - 1) == 2  # ...for 14 days
    assert depth_with(h.store, 101, 1, now=then + 14 * DAY) == 3      # and 3 again after
    assert depth_with(h.store, 101, 103, now=then) == 0 and depth_with(h.store, 102, 1, now=then) == 0
    assert disclose_memories(h, 101) == []                           # nobody listened
    with h.store.transaction():                                      # dismissed, and nothing he listened to before
        h.store.conn.execute("INSERT INTO disclosure(guid, other, depth, response, ts, ref) "
                             "VALUES (102, 1, 1, 'dismissive', ?, 'chat:998')", (then,))
    assert depth_with(h.store, 102, 1, now=then) == 0                # never below 0


def test_labels_must_cite_the_right_speaker(h):
    a, _ = whisper_thread(h, [(101, "i failed my driving test today"), (1, "aw no, you'll pass next time")])
    b, (sick, _, _) = whisper_thread(h, [(102, "my brother is in hospital again"), (1, "sorry to hear that, is he ok?"),
                                         (102, "they think so, thanks")], t=h.T + 600)
    c, (_, cat, _) = whisper_thread(h, [(1, "you ok? you seem off"), (104, "not really, my cat died this morning"),
                                        (1, "damn, sorry")], member=104, t=h.T + 1200)
    whisper_thread(h, [(105, "money is tight this month"), (1, "same tbh")], t=h.T + 1800)
    replies = {
        "driving test": {"facts": [], "disclosure": {"depth": "surface", "evidence": [2]},       # his line
                         "response": {"kind": "responsive", "evidence": [2]}},
        "brother is in hospital": {"facts": [], "disclosure": {"depth": "middle", "evidence": [1]},
                                   "response": {"kind": "responsive", "evidence": [3]}},   # her line, after it
        "cat died": {"facts": [], "disclosure": {"depth": "middle", "evidence": [2]},
                     "response": {"kind": "responsive", "evidence": [1]}},                  # his, but before it
        "money is tight": {"facts": [], "disclosure": {"depth": "middle", "evidence": []},    # no lines at all
                           "response": {"kind": "responsive", "evidence": [2]}}}
    run(stage_confide(h.ctx(default=by_line(replies))))
    log = {(r["owner"], r["kind"]): (r["verdict"], r["check_stage"], r["reason"]) for r in h.store.conn.execute(
        "SELECT owner, kind, verdict, check_stage, reason FROM grounding_log WHERE stage = 'confide'")}
    assert log == {(101, "disclosure"): ("rejected", "deterministic", "disclosure not theirs"),
                   (105, "disclosure"): ("rejected", "deterministic", "disclosure not theirs"),
                   (102, "disclosure"): ("accepted", "deterministic", ""),
                   (102, "response"): ("rejected", "deterministic", "response not his"),
                   (104, "disclosure"): ("accepted", "deterministic", ""),
                   (104, "response"): ("rejected", "deterministic", "response not his")}
    rows = h.store.conn.execute("SELECT guid, depth, response, ref FROM disclosure ORDER BY guid").fetchall()
    assert [tuple(r) for r in rows] == [(102, 2, "none", f"chat:{sick}"), (104, 2, "none", f"chat:{cat}")]
    assert h.count("rel_delta") == 0 and all(disclose_memories(h, g) == [] for g in (101, 102, 104, 105))
    assert depth_with(h.store, 102, 1, now=h.clock.now()) == 0      # no responsive answer: no depth with him


def test_guild_line_asking_for_secrecy_stays_public(h):           # controller ruling B2
    lines = ["between us i'm switching mains", "my lips are sealed"]
    _, (said, _) = members_thread(h, 103, 104, lines, channel="guild")
    ctx = h.ctx()
    run(stage_confide(ctx))
    assert ctx.reasoner.llm.calls == [] and h.count("fact") == 0    # not re-read
    assert not is_secret(h.store, f"chat:{said}")
    mid = h.ctx().writer.put(make_mem(owner=104, kind="conversation", text="Coros is switching mains.",
                                      actors=(103,), source="reflection", evidence=(f"chat:{said}",),
                                      scope="secret", scope_with=103))
    (m,) = h.store.get_memories([mid])
    assert (m.scope, m.scope_with) == ("open", 0)                    # guild chat cannot be made secret

    wt, (told, _) = members_thread(h, 103, 104, lines, channel="whisper", t=h.T + 600)
    assert confidential_threads(h.store, ctx.window, 1) == [wt]
    reply = {"facts": [{"kind": "confidential", "about": "Coros", "text": "Coros is switching mains.", "evidence": [1]}],
             "disclosure": {"depth": "middle", "evidence": [1]}, "response": {"kind": "responsive", "evidence": [2]}}
    run(stage_confide(h.ctx({"confide": [reply], "judge": [yes]})))
    root = f"chat:{told}"
    assert [tuple(r) for r in h.store.conn.execute("SELECT kind, root, about, secret FROM fact")] == [
        ("whisper_scene", root, 103, 1)]                             # about the one who said it: Coros himself
    assert knows_rows(h, root) == {103: ("witnessed", 0), 104: ("confided", 0)}
    assert believes_knows(h.store, 103, 104, root) == "yes" == believes_knows(h.store, 104, 103, root)
    assert is_secret(h.store, root) and not may_tell(h.store, 104, root)
    # disclosures are to him: two members' labels are not read
    assert h.count("disclosure") == 0 and h.count("rel_delta") == 0 and disclose_memories(h, 103) == []
    assert [r[0] for r in h.store.conn.execute("SELECT kind FROM grounding_log")] == ["fact"]

    members_thread(h, 103, 104, ["wanna run heroics later", "sure, after dinner"], channel="whisper", t=h.T + 1200)
    assert confidential_threads(h.store, ctx.window, 1) == [wt]      # two members' plain whisper: not re-read
    again = h.ctx()
    run(stage_confide(again))
    assert again.reasoner.llm.calls == []


def test_plain_whisper_news_is_private_not_secret(h):              # controller ruling I8
    h.woman(101, "fond")
    tid, (news, congrats) = whisper_thread(h, [(1, "got the job btw, start monday"), (101, "omg congrats!!")])
    reply = {"facts": [{"kind": "life_news", "about": "Testy", "text": "Testy got the job and starts Monday.",
                        "evidence": [2, 1]}], **NONE}
    run(stage_confide(h.ctx({"confide": [reply], "judge": [yes]})))
    root = f"chat:{news}"                                            # the first line it cites, in the thread's order
    assert [tuple(r) for r in h.store.conn.execute("SELECT kind, root, about, secret FROM fact")] == [
        ("life_news", root, 1, 0)]
    assert knows_rows(h, root) == {101: ("confided", 1)}             # not every line it cites is hers
    assert not is_secret(h.store, root) and is_private(h.store, root) and is_private(h.store, f"chat:{congrats}")
    assert knowers(h.store, root) == {101} and may_tell(h.store, 101, root)   # nobody else knows it


def test_real_name_never_stored(h):
    tid, ids = whisper_thread(h, [(101, "night Quillon"), (1, "night, sleep well")])
    reply = {"facts": [{"kind": "life_news", "about": "Testy", "text": "Quillon is a nurse.", "evidence": [1]}], **NONE}
    ctx = h.ctx({"confide": [reply]})
    run(stage_confide(ctx))
    (call,) = [c for c in ctx.reasoner.llm.calls if "messages" in c]   # the one confide call: no judge
    shown = "\n".join(m["content"] for m in call["messages"])
    assert "night Testy" in shown and "Quillon" not in shown
    log = h.store.conn.execute("SELECT kind, text, verdict, reason FROM grounding_log").fetchall()
    assert [tuple(r) for r in log] == [("fact", "[name] is a nurse.", "rejected", "real name")]
    assert h.count("fact") == 0 and no_real_name(h.store) == []


# -- beyond the plan -----------------------------------------------------------------------------

def test_the_schema_holds_the_plans_values():
    props = CONFIDE_SCHEMA["properties"]
    fact = props["facts"]["items"]["properties"]
    assert CONFIDE_SCHEMA["title"] == "confide" and props["facts"]["maxItems"] == 3
    assert fact["kind"]["enum"] == ["said_about", "life_news", "confidential"] and fact["about"]["type"] == ["string", "null"]
    assert fact["text"]["maxLength"] == 160 and fact["evidence"]["minItems"] == 1
    assert props["disclosure"]["properties"]["depth"]["enum"] == ["none", "surface", "middle", "deep"]
    assert props["response"]["properties"]["kind"]["enum"] == ["responsive", "neutral", "dismissive", "none"]
    one = {"kind": "life_news", "about": None, "text": "x", "evidence": [1]}
    check_schema(CONFIDE_SCHEMA, {"facts": [one] * 3, **NONE})
    for bad in ({"facts": [one] * 4, **NONE}, {"facts": [{**one, "evidence": []}], **NONE},
                {"facts": [{**one, "text": "x" * 161}], **NONE}):
        with pytest.raises(ValueError):
            check_schema(CONFIDE_SCHEMA, bad)
    assert (DEPTHS, HEARD_TRUST, DISMISSED_TRUST, DISMISSED_DAYS) == (
        {"surface": 1, "middle": 2, "deep": 3}, {1: 2.0, 2: 3.0, 3: 4.0}, -2.0, 14)


@pytest.mark.parametrize("depth,kind,delta,memory", [("surface", "responsive", 2.0, False),
                                                     ("deep", "responsive", 4.0, True),
                                                     ("middle", "neutral", None, False)])
def test_his_answer_by_depth(h, depth, kind, delta, memory):
    _, ids = whisper_thread(h, [(102, "so yeah, that was my week"), (102, "kind of a lot honestly"),
                                (1, "glad you told me")])
    reply = {"facts": [], "disclosure": {"depth": depth, "evidence": [2, 1]}, "response": {"kind": kind, "evidence": [3]}}
    run(stage_confide(h.ctx({"confide": [reply]})))
    refs = [f"chat:{i}" for i in ids]
    assert [tuple(r) for r in h.store.conn.execute("SELECT depth, response, ref FROM disclosure")] == [
        (DEPTHS[depth], kind, refs[0])]                              # its first line, in the thread's order
    assert [(r[0], r[1], json.loads(r[2])) for r in h.store.conn.execute("SELECT delta, reason, evidence FROM rel_delta")] == (
        [] if delta is None else [(delta, "disclosure heard", refs)])
    assert [m.evidence for m in disclose_memories(h, 102)] == ([tuple(refs)] if memory else [])   # a middle or deep one he listened to


def test_a_fact_called_confidential_is_secret_in_a_plain_whisper(h):
    whisper_thread(h, [(101, "thinking of leaving my job, havent told my parents"), (1, "your call, good luck")])
    reply = {"facts": [{"kind": "confidential", "about": "Arkon", "text": "Arkon is thinking of leaving their job.",
                        "evidence": [1]}], **NONE}
    run(stage_confide(h.ctx({"confide": [reply], "judge": [yes]})))
    (f,) = h.store.conn.execute("SELECT kind, root, about, secret FROM fact").fetchall()
    assert (f["kind"], f["about"], f["secret"]) == ("whisper_scene", 101, 1)   # no secrecy asked, but he called it so
    assert knows_rows(h, f["root"]) == {101: ("witnessed", 0)} and is_secret(h.store, f["root"])


def test_which_threads_are_re_read_and_the_count(h):
    old, _ = whisper_thread(h, [(101, "you still up?")], t=h.T - DAY)           # closed the night before
    his, _ = whisper_thread(h, [(101, "you around?"), (1, "yep")], t=h.T)
    skipped, _ = whisper_thread(h, [(1, "gz on the mount")], member=102, t=h.T + 600, status="skipped")
    whisper_thread(h, [(103, "brb")], t=h.T + 1200, status="pending")          # not extracted yet
    hush, _ = members_thread(h, 104, 103, ["you ok?", "keep it quiet but im respeccing"], channel="whisper",
                             t=h.T + 1800)                                     # Coros asks, in the second line
    members_thread(h, 103, 105, ["wanna run heroics", "sure"], channel="whisper", t=h.T + 2400)
    members_thread(h, 103, 104, ["between us i'm switching mains", "my lips are sealed"], channel="guild",
                   t=h.T + 3000)
    members_thread(h, 103, 200, ["between us i'm switching mains", "k"], channel="whisper",
                   t=h.T + 3600)                                               # 200 is not in the cast
    whisper_thread(h, [(200, "got a sec?"), (1, "sure")], t=h.T + 4200)
    thread(h, [(103, 104, "officer", "between us i'm switching mains"),
               (101, 1, "say", "honestly my dad left when i was 12")], t=h.T + 4800)   # addressed, not whispered
    ctx = h.ctx()
    assert confidential_threads(h.store, ctx.window, 1) == [hush, skipped, his]   # newest first
    assert confidential_threads(h.store, ctx.window, 1, limit=2) == [hush, skipped]
    assert confidential_threads(h.store, (h.T - 2 * DAY, h.T - DAY + 3600), 1) == [old]
    assert budget.COUNTERS["confide"](ctx) == 3                                 # registered at import
    ctx.settings = replace(ctx.settings, confide_per_night=1)
    assert budget.COUNTERS["confide"](ctx) == 1
    seen = h.ctx(default=lambda text: {"facts": [], **NONE})
    run(stage_confide(seen))
    calls = [c for c in seen.reasoner.llm.calls if "messages" in c]
    assert len(calls) == 3 and all(c["format"] == CONFIDE_SCHEMA and c["options"]["temperature"] == 0.2
                                   for c in calls)
    system = next(c["messages"][0]["content"] for c in calls if "you around?" in c["messages"][-1]["content"])
    assert system.startswith("Read this conversation the way Arkon remembers it. List only what was said in "
                             "confidence or is personal news, who it is about, how far Arkon opened up to Testy, "
                             "and how Testy answered.")
    secret = next(c["messages"] for c in calls if "respeccing" in c["messages"][-1]["content"])
    assert secret[0]["content"].startswith("Read this conversation the way Coros remembers it.")   # who asked
    assert "Testy is not in this conversation" in secret[-1]["content"]


# -- fix I1: an ask covers the pair's later threads that night -----------------------------------

def test_an_ask_covers_the_secret_told_in_the_next_thread(h):         # review-task8 I1
    # P1b cut the thread between his ask and the secret (90 s of silence): it was still told in
    # confidence, so its fact is secret and nobody may pass it on (amendment A4)
    whisper_thread(h, [(1, "can i tell you something? between us"), (101, "of course, go on")])
    _, (moving, _) = whisper_thread(h, [(1, "im moving to another city next month"), (101, "wow, ok")],
                                    t=h.T + 600)
    replies = {"between us": {"facts": [], **NONE},
               "another city": {"facts": [{"kind": "life_news", "about": "Testy", "evidence": [1],
                                           "text": "Testy is moving to another city next month."}], **NONE}}
    run(stage_confide(h.ctx({"judge": [yes]}, default=by_line(replies))))
    root = f"chat:{moving}"
    assert [tuple(r) for r in h.store.conn.execute("SELECT kind, root, about, secret FROM fact")] == [
        ("life_news", root, 1, 1)]
    assert knows_rows(h, root) == {101: ("confided", 0)}
    assert is_secret(h.store, root) and not may_tell(h.store, 101, root)
