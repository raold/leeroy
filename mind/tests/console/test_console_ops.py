"""Ops uses synthetic databases/files and fake process probes; never live control or models."""
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from console.rig import http, make_app
from leeroy_mind.console import opsview as ops
from leeroy_mind.health import Check

SECRET = "PRIVATE_SENTINEL memory chat token password"
NOW = 1794150000


def deadline():
    return time.monotonic() + 10


def test_switches_read_booleans_only(tmp_path):
    (tmp_path/'mind.toml').write_text('[learn]\nenabled=true\nbandit=true\n[signals]\nenabled=false\n[unknown]\nsecret="PRIVATE_SENTINEL"')
    rows = ops.switches_view(tmp_path,deadline(),mind_started=None)['rows']
    states = {(r['table'],r['key']):r['state'] for r in rows}
    assert states['learn','enabled'] == states['learn','bandit'] == 'on'
    assert states['signals','enabled'] == 'off'
    assert 'PRIVATE_SENTINEL' not in json.dumps(rows) and all(r['table']!='unknown' for r in rows)


@pytest.mark.parametrize('text', ['[learn]\nenabled=true\nbandit="PRIVATE_SENTINEL"',
    'learn="PRIVATE_SENTINEL"', '[server]\nenabled=true\ndms_per_day=99',
    '[guards]\nenabled=true\nrepeat_cosine=2', '[claims]\nenabled=true\nunknown=true'])
def test_invalid_table_reads_as_invalid_off(tmp_path,text):
    (tmp_path/'mind.toml').write_text(text+'\n[signals]\nenabled=true')
    table = 'learn' if 'learn' in text else text.split(']')[0][1:]
    rows = ops.switches_view(tmp_path,deadline(),mind_started=None)['rows']
    assert all(r['state']=='invalid' for r in rows if r['table']==table)
    assert next(r for r in rows if r['table']=='signals')['state']=='on'
    assert 'PRIVATE_SENTINEL' not in json.dumps(rows)


def test_unmet_mind_needs_are_listed(tmp_path):
    (tmp_path/'mind.toml').write_text('[learn]\nbandit=true\n')
    rows = ops.switches_view(tmp_path,deadline(),mind_started=None)['rows']
    assert next(r for r in rows if r['key']=='bandit')['unmet'] == ['learn.enabled']


def test_restart_flag_when_toml_is_newer_than_the_mind(tmp_path):
    path = tmp_path/'mind.toml'
    path.write_text('[signals]\nenabled=true')
    changed = path.stat().st_mtime
    assert all(r['restart'] for r in ops.switches_view(tmp_path,deadline(),mind_started=changed-1)['rows'])
    assert not any(r['restart'] for r in ops.switches_view(tmp_path,deadline(),mind_started=changed+1)['rows'])


def test_blocklist_presence_only(tmp_path):
    (tmp_path/'realm').mkdir()
    (tmp_path/'realm'/'blocklist.txt').write_text(SECRET)
    rows = ops.switches_view(tmp_path,deadline(),mind_started=None)['rows']
    assert next(r for r in rows if r['table']=='realm')['blocklist']=='present'
    assert SECRET not in json.dumps(rows)


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def put(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))


def add_night(conn, *, state="done"):
    conn.execute("INSERT INTO night_run(night,window_start,window_end,status,started,finished,error,plan) "
                 "VALUES('2026-11-07',1,2,?,?,?,?,?)", (state, NOW-100, NOW-10 if state == "done" else None,
                     json.dumps({"reflect": 2, "private": SECRET}),
                     json.dumps({"budget_s": 7200, "estimate_s": 3600, "private": SECRET})))
    conn.execute("INSERT INTO night_stage VALUES('2026-11-07','reflect',?,?,3,42)", (NOW-90, NOW-30))
    conn.execute("INSERT INTO night_step VALUES('2026-11-07','reflect',?,?)", (SECRET, NOW-30))


def add_hold(conn, ident="a"*32, guids=(11, 12), state="unknown"):
    conn.execute("INSERT INTO realm_action(command_id,role,policy,owner,rank,kind,payload,"
                 "reserved_manifest,guild,human,state,transport_state,evidence,created,updated) "
                 "VALUES(?,'root','ordinary','test',1,'walk',?,?,21,1,?,'settled',?,?,?)",
                 (ident, json.dumps({"private": SECRET}), json.dumps(guids), state,
                  json.dumps({"job": {"reason": "busy:combat", "private": SECRET}}), NOW-4000, NOW-20))


def add_raid(conn, rid, offline):
    conn.execute("INSERT INTO guild_raid(raid_id,started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid,kills,wipes) "
                 "VALUES(?,?,'naxx:10:0',10,0,?,11,11,12,2,1)", (rid, NOW-300, offline))
    conn.execute("INSERT INTO guild_attendance VALUES(?,11,'raided')", (rid,))


def test_night_projects_counts_times_budget_without_unit_or_error_text(store, data):
    add_night(store.conn)
    log = data / "logs" / "mind.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text(json.dumps({"ts": datetime.fromtimestamp(NOW-50, timezone.utc).isoformat(),
        "night": "2026-11-07", "event": "nightly_unit_failed", "stage": "reflect",
        "error": "ValueError", "unit": SECRET, "msg": SECRET}) + "\n", encoding="utf-8")
    got = ops.night_view(store.conn, data, NOW, deadline(), {})
    assert got["running"] is False
    assert got["stages"][0]["units"] == 1
    assert got["stages"][0]["failures"] == 2
    assert got["stages"][0]["error_code"] == "ValueError"
    assert got["budget"] == {"budget_s": 7200, "estimate_s": 3600}
    assert got["next_after"] == NOW-10+6*3600
    assert SECRET not in json.dumps(got)


def test_saved_running_is_not_proof_of_current_task(store, data):
    add_night(store.conn, state="running")
    assert ops.night_view(store.conn, data, NOW, deadline(), {"mind": "running"})["running"] is None
    assert ops.night_view(store.conn, data, NOW, deadline(), {"mind": "stopped"})["running"] is False


@pytest.mark.parametrize('enabled', [True, False])
def test_night_rule_quiet_window(store, data, enabled):
    add_night(store.conn)
    (data / 'mind.toml').write_text('[server]\nenabled=' + str(enabled).lower())
    rule = ops.night_view(store.conn, data, NOW, deadline(), {})['next_rule']
    if enabled:
        assert 'between 03:00 and 08:00 once he has been offline 10 minutes' in rule
    else:
        assert 'between' not in rule
        assert 'with the owner offline 10 minutes' in rule


def test_missing_failure_summary_does_not_imply_clean_night(store, data):
    add_night(store.conn)
    store.conn.execute("UPDATE night_run SET error=NULL")
    got = ops.night_view(store.conn, data, NOW, deadline(), {})
    assert got["failures_complete"] is False and got["failure_source"] == "logs"
    assert got["stages"][0]["failures"] is None


def test_failed_stage_without_finished_stage_receipt_is_shown(store, data):
    add_night(store.conn)
    store.conn.execute("DELETE FROM night_stage")
    got = ops.night_view(store.conn, data, NOW, deadline(), {})
    assert got["stages"][0]["stage"] == "reflect"
    assert got["stages"][0]["failures"] == 2 and got["stages"][0]["finished"] is None


def test_realm_holds_unique_members_and_copy_text_only(store):
    add_hold(store.conn)
    add_hold(store.conn, "b"*32, (12, 13), "conflict")
    got = ops.realm_view(store.conn, NOW)
    assert got["held_members"] == 3
    assert got["roots"][0]["native_reason"] == "busy:combat"
    assert got["roots"][0]["release_command"] == "realm release " + "a"*32 + " --reason reviewed_stale_hold"
    assert "conflicts" in got["release_note"]
    assert SECRET not in json.dumps(got)


def test_realm_hold_counts_a_failed_leaves_released_members_as_free(store):
    add_hold(store.conn)
    store.conn.execute("INSERT INTO realm_action(command_id,role,policy,owner,rank,kind,payload,"
                       "reserved_manifest,guild,human,state,transport_state,evidence,created,updated) "
                       "VALUES(?,'root','ordinary','raid',1,'leave_pvp','{}','[21,22,23]',21,1,'unknown','settled',?,?,?)",
                       ("b"*32, json.dumps({"job": {"state": "failed", "reason": "cleanup_manifest_readback_changed",
                                                    "released": [21, 23]}}), NOW-4000, NOW-20))
    got = ops.realm_view(store.conn, NOW)
    assert got["held_members"] == 3
    assert [r["members"] for r in got["roots"]] == [2, 1]
    # Proof that names a member outside its manifest, or a conflict, frees nobody.
    store.conn.execute("UPDATE realm_action SET evidence=? WHERE command_id=?",
                       (json.dumps({"job": {"state": "failed", "released": [21, 99]}}), "b"*32))
    assert ops.realm_view(store.conn, NOW)["held_members"] == 5
    store.conn.execute("UPDATE realm_action SET state='conflict',evidence=? WHERE command_id=?",
                       (json.dumps({"job": {"state": "failed", "released": [21, 23]}}), "b"*32))
    assert ops.realm_view(store.conn, NOW)["held_members"] == 5


def test_realm_unentered_rows_do_not_claim_body_hold(store):
    add_hold(store.conn)
    store.conn.execute("UPDATE realm_action SET transport_state='not_entered',state='prepared'")
    assert ops.realm_view(store.conn, NOW)["held_members"] == 0


def test_malformed_hold_is_not_reported_clean(store):
    add_hold(store.conn)
    store.conn.execute("UPDATE realm_action SET reserved_manifest='{}'")
    with pytest.raises(ValueError):
        ops.realm_view(store.conn, NOW)


def test_oversized_manifest_is_counted_not_fatal(store):
    add_hold(store.conn)
    add_hold(store.conn, "b"*32, tuple(range(1, 20000)))
    got = ops.realm_view(store.conn, NOW)
    assert got["manifest_too_large"] == 1
    assert got["held_members"] == 2 and len(got["roots"]) == 1


def test_badge_counts_come_from_health_and_realm_only(tmp_path):
    seen = []
    def source(name, value):
        def run():
            seen.append(name)
            return ops.panel(**value)
        return run
    view = ops.OpsView(tmp_path, tmp_path, sources={
        "health": source("health", {"fails": 2}), "realm": source("realm", {"held_members": 4})})
    got = asyncio.run(view.badge())
    assert got["counts"] == {"fails": 2, "held_members": 4}
    assert sorted(seen) == ["health", "realm"]


def test_realm_direct_unknowns_and_desks(store):
    store.conn.execute("INSERT INTO realm_direct(command_id,owner,ref,kind,payload_sha256,guild,human,"
        "process_id,expects_job,state,prepared_ms,dispatched_ms,settled_ms,reason) "
        "VALUES(?,'calendar','post:1','calendar_post',?,21,1,?,1,'unknown',1000,2000,3000,'unknown')", ("c"*32, "0"*64, SECRET))
    got = ops.realm_view(store.conn, NOW)
    assert got["direct_unknown_total"] == 1
    assert got["direct_unknowns"][0]["id"] == "c"*32
    assert got["desks"] == {"calendar": 3, "mail": None}
    assert SECRET not in json.dumps(got)


def test_raid_receipt_and_retained_progress_are_allowlisted(store):
    add_raid(store.conn, 1, 0)
    add_raid(store.conn, 2, 1)
    put(store.conn, "guild:offline:owned", {"id": "d"*32, "raid_id": 2, "phase": "accepted",
        "plan": {"raid_key": "naxx:10:0", "private": SECRET}, "accepted": [11, 12],
        "instance": 77, "deadline": NOW+60, "sent_at": NOW-300, "private": SECRET})
    payload = {"id": "d"*32, "kind": "offline_raid", "state": "progress", "stage": "running",
        "members_in_instance": 2, "last_progress_ts": (NOW-30)*1000, "boss_engaged": False,
        "kills": 2, "wipes": 1, "private": SECRET}
    store.conn.execute("INSERT INTO game_event(ts,type,payload) VALUES(?,'job',?)", (NOW-10, json.dumps(payload)))
    got = ops.raids_view(store.conn, NOW)
    assert got["guild"]["id"] == 1 and got["guild"]["members"] == 1
    assert got["offline"]["instance"] == 77 and got["offline"]["members"] == 2
    assert got["offline"]["progress"]["stale"] is False
    assert got["offline"]["progress"]["boss_engaged"] is False
    assert ops.raids_view(store.conn, NOW+200)["offline"]["progress"]["stale"] is True
    assert SECRET not in json.dumps(got)


def test_new_presession_receipt_does_not_inherit_last_raid_counts(store):
    add_raid(store.conn, 2, 1)
    put(store.conn, "guild:offline:owned", {"id": "e"*32, "phase": "awaiting_ack", "deadline": NOW+60})
    got = ops.raids_view(store.conn, NOW)["offline"]
    assert got["id"] is None and got["kills"] is None and got["deadline"] == NOW+60


def test_selfie_queue_held_counts_stages_booth_and_no_private_text(store):
    store.conn.execute("INSERT INTO selfie_set(guid,set_no,reason,status,requested) VALUES(11,1,'more','queued',?)", (NOW-100,))
    put(store.conn, "selfies:booth:last", {"finished": NOW-20, "sets": 2, "outcome": "done", "prompt": SECRET})
    got = ops.selfies_view(store.conn, NOW)
    assert got["counts"]["queued"] == 1
    assert got["held"] == [{"guid": 11, "set": 1, "reason": "no_persona"}]
    assert got["snaps"]["planned"] == 0 and got["booth"]["sets"] == 2
    assert SECRET not in json.dumps(got)


def test_backup_inventory_never_reads_dump_or_snapshot_contents(tmp_path):
    data = tmp_path / "data"
    snapshots = data / "snapshots"
    snapshots.mkdir(parents=True)
    for name in ("mind-start-20261107-120000.db", "mind-start-20261108-120000.db", "mind-eval-20261108-130000.db"):
        (snapshots / name).write_text(SECRET)
    (snapshots / "console.key").write_text(SECRET)
    (tmp_path / "db_backup" / "pre_update_20261108_120000").mkdir(parents=True)
    got = ops.backups_view(tmp_path, data, deadline())
    assert got["snapshots"]["count"] == 3
    assert set(got["snapshots"]["labels"]) == {"start", "eval"}
    assert got["game"]["name"] == "pre_update_20261108_120000"
    assert got["offmachine"]["status"] == "missing"
    assert SECRET not in json.dumps(got)


def test_snapshot_labels_preserve_numeric_suffixes_and_long_valid_names(tmp_path):
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    long_label = "pre-migrate-" + "x" * 90
    labels = ["pre-migrate", "pre-migrate-17", "pre-migrate-2", long_label]
    for label in labels:
        (snapshots / f"mind-{label}-20261108-120000.db").write_text(SECRET)
    (snapshots / "mind-pre-migrate-17-20261109-120000.db").write_text(SECRET)
    got = ops.backups_view(tmp_path, tmp_path, deadline())
    assert got["snapshots"]["count"] == 5
    assert got["snapshots"]["bytes"] == 5 * len(SECRET.encode())
    assert set(got["snapshots"]["labels"]) == set(labels)
    assert got["snapshots"]["labels"]["pre-migrate-17"]["stamp"] == "20261109-120000"
    assert got["snapshots"]["labels"]["pre-migrate"]["stamp"] == "20261108-120000"
    assert "same-second copy counter" in got["note"]
    assert SECRET not in json.dumps(got)


def make_backup(tmp_path):
    name = "guild-20261108T120000000000Z-abcdef12"
    store = tmp_path / "dest" / "leeroy-guild-backups-v1"
    folder = store / "sets" / name
    folder.mkdir(parents=True)
    marker = {"owner": "leeroy-guild-backup", "format": 1, "store_id": "a"*32}
    (store / ".guild-backup-store.json").write_text(json.dumps(marker))
    raw = json.dumps({**marker, "set_id": name, "files": {SECRET: {"size": 1}}}).encode()
    (folder / "manifest.json").write_bytes(raw)
    (folder / ".guild-backup-set.json").write_text(json.dumps({**marker, "set_id": name,
        "manifest_sha256": hashlib.sha256(raw).hexdigest()}))
    cfg = tmp_path / "backup.toml"
    cfg.write_text("destination = " + json.dumps((tmp_path / "dest").as_posix()))
    return cfg, folder


def test_offmachine_manifest_check_not_claimed_as_restore_test(tmp_path):
    cfg, folder = make_backup(tmp_path)
    got = ops.offmachine_view(cfg, deadline())
    assert got["manifest_verified"] is True
    assert "restore-test result is not stored" in got["verification"]
    assert SECRET not in json.dumps(got)
    state = {"owner": "leeroy-guild-backup", "format": 1, "store_id": "a"*32, "last_full_verify": NOW,
             "private": SECRET}
    (folder.parent.parent / ".verify-state.json").write_text(json.dumps(state))
    assert ops.offmachine_view(cfg, deadline())["last_full_verify"] == NOW
    state["store_id"] = "b"*32
    (folder.parent.parent / ".verify-state.json").write_text(json.dumps(state))
    assert ops.offmachine_view(cfg, deadline())["last_full_verify"] is None
    (folder / "manifest.json").write_text("{}")
    assert ops.offmachine_view(cfg, deadline())["status"] == "error"


@pytest.mark.parametrize("change", [{"format": True}, {"store_id": "foreign"}, {"owner": "foreign"}])
def test_offmachine_ownership_failures(tmp_path, change):
    cfg, folder = make_backup(tmp_path)
    marker = folder.parent.parent / ".guild-backup-store.json"
    marker.write_text(json.dumps({**json.loads(marker.read_text()), **change}))
    assert ops.offmachine_view(cfg, deadline())["status"] == "error"


def test_health_reuses_checks_but_suppresses_private_diagnostics(tmp_path):
    calls = []
    def fake(root, data):
        calls.append((root, data))
        return [Check("server-log", "ok", "Server.log: cast 120 members, pinned 120"),
                Check("health-config", "fail", SECRET), Check("endpoint", "fail", SECRET),
                Check("ollama-models", "note", SECRET)]
    got = ops.health_view(tmp_path, tmp_path, fake)
    assert calls == [(tmp_path, tmp_path)] and got["fails"] == 2
    assert got["checks"][0]["text"] == "Server.log: cast 120 members, pinned 120"
    assert SECRET not in json.dumps(got)


def test_health_includes_merged_stuck_checks_with_real_checks_and_fake_io(tmp_path):
    from test_health import Rig
    from leeroy_mind import health
    rig = Rig(tmp_path)
    before = (rig.data / "mind.db").stat().st_mtime_ns
    got = ops.health_view(rig.root, rig.data,
        lambda root, data: health.run_checks(root, data, probes=rig.probes()))
    names = {c["name"] for c in got["checks"]}
    assert {"offline-raid", "guild-raids", "night-failures", "evidence-cycles", "selfie-queue"} <= names
    assert (rig.data / "mind.db").stat().st_mtime_ns == before
    assert all(call[2] == "status" and call[0] != 7878 for call in rig.endpoint.calls)
    assert SECRET not in json.dumps(got)
    assert not (rig.data / "health-state.json").exists()


@pytest.mark.parametrize("name", ops.PANELS)
def test_every_panel_failure_is_contained_without_exception_text(tmp_path, name):
    def fail():
        raise OSError(SECRET)
    sources = {p: lambda: ops.panel() for p in ops.PANELS}
    sources[name] = fail
    got = asyncio.run(ops.OpsView(tmp_path, tmp_path, sources=sources).snapshot())
    assert got["panels"][name]["status"] == "error"
    assert all(p["status"] == "ok" for key, p in got["panels"].items() if key != name)
    assert SECRET not in json.dumps(got)


@pytest.mark.parametrize("view", [lambda c, d: ops.night_view(c, d, NOW, deadline(), {}),
    lambda c, d: ops.realm_view(c, NOW), lambda c, d: ops.raids_view(c, NOW), lambda c, d: ops.selfies_view(c, NOW)])
def test_older_db_reports_missing(view, tmp_path):
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(ops.Missing):
            view(conn, tmp_path)


def test_missing_install_has_independent_panel_statuses(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "schedules_view", lambda *a: ops.panel(tasks=[]))
    got = asyncio.run(ops.OpsView(tmp_path, tmp_path).snapshot())
    assert got["panels"]["health"]["status"] == "missing"
    for name in ("night", "realm", "raids", "selfies", "drift"):
        assert got["panels"][name]["status"] == "missing"
    assert got["panels"]["backups"]["snapshots"]["count"] == 0
    assert got["counts"] == {"fails": None, "held_members": None}


def test_busy_source_does_not_block_others_or_spawn_another_worker(tmp_path):
    release, entered = threading.Event(), threading.Event()
    calls = []
    def slow():
        calls.append(1)
        entered.set()
        release.wait(2)
        return ops.panel(fails=0)
    sources = {p: lambda: ops.panel() for p in ops.PANELS}
    sources["health"] = slow
    view = ops.OpsView(tmp_path, tmp_path, sources=sources, budgets={p: .04 for p in ops.PANELS})
    async def run():
        first = await view.snapshot()
        assert entered.is_set()
        assert first["panels"]["health"]["skipped"] == "budget"
        assert first["panels"]["realm"]["status"] == "ok"
        assert (await view.snapshot())["panels"]["health"]["skipped"] == "budget"
    try:
        started = time.monotonic()
        asyncio.run(run())
        assert time.monotonic() - started < .5
        assert calls == [1]
    finally:
        release.set()
        for worker in list(view._workers.values()):
            worker.join(2)


def test_health_cache_at_most_once_per_sixty_seconds(tmp_path):
    now, calls = [1000.], []
    def health():
        calls.append(1)
        return ops.panel(fails=2)
    sources = {p: lambda: ops.panel() for p in ops.PANELS}
    sources["health"] = health
    view = ops.OpsView(tmp_path, tmp_path, sources=sources, monotonic=lambda: now[0])
    asyncio.run(view.snapshot())
    now[0] += 59
    asyncio.run(view.snapshot())
    assert len(calls) == 1
    now[0] += 1
    asyncio.run(view.snapshot())
    assert len(calls) == 2


def test_database_is_read_only_and_interrupts_long_queries(store, data):
    store.conn.commit()
    with ops.database(data, deadline()) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM meta")
    with ops.database(data, time.monotonic()-.1) as conn:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            conn.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n").fetchone()


def test_query_deadline_is_reported_as_skipped(tmp_path):
    def interrupted():
        raise sqlite3.OperationalError("interrupted")
    view = ops.OpsView(tmp_path, tmp_path, sources={p: interrupted for p in ops.PANELS})
    got = asyncio.run(view.snapshot())
    assert all(p["skipped"] == "budget" for p in got["panels"].values())


def test_disabled_empty_subsystems_are_missing_but_old_holds_remain_visible(store, data):
    store.conn.commit()
    view = ops.OpsView(data.parent, data)
    assert view._source("realm", deadline(), {})["status"] == "missing"
    assert view._source("selfies", deadline(), {})["status"] == "missing"
    add_hold(store.conn)
    store.conn.commit()
    got = view._source("realm", deadline(), {})
    assert got["status"] == "ok" and got["enabled"] == "off" and got["held_members"] == 2


def test_api_read_only_key_host_origin_and_no_control_gates(tmp_path):
    async def go():
        app = make_app(tmp_path)
        app.controls.enabled = False
        app._ops = ops.OpsView(tmp_path, tmp_path, sources={p: lambda: ops.panel() for p in ops.PANELS})
        await app.start()
        try:
            assert (await http(app, "GET", "/api/ops", key=False)).status == 403
            assert (await http(app, "GET", "/api/ops", host="evil")).status == 403
            assert (await http(app, "GET", "/api/ops", origin="http://evil")).status == 403
            assert (await http(app, "GET", "/api/ops")).status == 200
            assert (await http(app, "POST", "/api/ops", body={}, origin=True)).status == 405
            assert app.mind.acts == []
        finally:
            await app.close()
    asyncio.run(go())


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


def test_drift_recognizes_squash_and_merge_cherry_picks_without_fetch(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    (tmp_path / "base").write_text("base")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Base")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "one").write_text("one")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "First squash (#12)")
    picked = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-b", "topic")
    (tmp_path / "two").write_text("two")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Second work")
    git(tmp_path, "checkout", "main")
    git(tmp_path, "merge", "--no-ff", "topic", "-m", "Merge pull request #13 from fixture/topic\n\nSecond merged change")
    merge = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "three").write_text("three")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Third unmatched (#14)")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(tmp_path, "checkout", "-b", "live", base)
    git(tmp_path, "cherry-pick", picked)
    git(tmp_path, "cherry-pick", "-m", "1", merge)
    got = ops.drift_view(tmp_path, deadline())
    assert got["branch"] == "live"
    assert got["prs"] == [{"number": 14, "title": "Third unmatched (#14)"}]


def test_schedule_only_asks_for_status_never_actions_or_arguments(tmp_path, monkeypatch):
    calls = []
    def command(args, root, end):
        calls.append(args[-1])
        return json.dumps([{"name": "leeroy-console", "installed": True, "state": "Ready",
            "last_run": "2026-11-08T12:00:00+00:00", "result": 0, "Arguments": SECRET},
            {"name": "leeroy-health", "installed": False}])
    monkeypatch.setattr(ops, "command", command)
    monkeypatch.setattr(ops.os, "name", "nt")
    got = ops.schedules_view(tmp_path, deadline())
    assert got["tasks"][0]["result"] == 0 and got["tasks"][1]["installed"] is False
    assert "Get-ScheduledTaskInfo" in calls[0]
    assert "Actions" not in calls[0] and "Register-" not in calls[0] and "Start-" not in calls[0]
    assert SECRET not in json.dumps(got)


def test_the_live_learn_keys_are_valid_and_the_cycle_has_a_row(tmp_path):
    (tmp_path/'mind.toml').write_text('[learn]\nenabled=true\nbandit=true\napply=true\ncycle=true\n')
    states = {(r['table'],r['key']):r['state'] for r in ops.switches_view(tmp_path,deadline(),mind_started=None)['rows']}
    assert states['learn','enabled'] == states['learn','cycle'] == 'on'
