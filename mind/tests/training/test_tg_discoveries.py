"""Real synthetic LabRunner output through the ordinary social proposal port."""

import asyncio
from dataclasses import replace
import json

import pytest

from guild.fakes import member, persona
from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.training.discoveries import ATTEMPT_PREFIX, Discoveries, DiscoverySchedule
from leeroy_mind.training import lab_runner as lab_module
from leeroy_mind.training.lab import Candidate
from leeroy_mind.training.lab_runner import ATTEMPT_PREFIX as LAB_ATTEMPT_PREFIX, OWNED_KEY, SUMMARY_PREFIX
from .test_tg_lab_runner import complete, lab, start


async def finish_lab(fake, runner):
    fake.online = False
    await start(runner)
    for _ in range(100):
        if not runner.active:
            break
        if runner._owned["current"]:
            await complete(fake, runner)
        else:
            await runner.tick()
    assert not runner.active


@pytest.fixture
def discovery(fake, lab):
    runner, _, _ = lab
    asyncio.run(finish_lab(fake, runner))
    fake.online = True
    for guid in fake.members:
        fake.personas[guid] = persona(guid, "SyntheticMage", main_spec="fire", raid_role="ranged",
                                     thinks_as="theorycrafter")
    proof = DiscoverySchedule(fake.clock.now(), True, True, True, 17, False, False, False, False)
    discoveries = Discoveries(fake, fake.cfg, schedule=lambda: proof)
    return discoveries, proof, runner


def attempts(fake):
    return [(r["key"], json.loads(r["value"])) for r in fake.conn.execute(
        "SELECT key,value FROM meta WHERE key LIKE ? ORDER BY key", (ATTEMPT_PREFIX + "%",))]


def change_meta(fake, key, transform):
    value = _get(fake.conn, key)
    transform(value)
    with fake.conn:
        _put(fake.conn, key, value)


def test_verified_lab_find_uses_normal_aside_and_reserves_before_proposing(fake, discovery):
    discoveries, proof, _ = discovery
    proposed = fake.propose
    def inspect(guid, channel, instruction, *, kind):
        rows = attempts(fake)
        assert len(rows) == 2 and all(row["state"] == "attempted" for _, row in rows)
        assert all(row["session"] == proof.session_id and row["delivery"] == "unconfirmed" for _, row in rows)
        proposed(guid, channel, instruction, kind=kind)
    fake.propose = inspect
    assert asyncio.run(discoveries.tick())
    assert len(fake.proposed) == 1
    speaker, channel, instruction, kind = fake.proposed[0]
    assert speaker == 101 and channel == "guild" and kind is TurnKind.ASIDE
    assert "fire mage" in instruction and "4 paired" in instruction
    assert "requested-damage proxy" in instruction and "provisional" in instruction
    assert "Actual DPS and performance in raids remain unmeasured" in instruction
    assert "tg_8_1_" not in instruction and "%" not in instruction
    assert "You ran" not in instruction and "last night" not in instruction
    assert all(row["state"] == "proposed_unconfirmed" for _, row in attempts(fake))
    assert not fake.memories and not fake.said


def test_repeated_ticks_and_restart_do_not_repeat_find_or_consume_another_speakers_week(fake, discovery):
    discoveries, proof, _ = discovery
    assert asyncio.run(discoveries.tick())
    for _ in range(3):
        assert not asyncio.run(discoveries.tick())
    restarted = Discoveries(fake, fake.cfg, schedule=lambda: proof)
    assert not asyncio.run(restarted.tick())
    assert len(fake.proposed) == 1 and len(attempts(fake)) == 2


def test_theorycraft_officer_title_fallback_uses_no_character_name_rule(fake, discovery):
    discoveries, _, _ = discovery
    for guid in fake.personas:
        fake.personas[guid].game.thinks_as = "social"
    fake.members[201] = member(201, "ArbitraryOfficer", 1, 0)
    fake.personas[201] = persona(201, "ArbitraryOfficer", guild_role="officer", officer_title="Raid Lead, THEORYCRAFT")
    assert asyncio.run(discoveries.tick())
    assert fake.proposed[0][0] == 201
    assert "ArbitraryOfficer" not in fake.proposed[0][2]


def test_same_class_theorycrafter_precedes_an_earlier_guid_officer(fake, discovery):
    discoveries, _, _ = discovery
    fake.members[77] = member(77, "SyntheticOfficer", 1, 0)
    fake.personas[77] = persona(77, "SyntheticOfficer", guild_role="officer", officer_title="Theorycraft")
    assert asyncio.run(discoveries.tick()) and fake.proposed[0][0] == 101


@pytest.mark.parametrize("condition", ["offline", "away", "held", "wrong_class", "missing_persona", "wrong_identity", "social"])
def test_no_eligible_speaker_never_creates_attempts(fake, discovery, condition):
    discoveries, _, _ = discovery
    for guid in list(fake.members):
        if condition == "offline":
            fake.members[guid] = replace(fake.members[guid], online=False)
        elif condition == "away":
            fake.away_set.add(guid)
        elif condition == "held":
            fake.holders[guid] = "raid"
        elif condition == "wrong_class":
            fake.members[guid] = replace(fake.members[guid], class_=1)
        elif condition == "missing_persona":
            fake.personas.pop(guid)
        elif condition == "wrong_identity":
            fake.personas[guid].guid = 999
        else:
            fake.personas[guid].game.thinks_as = "social"
    assert not asyncio.run(discoveries.tick()) and not fake.proposed and not attempts(fake)


@pytest.mark.parametrize("field,value", [
    ("connected", False), ("presence_known", False), ("human_online", False),
    ("connected", None), ("session_id", 0), ("session_id", None),
    ("raid_active", True), ("raid_active", None), ("lab_active", True),
    ("benchmark_active", True), ("acquiring", True), ("observed_at", 0),
    ("observed_at", 9999999999),
])
def test_session_and_scheduler_proof_must_be_complete_fresh_and_outside_raids(fake, discovery, field, value):
    discoveries, proof, _ = discovery
    discoveries.schedule = lambda: replace(proof, **{field: value})
    assert not asyncio.run(discoveries.tick()) and not fake.proposed and not attempts(fake)


def test_missing_or_raising_provider_and_offline_owner_fail_closed(fake, discovery):
    discoveries, proof, _ = discovery
    discoveries.schedule = None
    assert not asyncio.run(discoveries.tick())
    def unavailable():
        raise RuntimeError("synthetic unavailable adapter")
    discoveries.schedule = unavailable
    assert not asyncio.run(discoveries.tick())
    discoveries.schedule = lambda: proof
    fake.online = False
    assert not asyncio.run(discoveries.tick()) and not attempts(fake)


@pytest.mark.parametrize("target,field,value", [
    ("summary", "reason", "shutdown"), ("summary", "metric", "actual_dps"),
    ("summary", "field_performance", "improved"), ("summary", "status", "field"),
    ("summary", "winner", None), ("summary", "pairs", 3), ("summary", "pairs", 5),
    ("summary", "winner_round", 9), ("summary", "ts", 9999999999),
    ("summary", "lab", "other_lab"), ("owned", "phase", "blocked"),
    ("owned", "halt", "cleanup_failed"), ("owned", "id", "other_lab"),
])
def test_summary_and_completed_ownership_must_be_exact_provisional_evidence(fake, discovery, target, field, value):
    discoveries, _, runner = discovery
    key = OWNED_KEY if target == "owned" else SUMMARY_PREFIX + runner._night()
    change_meta(fake, key, lambda row: row.update({field: value}))
    assert not asyncio.run(discoveries.tick()) and not fake.proposed and not attempts(fake)


@pytest.mark.parametrize("column,value", [("damage_basis", "unknown"), ("spell_basis", "unknown"),
    ("dropped", "missing_result"), ("stock_dps", 0), ("dps", 9999), ("guid", 999), ("seconds", 1)])
def test_final_round_pairs_need_matching_actual_dummy_rows(fake, discovery, column, value):
    discoveries, _, runner = discovery
    summary = _get(fake.conn, SUMMARY_PREFIX + runner._night())
    with fake.conn:
        fake.conn.execute("UPDATE tg_dummy SET " + column + "=? WHERE id=(SELECT MIN(d.id) FROM tg_dummy d "
            "JOIN tg_rotation r ON r.id=d.rotation WHERE r.name=? AND d.round=?)", (value, summary["winner"], summary["winner_round"]))
    assert not asyncio.run(discoveries.tick()) and not fake.proposed and not attempts(fake)


def test_corrupt_json_and_overwritten_ownership_do_not_reconstruct_old_find(fake, discovery):
    discoveries, _, runner = discovery
    with fake.conn:
        fake.conn.execute("UPDATE meta SET value='{' WHERE key=?", (OWNED_KEY,))
    assert not asyncio.run(discoveries.tick()) and not attempts(fake)
    with fake.conn:
        fake.conn.execute("DELETE FROM meta WHERE key=?", (OWNED_KEY,))
    assert _get(fake.conn, SUMMARY_PREFIX + runner._night())["winner"]
    assert not asyncio.run(discoveries.tick()) and not fake.proposed


@pytest.mark.parametrize("change", ["session", "raid", "speaker", "summary"])
def test_post_reservation_drift_consumes_attempt_without_proposal(fake, discovery, change):
    discoveries, proof, runner = discovery
    calls = 0
    def schedule():
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "session":
                return replace(proof, session_id=18)
            if change == "raid":
                return replace(proof, raid_active=True)
            if change == "speaker":
                fake.holders[101] = "raid"
            if change == "summary":
                change_meta(fake, SUMMARY_PREFIX + runner._night(), lambda row: row.update(pairs=0))
        return proof
    discoveries.schedule = schedule
    assert not asyncio.run(discoveries.tick()) and not fake.proposed
    assert len(attempts(fake)) == 2 and all(row["state"] == "cancelled" for _, row in attempts(fake))


def test_proposal_failure_does_not_retry_or_claim_delivery(fake, discovery):
    discoveries, _, _ = discovery
    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic queue failure")
    fake.propose = unavailable
    assert not asyncio.run(discoveries.tick())
    assert all(row["state"] == "proposal_failed" and row["delivery"] == "unconfirmed" for _, row in attempts(fake))
    assert not asyncio.run(discoveries.tick())


def test_second_real_lab_find_cannot_repeat_a_speakers_week(fake, discovery):
    discoveries, proof, runner = discovery
    for guid in (102, 103, 104):
        fake.personas[guid].game.thinks_as = "social"
    assert asyncio.run(discoveries.tick())
    async def next_night():
        await fake.clock.advance(86400, step=86400)
        await finish_lab(fake, runner)
    asyncio.run(next_night())
    fake.online = True
    discoveries.schedule = lambda: replace(proof, observed_at=fake.clock.now())
    assert discoveries._verified() is not None
    assert not asyncio.run(discoveries.tick()) and len(fake.proposed) == 1


@pytest.mark.parametrize("condition", ["open_raid", "unresolved_mutation", "disabled", "lab_disabled", "rotation_retired"])
def test_disabled_or_retained_work_or_retired_candidate_stays_silent(fake, discovery, condition):
    discoveries, _, _ = discovery
    if condition == "disabled":
        discoveries.cfg = replace(discoveries.cfg, enabled=False)
    elif condition == "lab_disabled":
        discoveries.cfg = replace(discoveries.cfg, lab=False)
    elif condition == "open_raid":
        with fake.conn:
            fake.conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
                "VALUES (0,'icc',10,0,0,101,101,101)")
    elif condition == "unresolved_mutation":
        with fake.conn:
            fake.conn.execute("INSERT INTO tg_applied(ts,run,layer,context,command,inverse) "
                "VALUES (0,'old','standing','0:0:0','{}','{}')")
    else:
        with fake.conn:
            fake.conn.execute("UPDATE tg_rotation SET status='retired'")
    assert not asyncio.run(discoveries.tick()) and not fake.proposed and not attempts(fake)


@pytest.mark.parametrize("leg", [0, 1, 2])
def test_missing_candidate_or_either_adjacent_stock_receipt_stays_silent(fake, discovery, leg):
    discoveries, _, _ = discovery
    owned = _get(fake.conn, OWNED_KEY)
    with fake.conn:
        fake.conn.execute("DELETE FROM meta WHERE key=?", (LAB_ATTEMPT_PREFIX + owned["attempt_ids"][leg],))
    assert not asyncio.run(discoveries.tick()) and not fake.proposed and not attempts(fake)


@pytest.mark.parametrize("leg", [0, 1, 2])
@pytest.mark.parametrize("change", ["damage", "ms", "basis", "command", "result_id", "guid", "lab", "round",
    "terminal", "cleanup", "restoration", "restoration_time", "result_time", "terminal_time", "borrowed_row"])
def test_original_native_chain_corruption_cannot_be_hidden_by_copied_rates(fake, discovery, leg, change):
    discoveries, _, _ = discovery
    owned = _get(fake.conn, OWNED_KEY)
    key = LAB_ATTEMPT_PREFIX + owned["attempt_ids"][leg]
    row = _get(fake.conn, key)
    result = row["results"]["101"]
    if change == "damage":
        result["damage"] *= 2
    elif change == "ms":
        result["ms"] -= 1
    elif change == "basis":
        result["damage_basis"] = "actual_dps"
    elif change == "command":
        row["co"][0] = "+damage"
    elif change == "result_id":
        result["id"] = "other-job"
    elif change == "guid":
        result["guid"] = 102
    elif change == "lab":
        row["lab"] = "other-lab"
    elif change == "round":
        row["round"] = 1
    elif change == "terminal":
        row["terminal"]["state"] = "failed"
    elif change == "cleanup":
        row["terminal"]["reason"] = "cleanup_failed:return"
    elif change == "restoration":
        row["restoration"] = "pending"
    elif change == "restoration_time":
        row["restoration_at"] = row["sent_ms"] / 1000
    elif change == "result_time":
        result["ts"] = row["sent_ms"]
    elif change == "terminal_time":
        row["terminal"]["ts"] = row["restoration_at"] * 1000 + 5000
    else:
        other = _get(fake.conn, LAB_ATTEMPT_PREFIX + owned["attempt_ids"][(leg + 1) % 3])
        row["rows"]["101"] = other["rows"]["101"]
    with fake.conn:
        _put(fake.conn, key, row)
    assert not asyncio.run(discoveries.tick()) and not fake.proposed and not attempts(fake)


@pytest.mark.parametrize("change", ["missing", "duplicate", "reordered", "oversized", "missing_last"])
def test_only_exact_bounded_producer_attempt_manifest_is_accepted(fake, discovery, change):
    discoveries, _, _ = discovery
    def corrupt(owned):
        ids = owned["attempt_ids"]
        if change == "missing":
            del owned["attempt_ids"]
        elif change == "duplicate":
            ids[-1] = ids[0]
        elif change == "reordered":
            ids[0], ids[2] = ids[2], ids[0]
        elif change == "oversized":
            owned["attempt_ids"] = [str(i) for i in range(129)]
        else:
            ids.pop()
    change_meta(fake, OWNED_KEY, corrupt)
    assert not asyncio.run(discoveries.tick()) and not fake.proposed and not attempts(fake)


@pytest.mark.parametrize("target", ["owned", "summary", "attempt"])
@pytest.mark.parametrize("extra", ['1e999', '[1,2,3]', '"' + 'x' * 65536 + '"'])
def test_nonfinite_deep_or_oversized_nested_state_fails_closed(fake, discovery, target, extra):
    discoveries, _, runner = discovery
    owned = _get(fake.conn, OWNED_KEY)
    key = {"owned": OWNED_KEY, "summary": SUMMARY_PREFIX + runner._night(),
           "attempt": LAB_ATTEMPT_PREFIX + owned["attempt_ids"][0]}[target]
    if extra == '[1,2,3]':
        extra = '[' * 20 + '1' + ']' * 20
    raw = fake.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]
    with fake.conn:
        fake.conn.execute("UPDATE meta SET value=? WHERE key=?", (raw[:-1] + ',"extra":' + extra + '}', key))
    assert not asyncio.run(discoveries.tick()) and not fake.proposed and not attempts(fake)


def test_unknown_small_extras_are_excluded_from_reserved_evidence(fake, discovery):
    discoveries, _, runner = discovery
    change_meta(fake, SUMMARY_PREFIX + runner._night(), lambda row: row.update(extra="untrusted annotation"))
    assert asyncio.run(discoveries.tick())
    assert all("untrusted annotation" not in json.dumps(row["evidence"]) for _, row in attempts(fake))


def test_real_multi_round_reversal_uses_final_receipts_and_pair_count(fake, lab, monkeypatch):
    runner, _, _ = lab
    candidates = [Candidate("tg_8_1_" + letter * 16, 8, 1, (f"often>bolt!{rank}",), None, "synthetic")
                  for letter, rank in (("a", 9), ("b", 8), ("c", 7), ("d", 6))]
    monkeypatch.setattr(lab_module, "propose", lambda *a, **kw: candidates)
    async def scenario():
        fake.online = False
        await start(runner)
        for _ in range(100):
            if not runner.active:
                break
            current = runner._owned["current"]
            if current:
                name = current["candidate_name"]
                rates = {"a": 5000, "b": 2200, "c": 2100, "d": 2000} if runner._owned["round"] == 0 else {"a": 2020, "b": 2300}
                rate = rates[name[-1]] if name else 2000
                await complete(fake, runner, changes={"damage": rate * current["seconds"]})
            else:
                await runner.tick()
        assert not runner.active
    asyncio.run(scenario())
    fake.online = True
    for guid in fake.members:
        fake.personas[guid] = persona(guid, "SyntheticMage", main_spec="fire", raid_role="ranged", thinks_as="theorycrafter")
    proof = DiscoverySchedule(fake.clock.now(), True, True, True, 17, False, False, False, False)
    discoveries = Discoveries(fake, fake.cfg, schedule=lambda: proof)
    evidence = discoveries._verified()
    assert evidence is not None
    assert evidence["summary"]["winner"] == candidates[1].name
    assert evidence["summary"]["winner_round"] == 1
    assert evidence["summary"]["pairs"] == 4 and evidence["summary"]["aggregate_pairs"] == 8
    # Earlier dominance cannot qualify the first candidate after its final 1% result.
    summary_key = SUMMARY_PREFIX + runner._night()
    summary = _get(fake.conn, summary_key)
    change_meta(fake, summary_key, lambda row: row.update(winner=candidates[0].name))
    assert not asyncio.run(discoveries.tick()) and not fake.proposed and not attempts(fake)
    with fake.conn:
        _put(fake.conn, summary_key, summary)
    assert asyncio.run(discoveries.tick())
    assert "4 paired" in fake.proposed[0][2] and "8 paired" not in fake.proposed[0][2]
