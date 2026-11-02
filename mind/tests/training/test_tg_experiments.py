import asyncio
import copy
from dataclasses import replace
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from leeroy_mind.training import bandit, drift, store
from leeroy_mind.training.cards import Card, Directive
from leeroy_mind.training.experiments import Experimenter
from .test_tg_store import context, record


CTX = "631:0:0"


def meta(fake, key, value):
    with fake.conn:
        fake.conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(value)))


def revision_tree(path):
    for name in drift.MODULES:
        git = path / "modules" / name / ".git"
        git.mkdir(parents=True)
        (git / "HEAD").write_text("a" * 40, encoding="ascii")


def pull(fake, ident, *, when=None, received=None, arm=None, card=None, kind="offline",
         score=0, basis="deaths_time", outcome="killed", boss=0, covariates=None, raid=1,
         duration=100):
    when = fake.clock.now() if when is None else when
    rec = record(ident, outcome=outcome, boss=boss)
    rec.header = replace(rec.header, start_ms=int((when - duration) * 1000), end_ms=int(when * 1000))
    cov = dict(ilvl=250, healers=2, drunk=0, flask_skips=0, human_in_raid=int(kind == "his"), assisted=0)
    if covariates is not None:
        cov.update(covariates)
    store.put_pull(fake.conn, rec, context(kind=kind, arm_id=arm, card_id=card, raid_id=raid, covariates=cov),
                   {101: "tank"}, received=when if received is None else received)
    store.set_score(fake.conn, ident, score, basis=basis)


def candidate(fake, context=CTX, lever="formation", value="far"):
    return store.put_card(fake.conn, Card(None, context, 1, "candidate", "coach",
        (Directive(lever, value),), created=fake.clock.now(), changed=fake.clock.now()))


@pytest.fixture
def experiment(fake, tmp_path):
    core = tmp_path / "core"
    revision_tree(core)
    fake.cfg = replace(fake.cfg, enabled=True, core_dir=core)
    now = fake.clock.now()
    # Actual eligible receipts span thirty days. Backdating first_pull metadata
    # alone is explicitly insufficient in a separate regression below.
    for name, age in (("baseline-start", 30 * 86400), ("baseline-middle", 10 * 86400), ("baseline-end", 200)):
        pull(fake, name, when=now-age, raid=None)
    meta(fake, "tg_first_pull", now - 30 * 86400)
    meta(fake, "tg_gate", {"phase": "passed", "verdict_ms": .5})
    meta(fake, "tg_revs", drift.revisions(core))
    with fake.conn:
        raid = fake.conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
            "VALUES (?,'icc',10,0,1,101,101,101)", (now-1000,)).lastrowid
    run = SimpleNamespace(human_in_raid=False, lead_is_human=False,
        session=SimpleNamespace(raid_id=raid, ctx=SimpleNamespace(map_id=631, instance_id=77)),
        plan=SimpleNamespace(lead_guid=101, size=10, heroic=False, offline=True))
    exp = Experimenter(fake, fake.cfg)
    return exp, run


def open_one(fake, experiment):
    exp, run = experiment
    ident = candidate(fake)
    assert asyncio.run(exp.open_trials()) == 1
    trial = store.trial_rows(fake.conn, status="open")[0]
    arms = exp._arms(trial["id"])
    derived = json.loads(fake.conn.execute("SELECT value FROM meta WHERE key=?", (f"tg:proposal:{ident}",)).fetchone()[0])["derived_card"]
    return exp, run, trial, next(a for a in arms if a["card"] == derived), next(a for a in arms if a["champion"])


def observe(fake, exp, name, arm, **kwargs):
    # Admission is at now; advance real fake time so fight starts after admission.
    asyncio.run(fake.clock.advance(101, step=101))
    pull(fake, name, arm=arm["id"], card=arm["card"], **kwargs)
    return asyncio.run(exp.on_pull(name))


def test_trials_open_only_on_farm_and_actual_28_day_receipts(fake, experiment):
    exp, _ = experiment
    candidate(fake)
    with fake.conn:
        fake.conn.execute("UPDATE tg_pull SET received=?", (fake.clock.now()-1,))
    assert not exp.ready()
    assert asyncio.run(exp.open_trials()) == 0


def test_nonfarm_does_not_open(fake, experiment):
    exp, _ = experiment
    candidate(fake)
    pull(fake, "recent-wipe", outcome="wiped")
    assert asyncio.run(exp.open_trials()) == 0


def test_basis_is_fixed_and_unknown_covariates_are_not_zero(fake, experiment):
    exp, _, _, arm, _ = open_one(fake, experiment)
    assert observe(fake, exp, "other-basis", arm, basis="mechanics_deaths_time") is None
    assert observe(fake, exp, "unknown-covariate", arm, covariates={"drunk": "unknown"}) is None
    assert store.obs_rows(fake.conn) == []


def test_no_human_or_benchmark_experimental_route(fake, experiment):
    exp, run, _, arm, _ = open_one(fake, experiment)
    run.human_in_raid = True
    assert exp.arm_for(CTX, run) is None
    run.human_in_raid = False
    with fake.conn:
        fake.conn.execute("UPDATE guild_raid SET offline=0 WHERE raid_id=?", (run.session.raid_id,))
    assert exp.arm_for(CTX, run) is None
    assert observe(fake, exp, "benchmark", arm, kind="benchmark") is None


def test_wipe_benches_arm_for_run_and_replay_keeps_counter(fake, experiment, monkeypatch):
    exp, run, trial, arm, _ = open_one(fake, experiment)
    monkeypatch.setattr(bandit, "decide", lambda *args, **kwargs: bandit.Verdict("continue", None, "test"))
    assert observe(fake, exp, "wipe", arm, outcome="wiped").action == "continue"
    assert asyncio.run(exp.on_pull("wipe")) is None
    assert fake.conn.execute("SELECT wipes FROM tg_arm WHERE id=?", (arm["id"],)).fetchone()[0] == 1
    # Restore farm evidence without creating observations on the benched arm.
    for name in ("recover-a", "recover-b", "recover-c"):
        asyncio.run(fake.clock.advance(101, step=101))
        pull(fake, name)
    choice = exp.arm_for(CTX, run)
    assert choice is None or choice[1] != arm["id"]


def test_promotion_is_atomic_and_retains_previous(fake, experiment, monkeypatch):
    exp, _, trial, arm, champion = open_one(fake, experiment)
    monkeypatch.setattr(bandit, "decide", lambda *args, **kwargs: bandit.Verdict("promote", arm["id"], "test"))
    assert observe(fake, exp, "winner", arm).action == "promote"
    row = store.champion(fake.conn, CTX)
    assert row["id"] == arm["card"] and row["prev_champion"] == champion["card"]
    assert asyncio.run(exp.on_pull("winner")) is None
    assert len(store.obs_rows(fake.conn)) == 1
    assert asyncio.run(exp.rollback(CTX))
    assert store.champion(fake.conn, CTX)["id"] == champion["card"]
    assert asyncio.run(exp.stock(CTX))
    assert json.loads(store.champion(fake.conn, CTX)["directives"]) == []


def test_failed_verdict_rolls_back_receipt_counter_and_promotion(fake, experiment, monkeypatch):
    exp, _, _, arm, _ = open_one(fake, experiment)
    monkeypatch.setattr(exp, "_verdict", lambda *args: (_ for _ in ()).throw(RuntimeError("injected")))
    with pytest.raises(RuntimeError, match="injected"):
        observe(fake, exp, "retry", arm, outcome="wiped")
    assert store.obs_rows(fake.conn) == []
    assert fake.conn.execute("SELECT wipes FROM tg_arm WHERE id=?", (arm["id"],)).fetchone()[0] == 0
    monkeypatch.setattr(exp, "_verdict", lambda *args: None)
    assert asyncio.run(exp.on_pull("retry")) is not None
    assert len(store.obs_rows(fake.conn)) == 1


def test_his_champion_requires_three_offline_observations(fake, experiment, monkeypatch):
    exp, _, _, _, champion = open_one(fake, experiment)
    monkeypatch.setattr(bandit, "decide", lambda *args, **kwargs: bandit.Verdict("continue", None, "test"))
    for name in ("champ-a", "champ-b"):
        observe(fake, exp, name, champion)
    asyncio.run(fake.clock.advance(101, step=101))
    pull(fake, "his-early", card=champion["card"], kind="his")
    assert asyncio.run(exp.on_pull("his-early")) is None
    observe(fake, exp, "champ-c", champion)
    asyncio.run(fake.clock.advance(101, step=101))
    pull(fake, "his-ready", card=champion["card"], kind="his")
    assert asyncio.run(exp.on_pull("his-ready")) is not None
    assert len(store.obs_rows(fake.conn, champion["id"])) == 4


def test_exact_arm_card_scope_provenance(fake, experiment):
    exp, _, _, arm, champion = open_one(fake, experiment)
    asyncio.run(fake.clock.advance(101, step=101))
    pull(fake, "wrong-card", arm=arm["id"], card=champion["card"])
    assert asyncio.run(exp.on_pull("wrong-card")) is None
    assert observe(fake, exp, "wrong-context", arm, boss=1) is None


def test_pooled_trial_one_scope_and_one_changed_lever(fake, experiment):
    exp, run = experiment
    candidate(fake, context="631:*:0", lever="threat", value="+")
    candidate(fake)
    assert asyncio.run(exp.open_trials()) == 2
    card, arm = exp.arm_for(CTX, run)
    assert len(card.directives) <= 1
    assert fake.conn.execute("SELECT card FROM tg_arm WHERE id=?", (arm,)).fetchone()[0] == card.id


def test_rotation_metric_is_explicitly_unavailable(fake, experiment):
    exp, _ = experiment
    ident = candidate(fake, context="631:*:0", lever="rotation", value="tg_example")
    assert asyncio.run(exp.open_trials()) == 0
    assert exp.refusals[ident] == exp.SPEC_UNAVAILABLE


def test_drift_missing_revision_blocks_and_changed_run_has_only_stock(fake, experiment):
    exp, run, _, _, _ = open_one(fake, experiment)
    head = fake.cfg.core_dir / "modules" / drift.MODULES[0] / ".git" / "HEAD"
    head.write_text("unknown", encoding="ascii")
    assert exp.arm_for(CTX, run) is None
    assert asyncio.run(exp.open_trials()) == 0
    assert not asyncio.run(exp.drift_check(run))
    head.write_text("b" * 40, encoding="ascii")
    assert asyncio.run(exp.drift_check(run))
    assert asyncio.run(exp.open_trials()) == 0
    assert store.trial_rows(fake.conn)[0]["status"] == "drift"


def test_revision_reader_loose_packed_common_and_external_refusal(tmp_path):
    revision_tree(tmp_path)
    git = tmp_path / "modules" / drift.MODULES[0] / ".git"
    (git / "HEAD").write_text("ref: refs/heads/main", encoding="ascii")
    (git / "packed-refs").write_text("# packed refs\n" + "b" * 40 + " refs/heads/main\n", encoding="ascii")
    assert drift.revisions(tmp_path)[drift.MODULES[0]] == "b" * 40
    (git / "HEAD").write_text("ref: refs/../../outside", encoding="ascii")
    assert drift.MODULES[0] not in drift.revisions(tmp_path)
    (git / "HEAD").write_text("ref: refs/heads/main", encoding="ascii")
    (git / "commondir").write_text(str(tmp_path.parent), encoding="ascii")
    assert drift.MODULES[0] not in drift.revisions(tmp_path)


def test_actual_ols_threshold_recomputes_every_observation_and_ignores_rolling_scores(fake, experiment):
    exp, _, trial, challenger, champion = open_one(fake, experiment)
    before = None
    for index in range(17):
        arm = challenger if index % 2 else champion
        gear = 250 + (index % 7 - 3)
        asyncio.run(fake.clock.advance(200, step=200))
        name = f"fit-{index:02d}"
        pull(fake, name, arm=arm["id"], card=arm["card"], duration=100 + 2 * (gear - 250),
             score=9000 if index % 2 else -7000, covariates={"ilvl": gear})
        verdict = asyncio.run(exp.on_pull(name))
        assert verdict is not None and verdict.action == "continue"
        if index == 15:  # Three frozen baseline rows plus sixteen = nineteen.
            before = [r["value"] for r in store.obs_rows(fake.conn)]
    assert any(abs(x) > .01 for x in before)
    assert [r["value"] for r in store.obs_rows(fake.conn)] == pytest.approx([0] * 17, abs=1e-10)
    receipt = json.loads(fake.conn.execute("SELECT value FROM meta WHERE key=?", (f"tg:obs:{champion['id']}:fit-00",)).fetchone()[0])
    assert receipt["ledger_score"] == -7000
    assert receipt["score"] == pytest.approx(-6.2)
    assert receipt["received"] > trial["opened"] and receipt["member_roles"][0]["role"] == "tank"
    transform = json.loads(fake.conn.execute("SELECT value FROM meta WHERE key=?", (f"tg:transform:{trial['id']}",)).fetchone()[0])
    assert transform[CTX]["sd"] == 10 and transform[CTX]["n"] == 20


def test_stock_on_first_farm_pull_every_third_iso_week(fake, experiment):
    exp, run, _, _, champion = open_one(fake, experiment)
    asyncio.run(fake.clock.advance(7 * 86400, step=7 * 86400))  # ISO week 45.
    card, arm = exp.arm_for(CTX, run)
    assert card.directives == () and arm == champion["id"]


def test_pooled_veto_requires_four_pulls_per_arm_in_context(fake, experiment, monkeypatch):
    exp, _ = experiment
    for name, age in (("second-a", 300), ("second-b", 250), ("second-c", 200)):
        pull(fake, name, when=fake.clock.now()-age, boss=1, raid=None)
    ident = candidate(fake, context="631:*:0", lever="threat", value="+")
    assert asyncio.run(exp.open_trials()) == 1
    trial = store.trial_rows(fake.conn)[0]
    arms = exp._arms(trial["id"])
    challenger = next(a for a in arms if exp._card(a["card"]).lineage == ident)
    champion = next(a for a in arms if a["champion"])
    monkeypatch.setattr(bandit, "decide", lambda *args, **kwargs: bandit.Verdict("continue", None, "test"))
    for index in range(4):
        observe(fake, exp, f"veto-champ-{index}", champion, boss=1)
        observe(fake, exp, f"veto-candidate-{index}", challenger, boss=1, score=-30)
    observe(fake, exp, "few-champ", champion)
    monkeypatch.setattr(bandit, "below", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(bandit, "decide", lambda *args, **kwargs: bandit.Verdict("promote", challenger["id"], "test"))
    observe(fake, exp, "overall-winner", challenger, score=50)
    assert json.loads(store.champion(fake.conn, "631:*:0")["vetoes"]) == ["631:1:0"]


def test_summary_delayed_once_daily_receipt_and_no_inline_send(fake, experiment, monkeypatch):
    # Stage2 supplies the deterministic text renderer; this standalone core
    # test stubs only that presentation dependency, never the speech receipt.
    monkeypatch.setitem(sys.modules, "leeroy_mind.training.lines",
                        SimpleNamespace(template=lambda key, **facts: facts["what"]))
    exp, run, _, arm, _ = open_one(fake, experiment)
    monkeypatch.setattr(bandit, "decide", lambda *args, **kwargs: bandit.Verdict("continue", None, "test"))
    observe(fake, exp, "summary-pull", arm)
    assert fake.said == []
    ended = fake.clock.now()
    with fake.conn:
        fake.conn.execute("UPDATE guild_raid SET ended=? WHERE raid_id=?", (ended, run.session.raid_id))
    assert not asyncio.run(exp.offline_summary(run, ended))
    asyncio.run(fake.clock.advance(60, step=60))
    assert asyncio.run(exp.offline_summary(run, ended))
    assert not asyncio.run(exp.offline_summary(run, ended))
    assert len(fake.said) == 1


def test_external_worktree_requires_exact_backlink_and_bounded_common(tmp_path):
    core = tmp_path / "checkout"
    revision_tree(core)
    module = core / "modules" / drift.MODULES[0]
    # Replace this test's known .git directory with a standard gitfile.
    (module / ".git" / "HEAD").unlink()
    (module / ".git").rmdir()
    common = tmp_path / "repository" / ".git"
    git = common / "worktrees" / "module-checkout"
    git.mkdir(parents=True)
    (module / ".git").write_text("gitdir: " + str(git), encoding="ascii")
    (git / "gitdir").write_text(str(module / ".git"), encoding="ascii")
    (git / "commondir").write_text("../..", encoding="ascii")
    (git / "HEAD").write_text("ref: refs/heads/main", encoding="ascii")
    (common / "packed-refs").write_text("c" * 40 + " refs/heads/main\n", encoding="ascii")
    assert drift.revisions(core)[drift.MODULES[0]] == "c" * 40
    (git / "gitdir").write_text(str(core / "unrelated"), encoding="ascii")
    assert drift.MODULES[0] not in drift.revisions(core)


def test_producer_shaped_pooled_candidate_has_new_scope_and_explicit_unspoken_proposal(fake, experiment):
    exp, run = experiment
    original = Card(None, CTX, 7, "candidate", "progression", (Directive("threat", "+"),),
                    call="dps wait for the tank", assignment={"ranged": "back"}, evidence=("observed-aggro",),
                    created=fake.clock.now(), changed=fake.clock.now())
    ident = store.put_card(fake.conn, original)
    before = dict(fake.conn.execute("SELECT * FROM tg_card WHERE id=?", (ident,)).fetchone())
    assert asyncio.run(exp.open_trials()) == 1
    assert dict(fake.conn.execute("SELECT * FROM tg_card WHERE id=?", (ident,)).fetchone()) == before
    trial = store.trial_rows(fake.conn)[0]
    assert trial["scope"] == "631:*:0"
    arm = next(a for a in exp._arms(trial["id"]) if not a["champion"])
    card = exp._card(arm["card"])
    assert card.context == "631:*:0" and card.id != ident and card.lineage == ident
    split = card.evidence[-1]
    assert split["prospective_call"] == original.call and split["prospective_assignment"] == dict(original.assignment)
    assert split["speech_tested"] is False and card.call != original.call and dict(card.assignment) != dict(original.assignment)
    assert asyncio.run(exp.open_trials()) == 0
    with fake.conn:
        exp._close_open("retired")
    assert asyncio.run(exp.open_trials()) == 0  # Exact original->derived receipt survives closure.


@pytest.mark.parametrize("scope,field", [(CTX, "version"), ("631:*:0", "call")])
def test_baseline_version_or_content_change_refuses_selection_and_observation(fake, experiment, scope, field):
    exp, run, _, arm, _ = open_one(fake, experiment)
    assert exp.arm_valid(CTX, arm["id"])
    with fake.conn:
        if field == "version":
            fake.conn.execute("UPDATE tg_card SET version=version+1 WHERE context=? AND status='champion'", (scope,))
        else:
            fake.conn.execute("UPDATE tg_card SET call='new plan' WHERE context=? AND status='champion'", (scope,))
    assert not exp.arm_valid(CTX, arm["id"])
    assert exp.arm_for(CTX, run) is None
    assert observe(fake, exp, "stale-baseline", arm) is None
    assert store.obs_rows(fake.conn) == []


def test_three_champion_threshold_reexpresses_old_candidate_under_same_scale(fake, experiment):
    exp, _, trial, arm, champ = open_one(fake, experiment)
    for name, selected, duration in (("candidate-first", arm, 102), ("champ-a", champ, 100),
                                     ("champ-b", champ, 100), ("champ-c", champ, 100)):
        asyncio.run(fake.clock.advance(200, step=200))
        pull(fake, name, arm=selected["id"], card=selected["card"], duration=duration)
        assert asyncio.run(exp.on_pull(name)).action == "continue"
        if name == "champ-b":
            earlier = store.obs_rows(fake.conn, arm["id"])[0]["value"]
    after = store.obs_rows(fake.conn, arm["id"])[0]["value"]
    assert earlier != pytest.approx(after)
    assert after == pytest.approx(-.06)
    assert [r["value"] for r in store.obs_rows(fake.conn, champ["id"])] == [0, 0, 0]
    transform = json.loads(fake.conn.execute("SELECT value FROM meta WHERE key=?", (f"tg:transform:{trial['id']}",)).fetchone()[0])
    assert transform[CTX]["mean"] == -8 and transform[CTX]["sd"] == 10


def test_equal_receipts_have_equal_final_transform_and_selection_in_reverse_callback_order(fake, experiment):
    exp, run, trial, challenger, champion = open_one(fake, experiment)
    names = []
    for index in range(17):
        arm = challenger if index % 2 else champion
        gear = 250 + (index % 7 - 3)
        asyncio.run(fake.clock.advance(200, step=200))
        name = f"arrival-{index:02d}"
        names.append(name)
        pull(fake, name, arm=arm["id"], card=arm["card"], duration=100 + 2 * (gear - 250),
             score=1000 * index, covariates={"ilvl": gear})
    other = copy.copy(fake)
    other.conn = sqlite3.connect(":memory:")
    other.conn.row_factory = sqlite3.Row
    fake.conn.backup(other.conn)
    other_exp = Experimenter(other, other.cfg)
    try:
        for name in names:
            assert asyncio.run(exp.on_pull(name)).action == "continue"
        for name in reversed(names):
            assert asyncio.run(other_exp.on_pull(name)).action == "continue"
        assert [dict(r) for r in store.obs_rows(fake.conn)] == [dict(r) for r in store.obs_rows(other.conn)]
        key = f"tg:transform:{trial['id']}"
        assert fake.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0] == other.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]
        assert exp.arm_for(CTX, run)[1] == other_exp.arm_for(CTX, run)[1]
    finally:
        other.conn.close()


def test_pooled_promotion_retires_stale_boss_trial_preserves_evidence_and_admits_new_proposal(fake, experiment, monkeypatch):
    exp, _ = experiment
    boss_source = candidate(fake)
    candidate(fake, lever="threat", value="+")
    assert asyncio.run(exp.open_trials()) == 2
    boss_trial = store.trial_rows(fake.conn, CTX, status="open")[0]
    pooled_trial = store.trial_rows(fake.conn, "631:*:0", status="open")[0]
    boss_arm = next(a for a in exp._arms(boss_trial["id"]) if not a["champion"])
    pooled_arm = next(a for a in exp._arms(pooled_trial["id"]) if not a["champion"])
    monkeypatch.setattr(bandit, "decide", lambda *args, **kwargs: bandit.Verdict("continue", None, "test"))
    observe(fake, exp, "preserved-boss-observation", boss_arm)
    old_observations = [dict(row) for row in store.obs_rows(fake.conn, boss_arm["id"])]
    receipt_key = f"tg:obs:{boss_arm['id']}:preserved-boss-observation"
    old_receipt = fake.conn.execute("SELECT value FROM meta WHERE key=?", (receipt_key,)).fetchone()[0]
    original = dict(fake.conn.execute("SELECT * FROM tg_card WHERE id=?", (boss_source,)).fetchone())
    consumed = fake.conn.execute("SELECT value FROM meta WHERE key=?", (f"tg:proposal:{boss_source}",)).fetchone()[0]
    monkeypatch.setattr(bandit, "decide", lambda *args, **kwargs: bandit.Verdict("promote", pooled_arm["id"], "test"))
    observe(fake, exp, "pooled-winner", pooled_arm)
    assert not exp.arm_valid(CTX, boss_arm["id"])
    fresh = candidate(fake, value="near")
    assert asyncio.run(exp.open_trials()) == 1
    retired = fake.conn.execute("SELECT * FROM tg_trial WHERE id=?", (boss_trial["id"],)).fetchone()
    assert retired["status"] == "retired" and retired["verdict"] == "baseline_changed"
    assert {r["status"] for r in fake.conn.execute("SELECT status FROM tg_arm WHERE trial=?", (boss_trial["id"],))} == {"retired"}
    assert [dict(row) for row in store.obs_rows(fake.conn, boss_arm["id"])] == old_observations
    assert fake.conn.execute("SELECT value FROM meta WHERE key=?", (receipt_key,)).fetchone()[0] == old_receipt
    assert dict(fake.conn.execute("SELECT * FROM tg_card WHERE id=?", (boss_source,)).fetchone()) == original
    assert fake.conn.execute("SELECT value FROM meta WHERE key=?", (f"tg:proposal:{boss_source}",)).fetchone()[0] == consumed
    audit = json.loads(fake.conn.execute("SELECT value FROM meta WHERE key=?", (f"tg:trial_invalidated:{boss_trial['id']}",)).fetchone()[0])
    assert audit["reason"] == "baseline_changed" and CTX in audit["contexts"]
    assert audit["preserved_observations"] == [{"arm": boss_arm["id"], "observations": 1}]
    replacement = store.trial_rows(fake.conn, CTX, status="open")[0]
    replacement_arm = next(a for a in exp._arms(replacement["id"]) if not a["champion"])
    assert exp._card(replacement_arm["card"]).lineage == fresh
    assert exp.arm_valid(CTX, replacement_arm["id"])
    assert asyncio.run(exp.open_trials()) == 0
