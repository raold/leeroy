"""Grounding the night's claims (P1c Task 6): the deterministic rules, the judge's policy, the
evidence a model is shown and retrieval that touches nothing. Every name here is invented."""
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import run
from leeroy_mind.nightly.evidence import (STOPWORDS, Evidence, Item, content_words, load_facts,
                                          load_items, render_event, retrieve)
from leeroy_mind.nightly.ground import (CHECK_ONLY, MAX_LEN, Claim, Verdict, check, ground,
                                        log_verdicts, screen_text)
from nightly.conftest import IDENT, START, no_real_name, yes

T = START.timestamp()


@pytest.fixture
def facts(h):
    f = load_facts(h.ctx())
    assert {"sindragosa", "the lich king"} <= f.bosses
    return f


@pytest.fixture
def ev():
    e = Evidence()
    e.add(Item("memory:10", "Wiped 6× on Sindragosa with Belra.", frozenset({101, 102}), T,
               knowers=frozenset({101, 102})))
    e.add(Item("memory:11", "Testy thanked me for the heal.", frozenset({101, 1}), T,
               knowers=frozenset({101})))
    return e


def test_check_rules(facts, ev):
    ok = Claim("belief", 101, "Belra keeps her cool on Sindragosa.", ["memory:10"], about=102)
    assert check(ok, ev, facts).ok
    assert check(replace(ok, evidence=["memory:99"]), ev, facts).reason == "unknown evidence"
    assert check(replace(ok, text="Coros keeps his cool."), ev, facts).reason == "name not in evidence: coros"
    assert check(replace(ok, text="We wiped 7 times on Sindragosa."), ev, facts).reason == "number not in evidence"
    assert check(replace(ok, text="Belra is great on The Lich King."), ev, facts).reason == "boss/item not in evidence"
    assert check(replace(ok, text="Belra sounds like a language model."), ev, facts).reason == "meta"   # P1b's META_RE (ruling C4)
    assert check(replace(ok, about=103), ev, facts).reason == "about not in evidence"
    rd = Claim("rel_delta", 101, "She held aggro.", ["memory:10"], about=102, dim="trust", delta=3)
    assert check(rd, ev, facts).ok
    assert check(replace(rd, dim="attraction", evidence=["memory:11"], about=1), ev, facts).reason == "attraction moves only by acts"
    assert check(replace(rd, delta=12), ev, facts).reason == "bad delta"
    assert check(Claim("life", 101, "My sister visits in 3 weeks.", ["memory:10"]), ev, facts).ok
    # PR6: rule 7 reads the cast's names in any case, as typed chat has them
    assert check(replace(ok, text="belra has a crush on arkon"), ev, facts).reason == "romance between members"
    assert check(replace(ok, text="belra flirts with testy lol"), ev, facts).ok


def test_real_name_member_romance_and_leaving(h, facts, ev):
    base = Claim("belief", 101, "x", ["memory:11"], about=1)
    for text in ("Quillon was great tonight.", "QUILLON was great tonight."):
        assert check(replace(base, text=text), ev, facts).reason == "real name"
    assert check(replace(base, text="quillon was great tonight."), ev, facts).ok           # the lower-case word: P1b's ruling
    assert check(Claim("belief", 101, "I have a crush on Belra.", ["memory:10"], about=102), ev, facts).reason == "romance between members"
    assert check(Claim("belief", 101, "Belra flirts with Testy all night and I hate it.", ["memory:10", "memory:11"], about=102), ev, facts).ok
    assert check(Claim("life", 101, "I'm leaving the guild after this tier.", ["memory:10"]), ev, facts).reason == "leaves the guild"
    assert check(Claim("wowlife", 101, "Said I'd quit after Lich King. Again.", ["memory:10"]), ev, facts).ok
    with h.store.transaction():
        log_verdicts(h.store, "n", "reflect", [(replace(base, text="Quillon was great."), Verdict(False, "deterministic", "real name"))], 1.0, IDENT)
    assert no_real_name(h.store) == [] and h.count("grounding_log") == 1


def test_tom_and_gossip_rules(h, facts, ev):
    tom = Claim("tom", 101, "Testy has no idea how close that was.", ["memory:10", "memory:11"], about=1)
    assert check(tom, ev, facts).ok                                           # Testy is not among memory:10's knowers
    assert check(replace(tom, text="Belra has no idea.", about=102, evidence=["memory:10"]), ev, facts).reason == "tom not supported"
    assert check(replace(tom, about=None), ev, facts).reason == "tom not supported"
    rumour = Claim("gossip", 101, "Belra wiped us like 9 times on Sindragosa.", ["memory:10"], about=102)
    assert check(rumour, ev, facts).ok                                        # 9 lies within the band of 6 (3 to 9)
    assert check(replace(rumour, text="Belra wiped us like 10 times on Sindragosa."), ev, facts).reason == "number outside the band"
    assert check(replace(rumour, text="Belra wiped us on The Lich King."), ev, facts).reason == "boss/item not in evidence"
    assert check(replace(rumour, text="Coros wiped us 6 times."), ev, facts).reason == "name not in evidence: coros"
    # review-task11 I2: rule 12 counts whoever could have seen the root (knowledge.could_have_seen),
    # as Task 11's who-knows-what does: a line's audience, an event's members, and him
    line = h.say(T, 102, "coros and dunna pulled early again lol", channel="raid")
    h.say(T + 60, 103, "my bad", channel="raid")                              # Coros typed: in the line's audience
    kill = h.boss(T, 631, 1, "killed", [1, 101, 102])                        # his raid
    seen = Evidence()
    for item in load_items(h.store, [f"chat:{line}", f"event:{kill}"], ident=IDENT):
        seen.add(item)
    pulled = Claim("tom", 101, "Coros doesn't know they pulled early.", [f"chat:{line}"], about=103)
    assert check(pulled, seen, facts).reason == "tom not supported"           # in the audience
    assert check(replace(pulled, text="Dunna doesn't know they pulled early.", about=104), seen, facts).ok   # was not
    raid = Claim("tom", 101, "Testy doesn't know how close it was.", [f"event:{kill}"], about=1)
    assert check(raid, seen, facts).reason == "tom not supported"             # a raid he was in
    assert check(replace(raid, text="Belra doesn't know how close it was.", about=102), seen, facts).reason == (
        "tom not supported")                                                  # a member of the raid
    loot = h.loot(T, 103, "Frozen Shard")
    for item in load_items(h.store, [f"event:{loot}"], ident=IDENT):
        seen.add(item)
    won = Claim("tom", 101, "Coros doesn't know what dropped.", [f"event:{loot}"], about=103)
    assert check(won, seen, facts).reason == "tom not supported"              # the event was his
    # and about him over anything: the ledger never holds him, so it cannot say he does not know
    told = h.say(T, 102, "testy is quitting his job, dont tell him", channel="whisper", to=101)
    heard = h.mem(101, "Belra told me Testy is quitting his job.", actors=(102, 1), evidence=(f"chat:{told}",))
    rootless = h.mem(101, "Testy seemed tired.", actors=(1,), evidence=("memory:9999",))
    for item in load_items(h.store, [f"memory:{heard}", f"memory:{rootless}"], ident=IDENT):
        seen.add(item)
    quitting = Claim("tom", 101, "Testy doesn't know Belra told me.", [f"memory:{heard}"], about=1)
    assert check(quitting, seen, facts).reason == "tom not supported"         # a whisper between two members
    assert check(replace(quitting, text="Testy doesn't know I noticed.", evidence=[f"memory:{rootless}"]),
                 seen, facts).reason == "tom not supported"                   # a memory resting on no root


def test_chat_items_redact_real_name(h):
    cid = h.say(h.T, 101, "night Quillon, good raid")
    item = load_items(h.store, [f"chat:{cid}"], ident=IDENT)[0]
    assert item.text == "Arkon: night Testy, good raid"


def test_judge_policy(h, facts, ev):
    reply = {"verdicts": [{"i": 0, "support": "partly", "invariants_ok": True, "why": ""},
                          {"i": 1, "support": "partly", "invariants_ok": True, "why": ""},
                          {"i": 2, "support": "yes", "invariants_ok": False, "why": ""}]}
    ctx = h.ctx({"judge": [reply]})
    claims = [Claim(k, 101, "Belra tanked Sindragosa.", ["memory:10"], about=102 if k == "belief" else None)
              for k in ("belief", "diary", "plan")]
    assert [v.ok for _, v in run(ground(ctx, claims, ev, facts))] == [False, True, False]


def test_missing_verdict_rejects_and_deterministic_rejects_skip_judge(h, facts, ev):
    ctx = h.ctx({"judge": [{"verdicts": []}]})
    pairs = run(ground(ctx, [Claim("diary", 101, "Long night.", ["memory:10"]), Claim("diary", 101, "x", ["memory:99"])], ev, facts))
    assert [v.ok for _, v in pairs] == [False, False] and pairs[1][1].check == "deterministic"
    prompt = ctx.reasoner.llm.calls[0]["messages"][-1]["content"]
    assert len(ctx.reasoner.llm.calls) == 1 and "ITEM 0" in prompt and "ITEM 1" not in prompt


def test_retrieve_does_not_touch(h):
    a = h.mem(101, "Belra out-healed everyone on Sindragosa."); h.mem(101, "Fished in Dalaran.")
    ctx = h.ctx()
    got = run(retrieve(ctx, 101, "who healed on Sindragosa", 1))
    assert [m.id for m in got] == [a] and h.store.get_memories([a])[0].access_n == 0
    got = run(retrieve(ctx, 101, "a an", 1))                            # fts_query None: vectors only, no error
    assert len(got) == 1 and all(m.owner == 101 and m.status == "active" for m in got)   # PR24


def test_item_actors(h):                                             # review I7: rules 5 and 6 read these
    w = h.say(h.T, 101, "dont tell belra but im rerolling", channel="whisper", to=1)
    g = h.say(h.T + 1, 103, "anyone up for archavon")
    m = h.mem(104, "Ebbo carried us.", actors=(105,))
    items = {i.ref: i.actors for i in load_items(h.store, [f"chat:{w}", f"chat:{g}", f"memory:{m}"], ident=IDENT)}
    assert items == {f"chat:{w}": {101, 1, 102}, f"chat:{g}": {103}, f"memory:{m}": {104, 105}}
    h.boss(h.T + 2, 631, 0, "killed", (101, 102))
    (boss,) = [r[0] for r in h.store.conn.execute("SELECT id FROM game_event WHERE type = 'boss'")]
    assert load_items(h.store, [f"event:{boss}"], ident=IDENT)[0].actors == {101, 102}


def test_evidence_trim_drops_retrieved_first():                     # review M16
    ev = Evidence()
    for i in range(3): ev.add(Item(f"rule:fact:{i}", "w" * 150, frozenset(), 0.0, tier=0))
    for i in range(60): ev.add(Item(f"memory:{i}", "d" * 190, frozenset(), 0.0, tier=1))
    for i in range(60, 90): ev.add(Item(f"memory:{i}", "r" * 190, frozenset(), 0.0, tier=2))
    dropped = ev.trim(14_000, timezone.utc)
    retrieved = [f"memory:{i}" for i in range(60, 90)]
    assert len(ev.render(timezone.utc)) <= 14_000 and dropped == retrieved[::-1][:len(dropped)]   # last added first
    assert all(f"rule:fact:{i}" in ev for i in range(3)) and all(f"memory:{i}" in ev for i in range(60))


# -- beyond the brief's list: the parts no test above reaches ------------------------------------

def test_aliases_rendering_and_what_trim_leaves_unknown():
    ev = Evidence()
    t = datetime(2026, 10, 26, 23, 30, tzinfo=timezone.utc).timestamp()
    assert ev.add(Item("memory:5", "Belra pulled early.", frozenset({102}), t)) == 1
    assert ev.add(Item("chat:9", "Arkon: lol", frozenset({101}), t, tier=2)) == 2
    assert ev.add(Item("chat:9", "Arkon: lol", frozenset({101}), t, tier=0)) == 2   # a ref once
    assert (ev.ref(1), ev.ref(2), ev.ref(3)) == ("memory:5", "chat:9", "?3")
    assert [i.ref for i in ev.items()] == ["memory:5", "chat:9"] and "chat:9" in ev
    # the date is the mind clock's zone, never the host's: 23:30 UTC is already Tuesday at +02
    assert ev.render(timezone.utc) == "[1] (Mon 26 Oct) Belra pulled early.\n[2] (Mon 26 Oct) Arkon: lol"
    assert ev.render(timezone(timedelta(hours=2))).startswith("[1] (Tue 27 Oct) ")
    assert ev.trim(40, timezone.utc) == ["memory:5"]           # the re-add made chat:9 tier 0
    assert "memory:5" not in ev and ev.ref(1) == "?1" and ev.ref(2) == "chat:9"
    assert ev.trim(5, timezone.utc) == [] and "chat:9" in ev   # tier 0 stays, fitting or not


def test_events_and_sessions_render_as_the_night_shows_them(h):
    assert render_event("boss", {"map": 631, "boss_id": 11, "boss_name": "", "state": "killed"}) == "Sindragosa: killed"
    assert render_event("boss", {"map": 631, "boss_id": 12, "boss_name": "", "state": "wiped"}) == "the Lich King: wiped"
    assert render_event("bg_end", {"winner": "horde", "members": [101]}) == "battleground won by horde"
    loot = h.loot(h.T, 102, "Bulwark of Smouldering Steel")
    sid = h.session(h.T, h.T + 2.5 * 3600)
    to = h.say(h.T, 101, "nice pull", channel="guild", to=102)       # only a whisper's other end is an actor
    items = {i.ref: i for i in load_items(h.store, [f"event:{loot}", f"session:{sid}", f"chat:{to}", "event:999", "memory:999"], ident=IDENT)}
    assert set(items) == {f"event:{loot}", f"session:{sid}", f"chat:{to}"}   # unknown refs dropped
    assert items[f"chat:{to}"].actors == {101}
    assert (items[f"event:{loot}"].text, items[f"event:{loot}"].actors) == ("Belra won Bulwark of Smouldering Steel", {102})
    assert (items[f"session:{sid}"].text, items[f"session:{sid}"].actors) == ("Testy was online 2.5 hours", {1})
    facts = load_facts(h.ctx())
    assert facts.items == {"bulwark of smouldering steel"} and facts.human == 1
    ev = Evidence()
    ev.add(items[f"event:{loot}"])
    ev.add(Item("memory:10", "Wiped 6× on Sindragosa with Belra.", frozenset({101, 102}), T))
    won = Claim("belief", 101, "Belra got the Bulwark of Smouldering Steel.", ["memory:10"], about=102)
    assert check(won, ev, facts).reason == "boss/item not in evidence"   # no item is invented either
    assert check(replace(won, evidence=[f"event:{loot}"]), ev, facts).ok
    assert facts.names == {"arkon": 101, "belra": 102, "coros": 103, "dunna": 104, "ebbo": 105, "testmage": 1, "testy": 1}
    assert facts.member_names == ("Arkon", "Belra", "Coros", "Dunna", "Ebbo")


def test_retrieve_keeps_only_active_rows(h):
    h.mem(101, "Belra out-healed everyone on Sindragosa.", status="superseded")
    new = h.mem(101, "Belra out-healed everyone on Sindragosa again.")
    h.mem(102, "Belra out-healed everyone on Sindragosa.")           # another member's
    got = run(retrieve(h.ctx(), 101, "who out-healed everyone on Sindragosa", 2))
    assert [m.id for m in got] == [new]


def test_content_words_leave_out_names_and_stopwords():
    assert content_words("Testy quit his job, Belra got the papers today!", ["Testy", "Belra"]) == {"papers"}
    assert content_words("SHOULD we pull Sindragosa", []) == {"sindragosa"} and "should" in STOPWORDS


def test_the_judge_sees_each_claim_with_its_cited_lines_and_check_only_kinds_skip_it(h, facts, ev):
    ctx = h.ctx(default=yes)
    claims = [Claim("diary", 101, "Long night on Sindragosa.", ["memory:10"]),
              *[Claim(k, 101, "Belra the Wall", ["memory:10"]) for k in sorted(CHECK_ONLY)]]
    pairs = run(ground(ctx, claims, ev, facts))
    assert all(v.ok for _, v in pairs) and [v.check for _, v in pairs] == ["judge", *["deterministic"] * len(CHECK_ONLY)]
    (call,) = ctx.reasoner.llm.calls
    assert call["messages"][0]["content"].startswith("You check whether each claim is supported")
    assert ("ITEM 0: [diary] Long night on Sindragosa.\nEVIDENCE:\n[1] (Thu 29 Oct) Wiped 6× on Sindragosa with Belra."
            in call["messages"][-1]["content"])
    many = [Claim("diary", 101, "Long night.", ["memory:10"]) for _ in range(12)]
    ctx = h.ctx(default=yes)
    assert all(v.ok for _, v in run(ground(ctx, many, ev, facts)))
    texts = [c["messages"][-1]["content"] for c in ctx.reasoner.llm.calls]
    assert len(texts) == 2 and "ITEM 9" in texts[0] and "ITEM 10" not in texts[0] and "ITEM 1:" in texts[1]


def test_invariant_only_kinds_need_only_the_invariants(h, facts, ev):
    def unsupported(prompt: str) -> dict:
        return {"verdicts": [{"i": int(n), "support": "no", "invariants_ok": True, "why": "made up"}
                             for n in dict.fromkeys(re.findall(r"\bITEM (\d+)\b", prompt))]}
    ctx = h.ctx(default=unsupported)
    claims = [Claim(k, 101, "My sister visits soon.", ["memory:10"]) for k in ("life", "break", "wowlife", "belief", "scene")]
    assert [(v.ok, v.label, v.reason) for _, v in run(ground(ctx, claims, ev, facts))] == [
        (True, "no", "made up")] * 3 + [(False, "no", "unsupported: made up")] * 2


def test_the_owner_and_the_evidence_edges(facts, ev):
    assert check(Claim("belief", 101, "Long night.", []), ev, facts).reason == "unknown evidence"
    assert check(Claim("belief", 103, "Coros has Belra's back.", ["memory:10"], about=102), ev, facts).ok   # her own name
    assert check(Claim("belief", 101, "Arkon kept his cool.", ["memory:10"], about=101), ev,
                 facts).reason == "about not in evidence"            # never about herself


def test_limits_and_the_logged_reason_are_screened(h, facts, ev):
    assert (MAX_LEN["scene"], MAX_LEN["nickname"], MAX_LEN["belief"]) == (450, 24, 200)
    assert check(Claim("nickname", 101, "Lord of the Long Pulls Again", ["memory:10"]), ev, facts).reason == "length"
    assert check(Claim("belief", 101, "   ", ["memory:10"]), ev, facts).reason == "length"
    assert check(Claim("belief", 101, "Belra is great on Lich King.", ["memory:10"], about=102), ev,
                 facts).reason == "boss/item not in evidence"           # "the Lich King" without its article
    assert screen_text("Quillon and QUILLON, not quillon", IDENT) == "[name] and [name], not quillon"
    with h.store.transaction():
        log_verdicts(h.store, h.night, "reflect", [(Claim("belief", 101, "fine", ["memory:10"]), Verdict(False, "judge", "no: Quillon is not in it", "no"))], T, IDENT)
    row = h.store.conn.execute("SELECT verdict, check_stage, reason FROM grounding_log").fetchone()
    assert tuple(row) == ("rejected", "judge", "no: [name] is not in it")


def test_a_rumours_number_needs_a_number_to_lean_on(facts, ev):     # review M8
    assert check(Claim("gossip", 101, "Testy healed us 6 times.", ["memory:11"]), ev, facts).reason == "number not in evidence"
    assert facts.terms == facts.bosses | facts.items and facts.terms is facts.terms   # built once, not per claim
