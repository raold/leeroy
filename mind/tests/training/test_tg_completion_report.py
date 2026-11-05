"""Completion requires durable accepted evidence, never a status label alone."""

import asyncio
import copy
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from leeroy_mind.training import bandit, report, store
from .test_tg_experiments import CTX, experiment, meta, observe, open_one, pull
from .test_tg_report import DAY, NOW, START, WEEK, add, populate, section
from .test_tg_benchmark import bench, record as benchmark_pull, step as benchmark_step
from .test_tg_spread import NoExplore, healing_pull, weekly


def current(fake):
    year, week, _ = datetime.fromtimestamp(fake.clock.now(), timezone.utc).isocalendar()
    return report.training_section(fake.conn, f"{year}-W{week:02}", now=fake.clock.now(), names=lambda g: f"Member {g}")


def promoted(fake, experiment, monkeypatch):
    exp, _, trial, winner, _ = open_one(fake, experiment)
    monkeypatch.setattr(bandit, "decide", lambda *args, **kwargs: bandit.Verdict("promote", winner["id"], "test"))
    assert observe(fake, exp, "accepted-winner", winner).action == "promote"
    return exp, trial, winner


def human_use(fake, winner):
    now = fake.clock.now()
    with fake.conn:
        raid = fake.conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
                                "VALUES (?,'icc',10,0,0,101,101,101)", (now,)).lastrowid
    meta(fake, f"guild:raid:{raid}:instance", 77)
    meta(fake, f"guild:raid:{raid}:plan", dict(raid_key="icc", size=10, heroic=False,
         offline=False, human_in_raid=True, lead_guid=101))
    meta(fake, f"tg:in-play:{raid}:{CTX}", [dict(at_ms=int(now * 1000), card_id=winner["card"], arm_id=None, change="")])
    asyncio.run(fake.clock.advance(101, step=101))
    pull(fake, "human-use", card=winner["card"], kind="his", raid=raid)
    return raid


def test_actual_promotion_and_subsequent_recorded_human_use_complete_only_tactic(fake, experiment, monkeypatch):
    _, trial, winner = promoted(fake, experiment, monkeypatch)
    assert not current(fake)["done_when"]["won_tactic"]
    human_use(fake, winner)
    result = current(fake)
    assert result["done_when"]["won_tactic"]
    assert result["promotions"][0]["trial"] == trial["id"]
    assert result["promotions"][0]["human_use_pulls"] == ["human-use"]
    assert result["promotions"][0]["human_outcomes"]["pulls"] == 1
    assert "no causal" in result["evidence"]["tactic"]
    assert not result["done_when"]["won_rotation"] and not result["done_when"]["voice_run"]


@pytest.mark.parametrize("missing", ["verdict", "observation", "revision", "in_play", "instance", "basis", "early", "card",
    "unrelated_reference", "reference_basis", "reference_score", "reference_median", "reference_duplicate",
    "reference_missing", "raw_score", "in_play_change", "in_play_early", "contexts_limit", "arms_limit"])
def test_partial_or_incompatible_promotion_provenance_never_completes(fake, experiment, monkeypatch, missing):
    _, trial, winner = promoted(fake, experiment, monkeypatch)
    raid = human_use(fake, winner)
    with fake.conn:
        if missing == "verdict":
            fake.conn.execute("DELETE FROM meta WHERE key GLOB 'tg:verdict:*'")
        elif missing == "observation":
            fake.conn.execute("DELETE FROM meta WHERE key GLOB 'tg:obs:*'")
        elif missing == "revision":
            saved = report._meta(fake.conn, f"tg:trial:{trial['id']}")
            saved["revisions"] = {}
            meta(fake, f"tg:trial:{trial['id']}", saved)
        elif missing == "in_play":
            fake.conn.execute("DELETE FROM meta WHERE key GLOB 'tg:in-play:*'")
        elif missing == "instance":
            meta(fake, f"guild:raid:{raid}:instance", 88)
        elif missing == "basis":
            fake.conn.execute("UPDATE tg_pull SET score_basis='mechanics_deaths_time' WHERE id='human-use'")
        elif missing == "card":
            fake.conn.execute("UPDATE tg_card SET directives='[]' WHERE id=?", (winner["card"],))
        elif missing.startswith("reference") or missing in {"unrelated_reference", "contexts_limit", "arms_limit"}:
            key = f"tg:trial:{trial['id']}"
            saved = report._meta(fake.conn, key)
            if missing == "unrelated_reference":
                saved["references"] = {"unrelated": {}}
            elif missing == "reference_basis":
                saved["references"][CTX]["rows"][0]["score_basis"] = "mechanics_deaths_time"
            elif missing == "reference_score":
                saved["references"][CTX]["rows"][0]["score"] += 1
            elif missing == "reference_median":
                saved["references"][CTX]["median_kill_s"] += 1
            elif missing == "reference_duplicate":
                saved["references"][CTX]["rows"][1] = copy.deepcopy(saved["references"][CTX]["rows"][0])
            elif missing == "reference_missing":
                saved["contexts"].append("631:1:0")
            elif missing == "contexts_limit":
                saved["contexts"] = [CTX] * (report.MAX_ROWS + 1)
            else:
                saved["arms"].update({str(1000+i): {} for i in range(report.MAX_ARMS+1)})
            meta(fake, key, saved)
        elif missing == "raw_score":
            key = f"tg:obs:{winner['id']}:accepted-winner"
            saved = report._meta(fake.conn, key)
            saved["score"] += 1
            meta(fake, key, saved)
        elif missing.startswith("in_play_"):
            key = f"tg:in-play:{raid}:{CTX}"
            saved = report._meta(fake.conn, key)
            if missing == "in_play_change":
                saved[0]["change"] = "interrupted"
            else:
                saved[0]["at_ms"] -= 1
            meta(fake, key, saved)
        else:
            fake.conn.execute("UPDATE tg_pull SET start_ms=0 WHERE id='human-use'")
    assert not current(fake)["done_when"]["won_tactic"]


def test_reports_real_rollback_suspension_and_rotation_without_claiming_field_win(fake, experiment, monkeypatch):
    exp, _, winner = promoted(fake, experiment, monkeypatch)
    assert asyncio.run(exp.rollback(CTX))
    with fake.conn:
        fake.conn.execute("UPDATE tg_card SET status='suspended' WHERE id=?", (winner["card"],))
        fake.conn.execute("INSERT INTO tg_rotation(name,class,spec_tab,lines,origin,status,created) "
                          "VALUES ('synthetic_rotation',8,0,'[]','lab','champion',?)", (fake.clock.now(),))
    result = current(fake)
    assert result["rollbacks"][0]["old"] == winner["card"]
    assert result["rollbacks"][0]["commands_undone"] == "unverified"
    assert result["suspended"][0]["id"] == winner["card"]
    assert result["rotations"][0]["field_performance"] == "unavailable"
    assert result["rotations"][0]["requested_damage_per_second"] is None
    assert not result["done_when"]["won_rotation"]


def test_benchmark_and_other_backlogs_cannot_age_observation(fake):
    populate(fake)
    meta(fake, "tg_first_pull", START)
    with fake.conn:
        fake.conn.execute("UPDATE tg_pull SET kind='benchmark' WHERE received<?", (START + 28*DAY,))
    assert not section(fake)["done_when"]["four_weeks"]
    assert section(fake)["contexts"][0]["baseline_pulls"] == 10


@pytest.mark.parametrize("value", [-1, True, "0.4", float("nan")])
def test_malformed_or_negative_gate_does_not_pass(fake, value):
    meta(fake, "tg_gate", {"phase": "passed", "verdict_ms": value})
    assert not section(fake)["done_when"]["gate_within_1ms"]


def test_voice_send_receipts_are_diagnostics_never_acceptance(fake):
    meta(fake, "tg_voice:2026-10-26:1", dict(events=[
        dict(ts=START+28*DAY, kind="briefing", status="proposed"),
        dict(ts=START+28*DAY+1, kind="callout", status="sent")],
        acceptance="passed", audible_delivery="verified", fact_delivery="verified"))
    result = section(fake)
    assert result["evidence"]["voice"]["callout_send_receipts"] == 1
    assert result["evidence"]["voice"]["briefing_proposals"] == 1
    assert result["evidence"]["voice"]["audible_delivery"] == "unverified"
    assert not result["done_when"]["voice_run"] and not result["done_when"]["cast_callouts"]


def test_read_only_schema7_report_is_explicit_and_does_not_migrate(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=7")
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
    before = path.read_bytes()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        result = report.training_section(conn, WEEK, now=NOW, names=str)
        assert result["evidence"]["schema"].startswith("unavailable_or_incompatible")
        assert not any(result["done_when"].values())
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert path.read_bytes() == before


def test_bounded_report_marks_incomplete_evidence_and_stays_serializable(fake, monkeypatch):
    populate(fake)
    meta(fake, "tg_first_pull", START)
    monkeypatch.setattr(report, "MAX_PULLS", 3)
    result = section(fake)
    assert "pulls" in result["evidence"]["truncated"]
    assert not any(result["done_when"].values())
    json.dumps(result, allow_nan=False)


def test_bare_setup_and_benchmark_rows_do_not_imply_verified_adoption_or_rivals(fake):
    with fake.conn:
        fake.conn.execute("INSERT INTO tg_setup(guid,since,copied_from,applied) VALUES (102,?,101,?)", (NOW-1, NOW-1))
        fake.conn.execute("INSERT INTO tg_benchmark(week,raid_key,guild_id,members,bosses,outcome,kills,wipes,ts) "
                          "VALUES (?,'icc10',2,'[5001]','[0]','done',1,0,?)", (WEEK, NOW-1))
    result = section(fake)
    assert not result["spread"]
    assert result["rivals"][0]["provenance"] == "unavailable"
    assert result["rivals"][0]["actual_dps_percentiles"] is None


def test_dummy_proxy_requires_correlated_raw_result_and_restoration_receipt(fake):
    sent = NOW - 100
    with fake.conn:
        rotation = fake.conn.execute("INSERT INTO tg_rotation(name,class,spec_tab,lines,origin,status,created) "
            "VALUES ('synthetic_rotation',8,0,'[]','lab','dummy',?)", (sent,)).lastrowid
        sample = fake.conn.execute("INSERT INTO tg_dummy(rotation,guid,round,seconds,dps,stock_dps,ts,damage_basis,spell_basis) "
            "VALUES (?,101,0,60,100,0,?,'requested_pre_ai','best_effort_thread_pair')", (rotation, sent)).lastrowid
    receipt = dict(metric="dummy_requested_damage_proxy", restoration="complete_no_desired_setup", ack="accepted", bad="",
        candidate_name="synthetic_rotation", sent_ms=int(sent*1000), rows={"101": sample},
        terminal=dict(state="done", reason="", guids=[101], ts=int((sent+61)*1000)),
        results={"101": dict(guid=101, dropped="", damage=6000, ms=60000, ts=int((sent+60)*1000),
                             damage_basis="requested_pre_ai", spell_basis="best_effort_thread_pair")})
    meta(fake, "tg:lab:attempt:synthetic", receipt)
    result = section(fake)
    assert result["rotations"][0]["verified_proxy_samples"] == 1
    assert result["rotations"][0]["requested_damage_per_second"] == 100
    assert not result["done_when"]["won_rotation"]
    receipt["restoration"] = "pending"
    meta(fake, "tg:lab:attempt:synthetic", receipt)
    assert section(fake)["rotations"][0]["requested_damage_per_second"] is None


def spread_history(fake, *, sparse_peer=False):
    from leeroy_mind.training.spread import _week_bounds, score_evidence
    with fake.conn:
        fake.conn.execute("INSERT INTO mentor(mentor,mentee,started,ends) VALUES (102,101,?,?)", (START, NOW+DAY))
        fake.conn.execute("INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
            "VALUES (1,?,?,'icc',10,0,1,101,101,101)", (START-40*DAY, NOW-1))
    for index in range(3):
        healing_pull(fake, f"farm-{index}", START+index*DAY, members=[101,102])
    for offset in range(4):
        for index in range(4):
            guids = [101,102,103] if sparse_peer and offset == 3 and index == 0 else [101,102]
            healing_pull(fake, f"healing-{offset}-{index}", START+(7*(offset+1)+index/10)*DAY+100,
                         members=guids)
        week = f"2026-W{41+offset}"
        stamp = START + (7*(offset+1)+1)*DAY
        cohort = json.loads(json.dumps(score_evidence(fake.conn, week, now=stamp, tz=timezone.utc)))
        assert set(cohort) == {"101", "102"}
        with fake.conn:
            setup = fake.conn.execute("INSERT INTO tg_setup(guid,since,copied_from) VALUES (101,?,102)", (stamp,)).lastrowid
        meta(fake, "tg:spread:week:" + week, dict(state="desired", at=stamp, week=week,
            reason="realm_default_restoration_and_exact_setup_application_receipt_required",
            normalization_version=1, week_bounds=list(_week_bounds(week, timezone.utc)), timezone_key=None,
            utc_offset_seconds=0, score_cohort=cohort, cohort_kind="rank_eligible_scores_requires_original_sql",
            changes=[dict(setup=setup, guid=101, copied_from=102, application="deferred",
                          evidence=cohort)]))


def test_desired_spread_and_real_four_week_scores_are_not_live_adoption(fake):
    spread_history(fake)
    result = section(fake)
    assert result["spread"][0]["application"] == "desired_only"
    assert result["spread"][0]["copied_from"] == 102
    assert result["mentees"][0]["four_week_change"] == 0
    assert len(result["mentees"][0]["weeks"]) == 4
    with fake.conn:
        fake.conn.execute("UPDATE tg_member SET healing=0 WHERE pull='healing-3-0' AND guid=101")
    assert section(fake)["mentees"][0]["four_week_change"] is None


@pytest.mark.parametrize("bad", ["duplicate", "out_of_week", "score", "z", "non_healer", "non_farm", "legacy", "bounds", "large_evidence"])
def test_spread_refuses_invented_or_incomplete_normalization(fake, bad):
    spread_history(fake)
    key = "tg:spread:week:" + WEEK
    receipt = report._meta(fake.conn, key)
    item = receipt["score_cohort"]["101"]
    if bad == "duplicate":
        item["pulls"][1] = copy.deepcopy(item["pulls"][0])
    elif bad == "out_of_week":
        item["pulls"][0]["start_ms"] = int(START*1000)
    elif bad == "score":
        item["score"] += 1
    elif bad == "z":
        item["pulls"][0]["z"] += 4
        item["score"] += 1
    elif bad == "non_healer":
        with fake.conn:
            fake.conn.execute("UPDATE tg_member SET role='tank' WHERE guid=101 AND pull='healing-3-0'")
    elif bad == "non_farm":
        with fake.conn:
            fake.conn.execute("UPDATE tg_pull SET outcome='wiped' WHERE start_ms<?", (int((START+28*DAY)*1000),))
    elif bad == "legacy":
        receipt.pop("score_cohort")
    elif bad == "bounds":
        receipt["week_bounds"][0] -= DAY
    elif bad == "large_evidence":
        receipt["changes"][0]["evidence"] = {str(g): item for g in range(report.MAX_ROWS+1)}
    if bad not in {"legacy", "large_evidence"}:
        receipt["changes"][0]["evidence"] = copy.deepcopy(receipt["score_cohort"])
    meta(fake, key, receipt)
    assert section(fake)["mentees"][0]["four_week_change"] is None


def test_sparse_unranked_healer_still_controls_normalization_provenance(fake):
    spread_history(fake, sparse_peer=True)
    receipt = report._meta(fake.conn, "tg:spread:week:" + WEEK)
    assert "103" not in receipt["score_cohort"]
    assert section(fake)["mentees"][0]["four_week_change"] is not None
    with fake.conn:
        fake.conn.execute("UPDATE tg_member SET healing=1 WHERE pull='healing-3-0' AND guid=103")
    assert section(fake)["mentees"][0]["four_week_change"] is None


def test_actual_weekly_producer_receipt_verifies_its_scores(fake, weekly):
    from leeroy_mind.training.spread import run_week
    applier, proof, week = weekly
    assert asyncio.run(run_week(fake, applier, week, schedule=lambda: proof, rng=NoExplore())) == 2
    receipt = report._meta(fake.conn, "tg:spread:week:" + week)
    actual = report._spread_scores(fake.conn, receipt, fake.clock.now())
    assert actual and actual[101]["z"] == receipt["score_cohort"]["101"]["score"]


async def finalized_benchmark(bench):
    for index in range(3):
        benchmark_pull(bench, f"guild-{index}", kind="offline",
            start=int(bench.clock.now()*1000)-500000+index*100000,
            end=int(bench.clock.now()*1000)-440000+index*100000)
    await benchmark_step(bench)
    await benchmark_step(bench)
    await bench.clock.advance(61, step=61)
    observed = benchmark_pull(bench, "sealed-rival")
    await bench.runner.on_pull(observed)
    await bench.runner.on_job(bench.job(state="done", reason="frontier"))
    bench.cleanup_state = "complete"
    await benchmark_step(bench)
    return report.training_section(bench.conn, "2026-W45", now=bench.clock.now(), names=str)


def test_actual_benchmark_finalization_is_immutable_after_ledger_edits_and_restart(bench):
    from leeroy_mind.training.benchmark import Benchmarks
    result = asyncio.run(finalized_benchmark(bench))["rivals"][0]
    assert result["provenance"] == "exact_scope_cleanup_receipt"
    assert result["comparisons"][0]["rival_kill_seconds"] == 60
    assert result["comparisons"][0]["guild_farm_kill_seconds"] == 60
    assert result["actual_dps_percentiles"] is None
    with bench.conn:
        bench.conn.execute("UPDATE tg_pull SET end_ms=end_ms-10000 WHERE id IN ('sealed-rival','guild-0')")
    assert report.training_section(bench.conn, "2026-W45", now=bench.clock.now(), names=str)["rivals"][0] == result
    bench.runner = Benchmarks(bench, bench.cfg, schedule_provider=bench.schedule)
    asyncio.run(benchmark_step(bench))
    assert report.training_section(bench.conn, "2026-W45", now=bench.clock.now(), names=str)["rivals"][0] == result


@pytest.mark.parametrize("bad", ["query", "missing_baselines", "missing_admitted", "mutated_result", "unhashable_map", "large_pulls",
    "terminal_string", "terminal_list", "terminal_state", "terminal_reason"])
def test_benchmark_requires_complete_typed_sealed_provenance(bench, bad):
    assert asyncio.run(finalized_benchmark(bench))["rivals"][0]["comparisons"]
    key = "tg:benchmark:result:2026-W45"
    receipt = report._meta(bench.conn, key)
    if bad == "missing_baselines":
        receipt.pop("baselines")
    elif bad == "missing_admitted":
        receipt.pop("admitted_pulls")
    elif bad == "mutated_result":
        receipt["comparisons"][0]["rival_kill_seconds"] += 1
    elif bad == "unhashable_map":
        receipt["receipt"]["map"] = []
    elif bad == "large_pulls":
        receipt["receipt"]["pulls"] = ["sealed-rival"] * 257
    elif bad == "terminal_string":
        receipt["receipt"]["terminal"] = "corrupt"
    elif bad == "terminal_list":
        receipt["receipt"]["terminal"] = ["corrupt"]
    elif bad == "terminal_state":
        receipt["receipt"]["terminal"]["state"] = ["corrupt"]
    elif bad == "terminal_reason":
        receipt["receipt"]["terminal"]["reason"] = ["corrupt"]
    else:
        receipt["receipt"]["query"] = {"id": "different-query"}
    meta(bench, key, receipt)
    result = report.training_section(bench.conn, "2026-W45", now=bench.clock.now(), names=str)
    assert not result["rivals"][0]["comparisons"]
    assert result["rivals"][0]["provenance"] == "unavailable"


@pytest.mark.parametrize("changes", [dict(start_ms=int((NOW+DAY)*1000)), dict(end_ms=1), dict(start_ms=-1),
    dict(deaths=-1), dict(deaths="unknown"), dict(failed_mechanics=-1), dict(failed_mechanics="unknown"),
    dict(received=START), dict(start_ms="unknown"), dict(end_ms="unknown")])
def test_invalid_wipe_cannot_age_observation_or_crash_statistics(fake, changes):
    add(fake, 0)
    ident = add(fake, 28, outcome="wiped")
    meta(fake, "tg_first_pull", START)
    with fake.conn:
        for field, value in changes.items():
            fake.conn.execute(f"UPDATE tg_pull SET {field}=? WHERE id=?", (value, ident))
    result = section(fake)
    assert not result["done_when"]["four_weeks"]
    assert not result["contexts"]
    json.dumps(result, allow_nan=False)


def test_corrupt_dummy_optional_receipt_does_not_hide_valid_pull_history(fake):
    add(fake, 28)
    sent = NOW - 100
    with fake.conn:
        rotation = fake.conn.execute("INSERT INTO tg_rotation(name,class,spec_tab,lines,origin,status,created) "
            "VALUES ('corrupt',8,0,'[]','lab','dummy',?)", (sent,)).lastrowid
        sample = fake.conn.execute("INSERT INTO tg_dummy(rotation,guid,round,seconds,dps,stock_dps,ts) "
            "VALUES (?,101,0,60,100,0,?)", (rotation, sent)).lastrowid
    meta(fake, "tg:lab:attempt:corrupt", dict(metric="dummy_requested_damage_proxy",
        restoration="complete_no_desired_setup", ack="accepted", bad="", candidate_name="corrupt",
        sent_ms=int(sent*1000), rows={"101": sample}, terminal=dict(state="done", reason="", guids=None),
        results={"101": dict(guid=101)}))
    result = section(fake)
    assert result["contexts"][0]["pulls"] == 1
    assert result["rotations"][0]["verified_proxy_samples"] == 0


def test_metadata_size_and_depth_are_bounded_before_evidence_use(fake, monkeypatch):
    monkeypatch.setattr(report, "MAX_RECEIPT_BYTES", 50)
    meta(fake, "oversized", {"data": "x" * 100})
    assert report._meta(fake.conn, "oversized") is None
    assert report._json("[" * 30 + "0" + "]" * 30) is None
