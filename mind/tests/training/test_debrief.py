"""Synthetic raid evidence only; no services, live DB, DBC, models or native bridge."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import pytest

from leeroy_mind.training import cli
from leeroy_mind.training.debrief import raid_debrief
from leeroy_mind.training.debrief_logs import read_logs, native_record, mind_record
from leeroy_mind.raidplay import playbook_for
from training.test_tg_hurt import save

START = 1795262400  # fixed UTC fixture, not wall-clock dependent
COMMAND = "a" * 32


def raid(conn, rid=1, start=START, ended=START+600, key="icc", offline=1):
    conn.execute("INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid,kills,wipes,outcome) "
                 "VALUES(?,?,?,?,10,0,?,101,101,102,1,1,'cleared')", (rid, start, ended, key, offline))


def native(at, text):
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S ") + text + "\n"


def mind(at, text):
    return json.dumps(dict(ts=datetime.fromtimestamp(at, timezone.utc).isoformat(), event="log", msg=text)) + "\n"


@pytest.fixture
def sample(fake, tmp_path):
    conn = fake.conn
    raid(conn)
    save(fake, "first", start=int((START+10)*1000), members=((101,"ranged",((71218,2000,2),)),),
         failures=(("debuff", "test_check", 101, 2),), deaths=((101,71218),))
    save(fake, "second", start=int((START+200)*1000), outcome="killed", members=((102,"ranged",((71218,1000,1),)),))
    conn.execute("UPDATE tg_pull SET raid_id=1")
    conn.execute("INSERT INTO meta VALUES('guild:offline:owned',?)", (json.dumps(dict(raid_id=1,id=COMMAND)),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('human_guid','1')")
    conn.commit()
    (tmp_path / "native").mkdir()
    (tmp_path / "logs").mkdir()
    return fake, tmp_path


def report(sample, **kwargs):
    fake, root = sample
    return raid_debrief(fake.conn, data=root, native_dir=root/"native", tz=timezone.utc, **kwargs)


def mechanic():
    return playbook_for(631,4,0).mechanics[0].id


def response(at, moved=3, **kw):
    return native(at, f"mechanic_response id={kw.get('id',mechanic())} spell=71218 kind=spread members={moved} map=631 instance={kw.get('instance',77)} cancelled=false")


def test_exact_raid_scope_hurt_and_completed_response_counts(sample):
    fake, root = sample
    raid(fake.conn, 2, START-1000, START-500)
    save(fake, "older", start=int((START-900)*1000), members=((103,"ranged",((71218,99999,999),)),))
    fake.conn.execute("UPDATE tg_pull SET raid_id=2 WHERE id='older'")
    (root/"native"/"Server.log").write_text(response(START+20,3)+response(START+21,2)+response(START+220,4), encoding="utf-8")
    r = report(sample)
    boss = next(b for b in r["bosses"] if b["boss_id"] == 4)
    assert r["raid"]["id"] == 1
    assert (boss["pulls"],boss["kills"],boss["wipes"]) == (2,1,1)
    assert {(h["guid"],h["hits"]) for h in boss["hits"]} == {(101,2),(102,1)}
    assert boss["deaths"] == [dict(guid=101,spell=71218,count=1)]
    assert boss["failures"][0]["count"] == 2
    m = next(m for m in boss["mechanics"] if m["id"] == mechanic())
    assert (m["activations"],m["moved_total"],m["moved_max"]) == (3,9,4)
    assert "not arrivals" in r["evidence"]["responder"]
    assert any(b["pulls"] == 0 for b in r["bosses"])


@pytest.mark.parametrize("case", ["instance", "overlap", "unbound_pull", "boundary", "mechanic", "between"])
def test_uncertain_responses_are_not_assigned_to_boss(sample, case):
    fake, root = sample
    at, kw = START+20, {}
    if case == "instance": kw["instance"] = 88
    if case == "mechanic": kw["id"] = "icc_other_boss"
    if case == "boundary": at = START+130  # pull ends in this second
    if case == "between": at = START+150
    if case in ("overlap", "unbound_pull"):
        raid(fake.conn,2)
        save(fake,"overlap",start=int((START+10)*1000))
        if case == "overlap": fake.conn.execute("UPDATE tg_pull SET raid_id=2 WHERE id='overlap'")
    (root/"native"/"Server.log").write_text(response(at,**kw),encoding="utf-8")
    r = report(sample)
    assert len(r["unassigned_responses"]) == 1
    assert all(m["activations"] == 0 for b in r["bosses"] for m in b["mechanics"])


def test_all_rez_reasons_and_hazards_stay_unassigned_and_counts_are_lower_bounds(sample):
    _, root = sample
    lines = [native(START+20+i, f"group_rez_blocked reason={reason} dead=3 casters=1 fence=raid")
             for i,reason in enumerate(["combat","busy","no_spell","combat"])]
    lines += [native(START+40,"hazard_exit_yield guid=101 zones=2"), native(START-10,"group_rez_blocked reason=outside dead=1 casters=0")]
    (root/"native"/"Server.log").write_text("".join(lines),encoding="utf-8")
    r = report(sample)
    assert {e["reason"]:e["records"] for e in r["group_rez"]} == {"combat":2,"busy":1,"no_spell":1}
    assert r["hazards"] == [dict(event="hazard_exit_yield",guid=101,reason=None,records=1,maxima={"zones":2})]
    assert r["logs"]["complete"] is False
    assert "unassigned" in r["evidence"]["hazards"]


def test_hazard_source_details_survive_without_counting_overlaps_as_more_yields(sample):
    _, root = sample
    source = 'map=631 instance=77 spell=72754 kind=trigger entry=38757 hazard=17379390965131100001'
    other = 'map=631 instance=77 spell=70825 kind=dynamic entry=0 hazard=17379390965131100002'
    lines = [native(START+40, f'hazard_exit_yield guid=101 zones=2 {source} details=2'),
             native(START+40, f'hazard_exit_zone guid=101 {other}'),
             native(START+41, f'hazard_exit_yield guid=101 zones=1 {other} details=1')]
    (root/'native'/'Server.log').write_text(''.join(lines), encoding='utf-8')
    hazards = report(sample)['hazards']
    assert len(hazards) == 3
    assert sum(h['records'] for h in hazards if h['event'] == 'hazard_exit_yield') == 2
    assert {(h['spell'], h['kind'], h['map'], h['instance']) for h in hazards} == {
        (72754, 'trigger', 631, 77), (70825, 'dynamic', 631, 77)}
    assert hazards[0]['hazard'] == 17379390965131100001


@pytest.mark.parametrize('suffix', ['kind=private_payload entry=0 hazard=1',
                                   'kind=trigger entry=0 hazard=18446744073709551616',
                                   'kind=trigger entry=0 hazard=1 extra=private_payload'])
def test_hazard_diagnostics_reject_unrecognised_text_and_overflow(sample, suffix):
    _, root = sample
    (root/'native'/'Server.log').write_text(native(START+40,
        f'hazard_exit_zone guid=101 map=631 instance=77 spell=72754 {suffix}'), encoding='utf-8')
    result = report(sample)
    assert result['hazards'] == []
    assert 'private_payload' not in json.dumps(result)


def test_rotations_mind_native_bound_stop_short_full_wipe_and_terminal(sample):
    fake, root = sample
    fake.conn.execute("DELETE FROM meta WHERE key='guild:offline:owned'")
    fake.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,'guild_offline_raid',0,?)",
                     (START+590,json.dumps(dict(command=COMMAND,raid_id=1))))
    (root/"native"/"Server.log.2026-11-21 00-00-00").write_text(
        native(START+50,f"leeroy: raid {COMMAND} full_wipe dead=8 inside=8 restarts=1 kills=0")+
        native(START+180,f"leeroy: raid {COMMAND} short leader_present=1 present=7 min=8 members=10")+
        native(START+200,f"leeroy: raid {COMMAND} stopping: too_few elapsed_ms=200000 instance=77")+
        native(START+300,f"leeroy: offline_raid {COMMAND} failed: too_few (0 kills, 1 wipes); recovery retained=false")+
        native(START+200,f"leeroy: raid {'b'*32} short leader_present=0 present=1 min=8 members=10"),encoding="utf-8")
    (root/"logs"/"mind.log.2026-11-21").write_text(mind(START+350,"offline_raid_ended raid_id=1 outcome=too_few cause=- kills=0 wipes=1 raided=True")+
        mind(START+350,"offline_raid_ended raid_id=2 outcome=cleared cause=- kills=12 wipes=0 raided=True"),encoding="utf-8")
    r = report(sample)
    assert [e["event"] for e in r["offline"]] == ["offline_full_wipe","offline_short","offline_stopping","offline_terminal","offline_raid_ended"]
    assert r["offline"][1]["leader_present"] is True
    assert "unbound_native_offline_events" in r["issues"]


def test_discord_and_unrelated_payload_cannot_bind_native_job(sample):
    fake, root = sample
    fake.conn.execute("DELETE FROM meta WHERE key='guild:offline:owned'")
    fake.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,'discord_post',101,?)",
                     (START+300,json.dumps(dict(command=COMMAND,raid_id=1))))
    (root/"native"/"Server.log").write_text(native(START+200,f"leeroy: raid {COMMAND} full_wipe dead=8 inside=8 restarts=1 kills=0"),encoding="utf-8")
    r = report(sample)
    assert r["offline"] == []
    assert "native_job_binding_unavailable" in r["issues"]


def test_missing_logs_empty_schema_and_specific_unknown_raid_are_explicit(sample, fake, tmp_path):
    r = report(sample)
    assert r["logs"]["issues"]["native_no_files"]
    assert "no_matching_log_evidence" in r["issues"]
    assert report(sample,raid_id=99)["issues"] == ["raid_not_found"]
    fake.conn.execute("DELETE FROM tg_failure")
    fake.conn.execute("DELETE FROM tg_death")
    fake.conn.execute("DELETE FROM tg_member")
    fake.conn.execute("DELETE FROM tg_pull")
    fake.conn.execute("DELETE FROM guild_raid")
    assert report(sample)["issues"] == ["no_recorded_raid"]


def test_cli_uses_read_only_db_even_with_mind_lock_and_no_services(sample, capsys):
    fake, root = sample
    from leeroy_mind.app import acquire_instance_lock
    fake.conn.commit()
    path = root/"mind.db"
    before = hashlib.sha256(path.read_bytes()).digest()
    with acquire_instance_lock(root):
        assert cli.training_main(["debrief","--raid","1","--data-dir",str(root),"--native-log-dir",str(root/"native"),"--log-timezone","UTC"]) == 0
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert json.loads(capsys.readouterr().out)["raid"]["id"] == 1


@pytest.mark.parametrize("args", [["--raid","-1"],["--log-timezone","Not/AZone"]])
def test_cli_bad_inputs_are_clean_failures(sample, args):
    _,root = sample
    assert cli.training_main(["debrief","--data-dir",str(root),*args]) == 1


def test_parser_never_returns_chat_raw_payload_or_partial_match():
    secret = "PRIVATE_SENTINEL"
    assert native_record(native(START,"chat: group_rez_blocked reason=busy dead=1 casters=0"),timezone.utc) is None
    assert native_record(native(START,"group_rez_blocked reason=busy dead=1 casters=0 "+secret),timezone.utc) is None
    assert mind_record(mind(START,secret)) is None
    with pytest.raises(ValueError):
        native_record("2026-11-01 01:30:00 group_rez_blocked reason=busy dead=1 casters=0",ZoneInfo("America/New_York"))
    assert native_record(native(START,"group_rez_blocked reason=fenced dead=2 casters=1 fence=raid,realm"),timezone.utc)["fence"] == "raid,realm"


def test_log_byte_event_deadline_and_line_limits_are_visible(sample):
    _,root = sample
    path = root/"native"/"Server.log"
    path.write_text("x"*9000+"\n"+response(START+20)*3,encoding="utf-8")
    opts = dict(deadline=time.monotonic()+5,tz=timezone.utc)
    got = read_logs(root/"native",root,START,START+600,event_limit=2,**opts)
    assert len(got["events"]) == 2 and got["coverage"]["issues"]["event_limit"]
    assert got["coverage"]["issues"]["partial_or_oversized_line"]
    got = read_logs(root/"native",root,START,START+600,byte_limit=200,**opts)
    assert got["coverage"]["bytes"] <= 200 and got["coverage"]["issues"]["truncated_file"]
    got = read_logs(root/"native",root,START,START+600,deadline=0)
    assert got["coverage"]["issues"]["directory_budget"]


@pytest.mark.parametrize("key,boss",[("toc",3),("icc",9)])
def test_twins_valithria_actions_never_inferred_from_no_log(sample,key,boss):
    fake,_ = sample
    fake.conn.execute("UPDATE guild_raid SET raid_key=?",(key,))
    r = report(sample)
    b = next(b for b in r["bosses"] if b["map"] == (649 if key == "toc" else 631) and b["boss_id"] == boss)
    assert any(m["evidence"] == "no_direct_action_telemetry" for m in b["mechanics"])


def test_ops_uses_same_read_only_report_and_own_budget(sample):
    from leeroy_mind.console.opsview import OpsView
    _,root = sample
    p = OpsView(root,root)._source("debrief",time.monotonic()+4,{})
    assert p["status"] == "ok" and p["raid"]["id"] == 1


def test_recent_open_raid_does_not_hide_completed_report_and_exact_id_selects_it(sample):
    fake,_ = sample
    raid(fake.conn,2,START+1000,None)
    assert report(sample)["raid"]["id"] == 1
    r = report(sample,raid_id=2,now=START+1100)
    assert r["raid"]["id"] == 2 and r["raid"]["ended"] is None


def test_pull_limit_cannot_hide_competing_assignment(sample,monkeypatch):
    from leeroy_mind.training import debrief
    _,root = sample
    (root/"native"/"Server.log").write_text(response(START+20),encoding="utf-8")
    monkeypatch.setattr(debrief,"MAX_PULLS",1)
    r = report(sample)
    assert "pulls_truncated" in r["issues"]
    assert len(r["unassigned_responses"]) == 1


def test_partial_overlap_inside_same_raid_is_unassigned(sample):
    fake,root = sample
    fake.conn.execute("UPDATE tg_pull SET start_ms=?,end_ms=? WHERE id='second'",(int((START+20.5)*1000),int((START+40)*1000)))
    (root/"native"/"Server.log").write_text(response(START+20),encoding="utf-8")
    assert len(report(sample)["unassigned_responses"]) == 1


def test_reading_logs_allows_rotation_and_refuses_link_escape(sample):
    _,root = sample
    outside = root/"private.txt"
    outside.write_text(response(START+20),encoding="utf-8")
    path = root/"native"/"Server.log"
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    r = report(sample)
    assert r["logs"]["issues"]["linked_or_nonfile"]
    assert r["logs"]["records"] == 0


def test_hit_bound_and_known_spaced_check_names(sample,monkeypatch):
    from leeroy_mind.training import debrief
    fake,_ = sample
    fake.conn.execute("UPDATE tg_failure SET key='Big Bang'")
    fake.conn.execute("UPDATE tg_pull SET checks_coverage=?", (json.dumps(["spell:unresolved:Big Bang", "PRIVATE words"]),))
    monkeypatch.setattr(debrief,"MAX_HITS",1)
    r = report(sample)
    b = next(b for b in r["bosses"] if b["boss_id"] == 4)
    assert len(b["hits"]) == 1
    assert b["failures"][0]["key"] == "Big Bang"
    assert "spell:unresolved:Big Bang" in b["checks"] and "PRIVATE words" not in b["checks"]
    assert "member_hits_truncated" in r["issues"]


def test_bound_outcome_after_session_close_without_extending_unscoped_window(sample):
    fake,root = sample
    fake.conn.execute("DELETE FROM meta WHERE key='guild:offline:owned'")
    fake.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,'guild_offline_raid',0,?)",
                     (START+900,json.dumps(dict(command=COMMAND,raid_id=1))))
    (root/"logs"/"mind.log").write_text(mind(START+900,"offline_raid_ended raid_id=1 outcome=cleared cause=- kills=1 wipes=1 raided=True"),encoding="utf-8")
    (root/"native"/"Server.log").write_text(native(START+899,f"leeroy: offline_raid {COMMAND} done: cleared (1 kills, 1 wipes); recovery retained=false")+
        native(START+899,"group_rez_blocked reason=outside dead=1 casters=0")+response(START+899),encoding="utf-8")
    r = report(sample,now=START+1000)
    assert len(r["offline"]) == 2
    assert r["group_rez"] == [] and r["unassigned_responses"] == []
    assert "native_job_binding_unavailable" not in r["issues"]
