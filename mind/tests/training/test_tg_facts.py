"""Facts never strengthen unverified damage, or attach another pull's history."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.protocol import Boss
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.reactions import FACTS_LOGGED, FACTS_RULE, WIPE_KNOWN, WIPE_LOGGED
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.training import protocol as p, store
from leeroy_mind.training.checks import CheckResult, Failure
from leeroy_mind.training.facts import NO_CAUSE, TalkHook, kill_facts, wipe_facts
from leeroy_mind.training.ledger import PullContext, PullRecord
from leeroy_mind.training.ports import RaidRow


INSTRUCTION = f"The raid wiped. {WIPE_KNOWN} {FACTS_RULE}"


def record(fake, *, outcome="wiped", duration=60, end=None, deaths=0, **over):
    end = int(fake.clock.now() * 1000) - 1000 if end is None else end
    start = end - int(duration * 1000)
    h = p.Pull(**(dict(ts=end, id=f"{start}-77-0", map=631, instance_id=77, boss_id=0,
        difficulty=0, size=10, outcome=outcome, start_ms=start, end_ms=end, members=1,
        deaths=deaths, tracks=0, assisted=False, source="tracked", boss_entry=36612,
        upd_mean_ms=30, damage_basis="requested_pre_ai", spell_basis="best_effort_thread_pair") | over))
    m = p.PullMember(ts=end, pull=h.id, guid=101, class_=8, spec_tab=0, tank=False,
        ilvl=232, damage=100, damage_boss=80, healing=0, overheal=0, taken=10,
        deaths=deaths, interrupts=0, active_ms=0, done=(), taken_by=(), debuffs=(), casts=())
    ds = tuple(p.PullDeath(ts=end, pull=h.id, guid=101, at_ms=1000+i, spell=10,
                          source=36612, x=0, y=0, hits=()) for i in range(deaths))
    return PullRecord(h, (m,), ds, {}, (), True)


def raid(fake, *, offline=False, started=None):
    started = fake.clock.now() - 5000 if started is None else started
    with fake.conn:
        rid = fake.conn.execute(
            "INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
            "VALUES (?,'icc',10,0,?,101,101,101)", (started, int(offline))).lastrowid
    r = RaidRow(rid, offline, 631, 10, False, 101, started)
    fake.raids.append(r)
    return r


def save(fake, rec, *, run=None, kind="his", scoreable=None, checked=False):
    r = run or (fake.raids[-1] if fake.raids else raid(fake))
    ctx = PullContext(kind, 101, r.raid_id, None, None, "", {})
    store.put_pull(fake.conn, rec, ctx, {101: "ranged"}, received=fake.clock.now(), scoreable=scoreable)
    if checked:
        # This represents a future complete analysis, not current tap coverage.
        with fake.conn:
            fake.conn.execute("UPDATE tg_pull SET checks_complete=1 WHERE id=?", (rec.header.id,))
    return rec


def lead(rec, *, kind=None, why=None):
    h = rec.header
    wipe = h.outcome == "wiped"
    return (Turn(kind or (TurnKind.CALLOUT if wipe else TurnKind.KILL), 101, "raid",
                 instruction=INSTRUCTION),
            TurnMeta("facts-test", why=why or ("wipe:lead" if wipe else "kill:lead"),
                     fight_map=h.map, fight_boss=h.boss_id, fight_instance=h.instance_id,
                     fight_end_ms=h.end_ms))


@pytest.mark.parametrize("basis", ["unknown", "best_effort_thread_pair", "invented_verified"])
def test_wipe_does_not_promote_unverified_top_cause(fake, basis):
    rec = record(fake, deaths=4, spell_basis=basis)
    result = CheckResult((), ("avoid:10", "avoid:10", "avoid:10", "unknown"), 0)
    assert wipe_facts(rec, result, {101: "ranged"}, book=None) == ""


def test_unverified_damage_does_not_become_damage_share(fake):
    result = CheckResult((Failure("avoid", "Coldflame", 101, 1, 0, .75),), (), 1)
    assert wipe_facts(record(fake), result, {}, book=None) == ""


def test_no_standout_requires_complete_analysis(fake):
    rec = record(fake)
    assert wipe_facts(rec, CheckResult((), (), 0), {}, book=None) == ""
    assert wipe_facts(rec, CheckResult((), (), 0, complete=True), {}, book=None) == NO_CAUSE


def test_wipe_facts_with_a_change_keeps_it_prospective(fake):
    result = CheckResult((), ("unknown",), 0)
    text = wipe_facts(record(fake, deaths=1), result, {}, book=None, change="ranged spread wider.")
    assert text == "Next pull we change one thing: ranged spread wider."
    assert "Combat log:" not in text


def test_kill_record_only_with_three_priors(fake):
    rec = record(fake, outcome="killed", duration=270)
    assert kill_facts(rec, (300, 290), "Test Boss") == ""
    assert kill_facts(rec, (300, 290, 280), "Test Boss") == "Record: 4:30, our fastest Test Boss kill yet."


def test_kill_no_deaths_and_tied_time(fake):
    rec = record(fake, outcome="killed", duration=270)
    assert kill_facts(rec, (300, 290, 270), "Test Boss") == "Nobody died, 4:30."
    assert kill_facts(record(fake, outcome="killed", duration=270, deaths=1),
                      (300, 290, 270), "Test Boss") == ""


@pytest.mark.parametrize("priors", [(300, 290, float("nan")), (300, 290, -1), (300, 290, True)])
def test_invalid_priors_do_not_supply_the_third_observation(fake, priors):
    assert kill_facts(record(fake, outcome="killed"), priors, "Test Boss") == ""


def test_ineligible_records_never_produce_facts(fake):
    for rec in (replace(record(fake), complete=False), record(fake, outcome="aborted"),
                record(fake, map=0), record(fake, duration=0)):
        assert wipe_facts(rec, CheckResult((), (), 0, complete=True), {}, book=None, change="spread") == ""
        assert kill_facts(rec, (300, 290, 280), "Test Boss") == ""


def test_numbers_only_from_records(fake):
    rec = record(fake, outcome="killed", duration=271)
    text = kill_facts(rec, (300, 290, 280), "Test Boss")
    seconds = (rec.header.end_ms - rec.header.start_ms) // 1000
    assert text == f"Record: {seconds // 60}:{seconds % 60:02d}, our fastest Test Boss kill yet."


def test_hook_only_for_the_leads_calls(fake):
    rec = save(fake, record(fake), checked=True)
    hook = TalkHook(fake)
    for kind, why in ((TurnKind.WIPE_REACT, "wipe_react:weighted_draw"),
                      (TurnKind.CALLOUT, "source:other"), (TurnKind.GZ, "kill_gz:weighted_draw")):
        turn, meta = lead(rec, kind=kind, why=why)
        assert hook(turn, meta, INSTRUCTION) == INSTRUCTION
    turn, meta = lead(rec, why="wipe:weighted_draw")
    assert hook(turn, meta, INSTRUCTION).endswith(NO_CAUSE)


def test_his_pull_adds_no_fact_while_the_gate_measures(fake):
    rec = save(fake, record(fake), checked=True)
    phase = ["ab"]
    hook = TalkHook(fake, live=lambda: phase[0] == "passed")
    assert hook(*lead(rec), INSTRUCTION) == INSTRUCTION
    phase[0] = "passed"
    assert hook(*lead(rec), INSTRUCTION).endswith(NO_CAUSE)


def test_fact_swaps_the_wipe_wording_only_with_complete_checks(fake):
    rec = save(fake, record(fake), checked=True)
    text = TalkHook(fake)(*lead(rec), INSTRUCTION)
    assert WIPE_LOGGED in text and FACTS_LOGGED in text
    assert WIPE_KNOWN not in text and FACTS_RULE not in text
    assert text.endswith(NO_CAUSE)


def test_current_unknown_analysis_keeps_original_guards(fake):
    rec = save(fake, record(fake, deaths=1))
    result = CheckResult((), ("unknown",), 0, ("deaths:unverified",), complete=False)
    assert wipe_facts(rec, result, {}, book=None) == ""
    assert TalkHook(fake)(*lead(rec), INSTRUCTION) == INSTRUCTION


def test_change_without_facts_keeps_original_guards(fake):
    rec = save(fake, record(fake))
    seen = []
    def change(context):
        seen.append(context)
        return "ranged spread wider"
    text = TalkHook(fake, change_for=change)(*lead(rec), INSTRUCTION)
    assert seen == ["631:0:0"]
    assert text == INSTRUCTION + " Next pull we change one thing: ranged spread wider."


@pytest.mark.parametrize("kind", ["offline", "benchmark"])
def test_hook_ignores_non_his_pulls(fake, kind):
    rec = save(fake, record(fake), kind=kind, checked=True)
    assert TalkHook(fake)(*lead(rec), INSTRUCTION) == INSTRUCTION


@pytest.mark.parametrize("age", [-1, 121, 180])
def test_hook_ignores_old_and_future_events(fake, age):
    rec = save(fake, record(fake, end=int(fake.clock.now() * 1000) - age * 1000), checked=True)
    assert TalkHook(fake)(*lead(rec), INSTRUCTION) == INSTRUCTION


def test_no_record_keeps_wipe_known(fake):
    rec = record(fake)
    raid(fake)
    assert TalkHook(fake)(*lead(rec), INSTRUCTION) == INSTRUCTION


@pytest.mark.parametrize("field,value", [("fight_map", 649), ("fight_boss", 1),
    ("fight_instance", 78), ("fight_end_ms", 0)])
def test_hook_requires_matching_event_identity(fake, field, value):
    rec = save(fake, record(fake), checked=True)
    turn, meta = lead(rec)
    setattr(meta, field, value)
    assert TalkHook(fake)(turn, meta, INSTRUCTION) == INSTRUCTION


def test_recent_previous_pull_cannot_supply_missing_current_log(fake):
    old = save(fake, record(fake, end=int(fake.clock.now() * 1000) - 6000), checked=True)
    current = record(fake, end=old.header.end_ms + 5000)
    assert TalkHook(fake)(*lead(current), INSTRUCTION) == INSTRUCTION


def test_other_current_raid_cannot_use_previous_raid_log(fake):
    rec = save(fake, record(fake), checked=True)
    raid(fake, started=fake.clock.now() - .5)
    assert TalkHook(fake)(*lead(rec), INSTRUCTION) == INSTRUCTION


@pytest.mark.parametrize("which", ["incomplete", "unscoreable", "different_difficulty", "different_size", "missing_speaker"])
def test_hook_rejects_ineligible_or_mismatched_records(fake, which):
    rec = record(fake)
    if which == "incomplete":
        rec = replace(rec, complete=False)
    elif which == "different_difficulty":
        rec = replace(rec, header=replace(rec.header, difficulty=2))
    elif which == "different_size":
        rec = replace(rec, header=replace(rec.header, size=25))
    save(fake, rec, checked=True, scoreable=False if which == "unscoreable" else None)
    turn, meta = lead(rec)
    if which == "missing_speaker":
        turn.speaker = 999
    assert TalkHook(fake)(turn, meta, INSTRUCTION) == INSTRUCTION


def gate_passed(fake, at=None):
    value = {"phase": "passed", "raids": 4, "verdict_ms": 0.2}
    if at is not None:
        value["passed_at"] = at
    with fake.conn:
        fake.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('tg_gate',?)", (json.dumps(value),))


def test_kill_hook_uses_only_earlier_his_same_context_kills(fake):
    gate_passed(fake)
    rec = record(fake, outcome="killed", duration=270)
    r = raid(fake)
    for i, duration in enumerate((300, 290, 280)):
        prior = record(fake, outcome="killed", duration=duration,
                       end=rec.header.start_ms - (i + 1) * 400_000)
        save(fake, prior, run=r)
    save(fake, rec, run=r)
    text = TalkHook(fake)(*lead(rec), "The raid killed the boss.")
    assert text.endswith("Record: 4:30, our fastest Lord Marrowgar kill yet.")


@pytest.mark.parametrize("phase", ["ab", "passed_later"])
def test_no_record_claim_from_the_gates_sampled_kills(fake, phase):
    rec = record(fake, outcome="killed", duration=270)
    r = raid(fake)
    for i, duration in enumerate((300, 290, 280)):
        save(fake, record(fake, outcome="killed", duration=duration,
                          end=rec.header.start_ms - (i + 1) * 400_000), run=r)
    save(fake, rec, run=r)
    if phase == "ab":
        with fake.conn:
            fake.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('tg_gate',?)",
                              (json.dumps({"phase": "ab", "raids": 2, "verdict_ms": None}),))
    else:  # the priors were stored before the gate passed: a sample, not every kill
        gate_passed(fake, (rec.header.start_ms - 500_000) / 1000)
    text = TalkHook(fake)(*lead(rec), "The raid killed the boss.")
    assert "Record" not in text and "fastest" not in text


def test_kill_without_three_priors_leaves_instruction_unchanged(fake):
    rec = save(fake, record(fake, outcome="killed"))
    assert TalkHook(fake)(*lead(rec), "The raid killed the boss.") == "The raid killed the boss."


def test_lead_planners_preserve_event_identity_and_members_do_not(rig, monkeypatch):
    queued = []
    monkeypatch.setattr(rig.engine, "_queue", lambda items: queued.extend(items))
    monkeypatch.setattr(rig.engine, "_lead_among", lambda members, now: 101)
    monkeypatch.setattr(rig.engine, "_spawn", lambda task: task.close())
    monkeypatch.setattr(rig.engine, "_maybe_whisper", lambda *a, **kw: None)
    now = rig.clock.now()
    for state, method in (("wiped", rig.engine._wipe), ("killed", rig.engine._kill)):
        queued.clear()
        ev = Boss(int(now * 1000), 631, 77, 0, "Test Boss", state, (101, 102))
        method(ev, "Test Boss", [101, 102], now)
        meta = queued[0][1]
        assert (meta.fight_map, meta.fight_instance, meta.fight_boss, meta.fight_end_ms) == (631, 77, 0, ev.ts)
        assert all(m.fight_end_ms == 0 for _, m in queued[1:])


def test_pipeline_applies_hook_text(rig):
    async def main():
        rig.engine.add_instruction_hook(lambda turn, meta, text: text + " HOOK-TEXT.")
        turn = Turn(TurnKind.CALLOUT, 101, "guild", instruction="Base instruction.")
        task = asyncio.create_task(rig.engine.speak(turn))
        await rig.advance(45)
        assert task.done() and task.result()
        assert any("Base instruction. HOOK-TEXT." in prompt for prompt in rig.prompts())
        assert turn.instruction == "Base instruction."
    run(main())


def test_pipeline_survives_a_failing_hook_and_keeps_order(rig, caplog):
    async def main():
        def bad(turn, meta, text):
            turn.instruction = "must not escape"
            raise RuntimeError("must not be logged")
        rig.engine.add_instruction_hook(lambda turn, meta, text: text + " FIRST.")
        rig.engine.add_instruction_hook(bad)
        rig.engine.add_instruction_hook(lambda turn, meta, text: text + " LAST.")
        turn = Turn(TurnKind.CALLOUT, 101, "guild", instruction="Base.")
        task = asyncio.create_task(rig.engine.speak(turn))
        await rig.advance(45)
        assert task.done() and task.result()
        assert any("Base. FIRST. LAST." in prompt for prompt in rig.prompts())
        assert "instruction_hook_failed" in caplog.text
        assert "must not be logged" not in caplog.text
        assert turn.instruction == "Base."
    run(main())


def test_pipeline_template_bypasses_hooks(rig):
    async def main():
        calls = []
        rig.engine.add_instruction_hook(lambda *args: calls.append(args))
        task = asyncio.create_task(rig.engine.speak(Turn(TurnKind.GZ, 101, "guild", template="gz")))
        await rig.advance(45)
        assert task.done() and task.result()
        assert calls == []
    run(main())


def test_reused_turn_cannot_retain_stale_fact_after_compose_failure(rig, monkeypatch):
    async def main():
        captured = []
        def hook(turn, meta, text):
            return text + " fresh fact" if not captured else text
        async def compose(turn, meta, *, live):
            captured.append(turn.instruction)
            raise ValueError("synthetic composition failure")
        rig.engine.add_instruction_hook(hook)
        monkeypatch.setattr(rig.engine, "_compose", compose)
        turn = Turn(TurnKind.CALLOUT, 101, "guild", instruction="base")
        for _ in range(2):
            task = asyncio.create_task(rig.engine.speak(turn))
            await rig.advance(1)
            assert task.done() and task.result() == []
        assert captured == ["base fresh fact", "base"]
        assert turn.instruction == "base"
    run(main())
