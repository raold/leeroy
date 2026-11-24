"""The verification ledger on synthetic Server.log, Playerbots.log and mind.log files, module
configs and a migrated scratch mind.db. Never the live install's logs, configs or database."""
import dataclasses
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from leeroy_mind.verify import ledger as L
from leeroy_mind.verify import logscan
from leeroy_mind.verify.cli import lines, verify_main

REPO = Path(__file__).resolve().parents[3]
NOW = time.time()
SINCE = NOW - 10 * 86400
SECRET = "PRIVATE_SENTINEL"
USER_LIST = {"group_rez", "loot_handout", "invitations", "you_lead", "twin_essences", "valithria_healers",
             "living_poison", "lich_king", "yogg_brain", "offline_toc", "offline_ulduar",
             "offline_icc_gunship", "offline_icc_valithria"}

TEMPLATE = r"""
[[feature]]
id = "rez"
name = "Group rez"
prs = [405]
since = "{since}"

[[feature.switch]]
source = "native"
key = "Leeroy.GroupRez.Enable"
default = true

[[feature.evidence]]
id = "cast"
kind = "working"
what = "a rez was cast"
log = "server"
token = "group_rez_cast "
pattern = '^group_rez_cast guid=\d+'

[[feature.evidence]]
id = "blocked"
kind = "failed"
what = "a body was left"
log = "server"
token = "group_rez_blocked "
pattern = '^group_rez_blocked reason=(?P<code>\S+)'

[[feature.evidence]]
id = "approach"
kind = "observed"
what = "a rezzer walked in"
log = "server"
token = "group_rez_approach "
pattern = '^group_rez_approach guid=\d+'

[[feature]]
id = "loot"
name = "Loot"
prs = [406]
since = "{since}"

[[feature.evidence]]
id = "charged"
kind = "working"
what = "charged"
db = {{ from = "dkp_ledger", where = "reason = 'item'", time = "ts", unit = "s" }}

[[feature.evidence]]
id = "unsettled"
kind = "failed"
what = "unsettled"
db = {{ from = "meta", where = "key LIKE 'guild:loot:%' AND json_extract(value, '$.state') = 'uncertain'", time = "json_extract(value, '$.received')", unit = "s", code = "json_extract(value, '$.error')" }}

[[feature]]
id = "invites"
name = "Invitations"
prs = [388]
since = "{since}"

[[feature.switch]]
source = "mind"
key = "talk.invites"

[[feature.switch]]
source = "native"
key = "Leeroy.Invites.Enable"
default = false

[[feature.evidence]]
id = "done"
kind = "working"
what = "a body moved"
log = "mind"
token = "invite_body"
pattern = '^invite_body .*\bkind=(?P<code>[a-z_]+) outcome=done\b'

[[feature.evidence]]
id = "ended"
kind = "failed"
what = "a raid ended badly"
log = "mind"
token = "offline_raid_ended"
pattern = '^offline_raid_ended raid_id=\d+ outcome=(?P<code>too_few|leader_lost) '
"""


def stamp(at):
    return datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S")


def native(at, text):
    return f"{stamp(at)} {text}\n"


def mind(at, event, **fields):
    return json.dumps({"ts": datetime.fromtimestamp(at).astimezone().isoformat(timespec="milliseconds"),
                       "level": "INFO", "event": event, **fields}) + "\n"


@pytest.fixture
def install(store, tmp_path):
    """The store's mind.db is in tmp_path: that is the data dir; native files beside it."""
    (tmp_path / "logs").mkdir()
    folder = tmp_path / "native"
    (folder / "configs" / "modules").mkdir(parents=True)
    return tmp_path, folder


def features(tmp_path, since=SINCE):
    path = tmp_path / "test-features.toml"
    path.write_text(TEMPLATE.format(since=datetime.fromtimestamp(since).strftime("%Y-%m-%dT%H:%M:%S")),
                    encoding="utf-8")
    return L.load_features(path)


def append(path, text):
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(text)


def conf(folder, name, text):
    (folder / "configs" / "modules" / name).write_text(text, encoding="utf-8")


def feature(result, ident):
    return next(f for f in result["features"] if f["id"] == ident)


def evidence(item, ident):
    return next(e for e in item["evidence"] if e["id"] == ident)


def run(install, fs, **kw):
    data, folder = install
    return L.ledger(data, folder, features=fs, now=NOW, **kw)


# -- the shipped file -----------------------------------------------------------------------------

def test_the_shipped_ledger_loads_and_covers_the_unverified_features():
    fs = L.load_features()
    assert len(fs) >= 20
    assert USER_LIST <= {f.id for f in fs}
    for f in fs:
        assert any(e.kind != "observed" for e in f.evidence) or f.note, f.id


def test_every_log_signature_matches_its_own_example():
    for f in L.load_features():
        for e in f.evidence:
            if not e.log:
                continue
            assert e.example, (f.id, e.id)
            raw = e.example.encode("utf-8")
            assert e.token.encode("ascii") in raw, (f.id, e.id)
            _, text = logscan._read_line("mind" if e.log == "mind" else "native", raw, None)
            found = e.pattern.search(text)
            assert found, (f.id, e.id)
            if "code" in e.pattern.groupindex:
                assert logscan.CODE.fullmatch(found["code"]), (f.id, e.id)


def test_a_real_scan_counts_every_shipped_example(install):
    data, folder = install
    fs = tuple(dataclasses.replace(f, since=None) for f in L.load_features())
    target = {"server": folder / "Server.log", "playerbots": folder / "Playerbots.log",
              "mind": data / "logs" / "mind.log"}
    for f in fs:
        for e in f.evidence:
            if e.log:
                append(target[e.log], e.example + "\n")
    result = run(install, fs)
    for f in fs:
        item = feature(result, f.id)
        for e in f.evidence:
            if e.log:
                assert evidence(item, e.id)["count"] >= 1, (f.id, e.id)
    assert all(c["complete"] for c in result["coverage"].values())


def test_every_db_signature_runs_on_the_current_schema(install):
    result = run(install, L.load_features())
    assert result["database"] == "ok"
    for item in result["features"]:
        for e in item["evidence"]:
            if e["source"] == "mind.db":
                assert e["available"], (item["id"], e["id"], e["why"])
                assert e["count"] == 0


def test_native_and_playerbots_defaults_are_the_compiled_ones():
    sources = {"native": sorted((REPO / "modules" / "mod-leeroy" / "src").rglob("*.cpp")),
               "playerbots": sorted((REPO / "patches" / "mod-playerbots").glob("*.patch"))}
    if not all(sources.values()):
        pytest.skip("not a full checkout: modules/ and patches/ are absent")
    text = {k: "\n".join(p.read_text("utf-8", errors="replace") for p in v) for k, v in sources.items()}
    checked = 0
    for f in L.load_features():
        for s in f.switches:
            if s.source == "mind":
                continue
            found = set(re.findall(r'GetOption<bool>\("' + re.escape(s.key) + r'",\s*(true|false)\)',
                                   text[s.source]))
            assert found == {"true" if s.default else "false"}, (f.id, s.key, found)
            checked += 1
    assert checked >= 10


def test_mind_switches_take_the_switchboard_defaults():
    fs = {f.id: f for f in L.load_features()}
    invites = {s.key: s for s in fs["invitations"].switches}
    assert invites["talk.invites"].default is True
    assert {s.key: s for s in fs["day_plans"].switches}["life.day_plans"].default is False


@pytest.mark.parametrize("bad, words", [
    ('source = "mind"\nkey = "talk.nope"', "boolean row"),
    ('source = "native"\nkey = "Leeroy.NotAKey"\ndefault = true', "native_switch_keys"),
    ('source = "native"\nkey = "Leeroy.GroupRez.Enable"', "compiled default"),
])
def test_unknown_switches_are_refused(tmp_path, bad, words):
    path = tmp_path / "bad.toml"
    path.write_text('[[feature]]\nid = "x"\nname = "X"\nprs = [1]\n[[feature.switch]]\n' + bad +
                    '\n[[feature.evidence]]\nid = "e"\nkind = "working"\nwhat = "w"\nlog = "server"\n'
                    'token = "t"\npattern = "t"\n', encoding="utf-8")
    with pytest.raises(ValueError, match=words):
        L.load_features(path)


@pytest.mark.parametrize("bad", [
    'log = "server"\ntoken = "t"\npattern = "(?P<text>t)"',
    'log = "server"\ntoken = "t"\npattern = "("',
    'log = "elsewhere"\ntoken = "t"\npattern = "t"',
    'db = { from = "meta", time = "1", unit = "s; DROP TABLE meta" }',
    'db = { from = "meta", time = "1", unit = "h" }',
])
def test_malformed_evidence_is_refused(tmp_path, bad):
    path = tmp_path / "bad.toml"
    path.write_text('[[feature]]\nid = "x"\nname = "X"\nprs = [1]\n[[feature.evidence]]\nid = "e"\n'
                    'kind = "working"\nwhat = "w"\n' + bad + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        L.load_features(path)


# -- status ---------------------------------------------------------------------------------------

def test_unverified_then_working_then_failed_then_working_again(install, tmp_path):
    data, folder = install
    fs = features(tmp_path)
    assert feature(run(install, fs), "rez")["status"] == "unverified"
    log = folder / "Server.log"
    append(log, native(NOW - 500, "group_rez_cast guid=1 body=2 spell=7328"))
    item = feature(run(install, fs), "rez")
    assert item["status"] == "seen_working"
    assert evidence(item, "cast")["count"] == 1
    assert int(item["working"]["last"]) == int(NOW - 500)
    append(log, native(NOW - 400, "group_rez_blocked reason=all_oom dead=1 casters=1"))
    item = feature(run(install, fs), "rez")
    assert item["status"] == "failed"
    assert evidence(item, "blocked")["codes"] == [{"code": "all_oom", "count": 1}]
    append(log, native(NOW - 300, "group_rez_cast guid=1 body=3 spell=7328"))
    item = feature(run(install, fs), "rez")
    assert item["status"] == "seen_working"
    assert (evidence(item, "cast")["count"], evidence(item, "blocked")["count"]) == (2, 1)


def test_evidence_before_since_is_ignored(install, tmp_path):
    data, folder = install
    append(folder / "Server.log", native(SINCE - 60, "group_rez_cast guid=1 body=2 spell=7328"))
    append(data / "logs" / "mind.log", mind(SINCE - 60, "invite_body", kind="go_to", outcome="done"))
    result = run(install, features(tmp_path))
    assert feature(result, "rez")["status"] == "unverified"
    assert evidence(feature(result, "invites"), "done")["count"] == 0


def test_observed_evidence_is_shown_but_never_decides(install, tmp_path):
    data, folder = install
    append(folder / "Server.log", native(NOW - 10, "group_rez_approach guid=1 body=2 yards=30 path=1"))
    item = feature(run(install, features(tmp_path)), "rez")
    assert item["status"] == "unverified"
    assert item["observed"]["signatures"] == 1 and evidence(item, "approach")["count"] == 1


def pull(conn, ident, boss, outcome, start, end, *, map_id=631, kind="his"):
    conn.execute("INSERT INTO tg_pull(id,context,map,instance_id,boss_id,difficulty,size,outcome,start_ms,end_ms,"
                 "members,assisted,boss_entry,upd_mean_ms,kind,complete,scoreable,received) "
                 "VALUES (?,?,?,1,?,0,10,?,?,?,10,0,0,0,?,1,1,?)",
                 (ident, f"{map_id}:{boss}:0", map_id, boss, outcome, int(start * 1000), int(end * 1000), kind, end))


def test_a_pull_outcome_counts_when_the_fight_ended(install, store):
    """A mechanic handled early in a pull that then wiped: the wipe is the newer evidence."""
    data, folder = install
    pull(store.conn, "lk-1", 12, "wiped", NOW - 600, NOW - 100)
    store.conn.commit()
    append(folder / "Playerbots.log", native(NOW - 500, "lk_mechanic instance=1 bot=2 room=1 code=defile_drop ok=true entry=0"))
    item = feature(run(install, L.load_features()), "lich_king")
    assert int(evidence(item, "wiped")["last"]) == int(NOW - 100)
    assert item["working"]["last"] < item["failed"]["last"]


def test_deaths_by_a_spell_never_decide_and_are_timed_from_the_pull_start(install, store):
    """tg_death.at_ms is time into the pull (TrainingTap's Elapsed), not a clock time."""
    pull(store.conn, "tw-1", 3, "killed", NOW - 900, NOW - 800, map_id=649)
    store.conn.execute("INSERT INTO tg_death(pull,guid,at_ms,spell,source,x,y,hits) VALUES ('tw-1',101,45000,66046,0,0,0,'[]')")
    store.conn.commit()
    item = feature(run(install, L.load_features()), "twin_essences")
    deaths = evidence(item, "vortex_deaths")
    assert deaths["kind"] == "observed" and deaths["count"] == 1 and int(deaths["last"]) == int(NOW - 855)
    assert item["failed"]["last"] is None


@pytest.mark.parametrize("outcome, status", [("boss_wipe_cap", "seen_working"), ("too_few", "failed"),
                                             ("terminal_lost", "failed"), ("cleanup_failed:leader_lost", "failed"),
                                             ("setup:starting:timeout", "unverified")])
def test_offline_reliability_reads_ordinary_ends_and_explicit_failures(install, store, outcome, status):
    raid_row(store.conn, 1, started=NOW - 900, ended=NOW - 100, outcome=outcome, kills=2)
    store.conn.commit()
    fs = tuple(dataclasses.replace(f, switches=()) for f in L.load_features() if f.id == "offline_reliability")
    assert feature(run(install, fs), "offline_reliability")["status"] == status


def test_day_plans_need_the_realm_on_both_sides():
    keys = {s.key: s for s in next(f for f in L.load_features() if f.id == "day_plans").switches}
    assert {"realm.enabled", "Leeroy.Realm.Enable", "life.day_plans", "Leeroy.DayPlans.Enable", "Leeroy.Enable"} <= set(keys)
    assert keys["Leeroy.Realm.Enable"].default is False


def test_the_module_switch_gates_every_feature_mod_leeroy_carries(install):
    data, folder = install
    fs = L.load_features()
    for f in fs:
        keys = [s.key for s in f.switches if s.source == "native"]
        if keys or any(e.log == "server" for e in f.evidence) or f.id.startswith(("offline_", "loot")):
            assert "Leeroy.Enable" in keys, f.id
    append(folder / "Server.log", native(NOW - 10, "group_rez_cast guid=1 body=2 spell=7328"))
    conf(folder, "mod_leeroy.conf", "Leeroy.Enable = 0\n")
    item = feature(run(install, fs), "group_rez")
    assert item["status"] == "switched_off" and evidence(item, "cast")["count"] == 1


def test_keep_cast_gates_the_jobs_that_need_it(install, store):
    """StartRaidJob (offline raids, heroics), dummy trials and death-loop rescue refuse without it."""
    data, folder = install
    fs = L.load_features()
    gated = {f.id for f in fs if any(s.key == "Leeroy.KeepCast" for s in f.switches)}
    assert {f.id for f in fs if f.id.startswith("offline_")} | {"member_heroics", "dummy_lab", "cast_death_loop"} == gated
    pull(store.conn, "toc", 1, "killed", NOW - 900, NOW - 800, map_id=649, kind="offline")
    store.conn.commit()
    conf(folder, "mod_leeroy.conf", "Leeroy.KeepCast = 0\n")
    assert feature(run(install, fs), "offline_toc")["status"] == "switched_off"
    conf(folder, "mod_leeroy.conf", "Leeroy.Enable = 1\n")
    assert feature(run(install, fs), "offline_toc")["status"] == "seen_working"


def test_a_later_aura_refusal_outranks_an_earlier_reapply(install):
    data, folder = install
    fs = tuple(f for f in L.load_features() if f.id == "owner_auras")
    append(folder / "Server.log", native(NOW - 50, "owner_aura_reapplied spell=6560 why=death"))
    assert feature(run(install, fs), "owner_auras")["status"] == "seen_working"
    append(folder / "Server.log", native(NOW - 10, "owner_aura_refused spell=6560 why=dispelled"))
    item = feature(run(install, fs), "owner_auras")
    assert item["status"] == "failed" and evidence(item, "refused")["codes"] == [{"code": "dispelled", "count": 1}]


def test_a_spent_deadline_never_costs_a_switch_state(install, store):
    """Switches are read first and on their own short deadline: switched off still outranks evidence."""
    data, folder = install
    fs = tuple(f for f in L.load_features() if f.id == "dummy_lab")
    store.conn.execute("INSERT INTO meta(key,value) VALUES ('tg:lab:summary:2026-11-22',?)",
                       (json.dumps({"reason": "dummy_rounds_complete", "ts": NOW - 900}),))
    store.conn.commit()
    (data / "mind.toml").write_text("[training]\nenabled = false\n", encoding="utf-8")
    item = feature(run(install, fs, deadline=time.monotonic() - 1), "dummy_lab")
    assert item["status"] == "switched_off"
    assert {s["key"]: s["state"] for s in item["switches"]}["training.enabled"] == "off"


def raid_row(conn, rid, *, started, ended, outcome, key="icc", offline=1, kills=1):
    conn.execute("INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,"
                 "loot_guid,kills,wipes,outcome) VALUES (?,?,?,?,10,0,?,101,101,102,?,0,?)",
                 (rid, started, ended, key, offline, kills, outcome))


def test_a_run_that_stalled_after_the_gunship_counts_when_it_ended(install, store):
    """It started before an earlier run reached Saurfang, but its failure is known only at its end."""
    data, folder = install
    conn = store.conn
    raid_row(conn, 1, started=NOW - 5000, ended=NOW - 3000, outcome="cleared_reachable")
    pull(conn, "saurfang", 3, "killed", NOW - 4000, NOW - 3900, kind="offline")
    conn.execute("UPDATE tg_pull SET raid_id=1 WHERE id='saurfang'")
    raid_row(conn, 2, started=NOW - 4500, ended=NOW - 100, outcome="stalled")
    pull(conn, "deathwhisper", 1, "killed", NOW - 4400, NOW - 4300, kind="offline")
    conn.execute("UPDATE tg_pull SET raid_id=2 WHERE id='deathwhisper'")
    conn.commit()
    append(folder / "Playerbots.log", native(NOW - 4200, "dc_icc_gunship_pass instance=9 earned=0"))
    fs = tuple(dataclasses.replace(f, switches=()) for f in L.load_features() if f.id == "offline_icc_gunship")
    item = feature(run(install, fs), "offline_icc_gunship")
    assert item["status"] == "failed"
    assert evidence(item, "stuck")["codes"] == [{"code": "stalled", "count": 1}]
    assert evidence(item, "passed")["kind"] == "observed" and evidence(item, "passed")["count"] == 1


@pytest.mark.parametrize("routing, elsewhere", [("4,DungeonClear", True), ('"4,Console Playerbots"', False), (None, False)])
def test_dungeon_clear_routed_to_its_own_file_is_partial_coverage(install, routing, elsewhere):
    data, folder = install
    append(folder / "Playerbots.log", native(NOW - 10, "dc_icc_gunship_pass instance=9 earned=0"))
    if routing is not None:
        conf(folder, "mod_dungeon_clear.conf", f"Appender.DungeonClear=2,4,0,DungeonClear.log\n"
                                               f"Logger.playerbots.dungeonclear={routing}\n")
    coverage = run(install, L.load_features())["coverage"]["playerbots"]
    assert ("dungeonclear_elsewhere" in coverage["issues"]) is elsewhere
    assert coverage["complete"] is (not elsewhere)
    assert L.dungeonclear_elsewhere(folder / "absent") is None


def test_loot_failures_are_timed_when_the_outcome_became_known(install, store):
    """Two drops before either award: the later failure is newer than the earlier charge."""
    data, folder = install
    fs = tuple(f for f in L.load_features() if f.id == "loot_handout")
    store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id) VALUES (?,101,-40,'item',1,50274)",
                       (NOW - 100,))
    store.conn.execute("INSERT INTO meta(key,value) VALUES ('guild:loot:1:b',?)",
                       (json.dumps({"state": "uncertain", "received": NOW - 300}),))
    store.conn.commit()
    item = feature(run(install, fs), "loot_handout")
    assert item["status"] == "seen_working" and evidence(item, "unsettled")["count"] == 1
    append(data / "logs" / "mind.log", json.dumps({"ts": datetime.fromtimestamp(NOW - 50).astimezone().isoformat(),
        "level": "WARNING", "event": "log", "msg": "guild_loot_give_unknown raid=1 item=50275 to=101 "
        "command=guild-loot-1-abc attempt=1 code=receipt_unproven"}) + "\n")
    item = feature(run(install, fs), "loot_handout")
    assert item["status"] == "failed"
    assert evidence(item, "give_unknown")["codes"] == [{"code": "receipt_unproven", "count": 1}]


def test_a_lab_blocked_now_is_a_failure(install, store):
    fs = tuple(dataclasses.replace(f, switches=()) for f in L.load_features() if f.id == "dummy_lab")
    store.conn.execute("INSERT INTO meta(key,value) VALUES ('tg:lab:summary:2026-11-22',?)",
                       (json.dumps({"reason": "dummy_rounds_complete", "ts": NOW - 900}),))
    store.conn.commit()
    assert feature(run(install, fs), "dummy_lab")["status"] == "seen_working"
    store.conn.execute("INSERT INTO meta(key,value) VALUES ('tg:lab:owned',?)",
                       (json.dumps({"phase": "blocked", "halt": "native_cleanup_unproved", "halted_at": NOW - 600}),))
    store.conn.commit()
    item = feature(run(install, fs), "dummy_lab")
    assert item["status"] == "failed"
    assert evidence(item, "blocked")["codes"] == [{"code": "native_cleanup_unproved", "count": 1}]


def test_day_plans_need_a_witnessed_physical_block(install, store):
    conn = store.conn
    conn.execute("INSERT INTO game_event(id,ts,type,payload) VALUES (1,?,'job','{}')", (NOW - 300,))
    for n, activity, reason, evidence_id in ((1, "offline", "no_game_action", None), (2, "fishing", "ended", None),
                                             (3, "fishing", "no_game_action", 1)):
        conn.execute("INSERT INTO day_plan(id,night,member,starts,ends,activity,status,reason,evidence) "
                     "VALUES (?,'2026-11-24',101,?,?,?,'done',?,?)", (n, NOW - 400 + n, NOW + 3600, activity, reason, evidence_id))
    conn.commit()
    fs = tuple(dataclasses.replace(f, switches=()) for f in L.load_features() if f.id == "day_plans")
    assert evidence(feature(run(install, fs), "day_plans"), "done")["count"] == 0
    conn.execute("INSERT INTO day_plan(id,night,member,starts,ends,activity,status,reason,evidence) "
                 "VALUES (4,'2026-11-24',102,?,?,'inn','done','ended',1)", (NOW - 900, NOW - 200))
    conn.commit()
    done = evidence(feature(run(install, fs), "day_plans"), "done")
    assert done["count"] == 1 and int(done["last"]) == int(NOW - 300)  # the witness's time, never a future end
    assert done["codes"] == [{"code": "inn", "count": 1}]


def test_switches_from_the_module_files_and_mind_toml(install, tmp_path):
    data, folder = install
    fs = features(tmp_path)
    append(folder / "Server.log", native(NOW - 10, "group_rez_cast guid=1 body=2 spell=7328"))
    append(data / "logs" / "mind.log", mind(NOW - 10, "invite_body", kind="go_to", outcome="done"))
    # no module files: unknown, never read as off
    result = run(install, fs)
    assert feature(result, "rez")["status"] == "seen_working"
    assert feature(result, "rez")["switches"][0]["state"] == "unknown"
    assert feature(result, "invites")["status"] == "seen_working"
    # unset keys take their compiled defaults
    conf(folder, "mod_leeroy.conf", "Leeroy.Enable = 1\n")
    result = run(install, fs)
    assert feature(result, "rez")["status"] == "seen_working"
    invites = feature(result, "invites")
    assert invites["status"] == "switched_off"
    assert {s["key"]: (s["state"], s["configured"]) for s in invites["switches"]} == {
        "talk.invites": ("on", "on"), "Leeroy.Invites.Enable": ("off", "unset")}
    # configured values win, first assignment as the core reads them
    conf(folder, "mod_leeroy.conf", 'Leeroy.GroupRez.Enable = 0\nLeeroy.Invites.Enable = "1"\nLeeroy.Invites.Enable = 0\n')
    result = run(install, fs)
    assert feature(result, "rez")["status"] == "switched_off"
    assert feature(result, "invites")["status"] == "seen_working"
    # a mind table its loader refuses is off; a plain false is off
    (data / "mind.toml").write_text("[talk]\ninvites = false\n", encoding="utf-8")
    assert feature(run(install, fs), "invites")["status"] == "switched_off"
    (data / "mind.toml").write_text('[talk]\ninvites = "yes"\n', encoding="utf-8")
    item = feature(run(install, fs), "invites")
    assert item["status"] == "switched_off"
    assert item["switches"][0]["configured"] == "invalid"


def test_db_evidence_times_codes_and_since(install, store, tmp_path):
    fs = features(tmp_path)
    conn = store.conn
    conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id) VALUES (?,101,-40,'item',1,50274)",
                 (SINCE - 100,))
    conn.commit()
    assert feature(run(install, fs), "loot")["status"] == "unverified"
    conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id) VALUES (?,101,-40,'item',1,50274)",
                 (NOW - 100,))
    conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id) VALUES (?,101,10,'kill',1,NULL)",
                 (NOW - 90,))
    conn.commit()
    item = feature(run(install, fs), "loot")
    assert item["status"] == "seen_working" and evidence(item, "charged")["count"] == 1
    for key, error, at in (("guild:loot:1:a", "receipt_unproven", NOW - 50), ("guild:loot:1:b", SECRET + " said", NOW - 40)):
        conn.execute("INSERT INTO meta(key,value) VALUES (?,?)",
                     (key, json.dumps({"state": "uncertain", "error": error, "received": at, "name": SECRET})))
    conn.commit()
    result = run(install, fs)
    item = feature(result, "loot")
    assert item["status"] == "failed"
    assert {c["code"]: c["count"] for c in evidence(item, "unsettled")["codes"]} == {"receipt_unproven": 1, "other": 1}
    assert SECRET not in json.dumps(result)


def test_missing_tables_and_a_missing_database_are_unavailable_not_absent(tmp_path):
    fs = features(tmp_path)
    folder = tmp_path / "native"
    result = L.ledger(tmp_path, folder, features=fs, now=NOW)
    assert result["database"] == "missing"
    assert evidence(feature(result, "loot"), "charged")["why"] == "missing"
    conn = sqlite3.connect(tmp_path / "mind.db")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    item = feature(L.ledger(tmp_path, folder, features=fs, now=NOW), "loot")
    assert evidence(item, "charged")["why"] == "not_in_this_database"
    assert evidence(item, "unsettled")["available"] is True
    assert item["status"] == "unverified"


def test_nothing_but_ids_counts_times_and_short_codes_leaves(install, tmp_path, capsys):
    data, folder = install
    fs = features(tmp_path)
    append(folder / "Server.log", native(NOW - 30, f"group_rez_cast guid=1 body=2 spell=7328 {SECRET}"))
    append(folder / "Server.log", native(NOW - 20, f"group_rez_blocked reason=has/slash {SECRET}"))
    append(data / "logs" / "mind.log", mind(NOW - 10, "invite_body", text=SECRET, kind="go_to", outcome="done"))
    append(data / "logs" / "mind.log", json.dumps({"ts": datetime.fromtimestamp(NOW - 5).astimezone().isoformat(),
        "level": "INFO", "event": "log", "msg": f"offline_raid_ended raid_id=3 outcome=too_few {SECRET}"}) + "\n")
    result = run(install, fs)
    rez = feature(result, "rez")
    assert evidence(rez, "blocked")["count"] == 1 and evidence(rez, "blocked")["codes"] == []
    assert evidence(feature(result, "invites"), "ended")["codes"] == [{"code": "too_few", "count": 1}]
    assert SECRET not in json.dumps(result)
    assert not any(SECRET in line for line in lines(result))


# -- the scan -------------------------------------------------------------------------------------

def scan_one(folder, **kw):
    sig = logscan.Signature("x.cast", "server", b"group_rez_cast ", re.compile(r"^group_rez_cast guid=\d+"))
    tallies, coverage = logscan.scan({"server": folder}, [sig], deadline=kw.pop("deadline", time.monotonic() + 30), **kw)
    return tallies["x.cast"], coverage["server"]


def test_newest_files_first_within_the_budget_and_no_partial_line(tmp_path):
    old, new = tmp_path / "Server.log.2026-11-20 12-00-00", tmp_path / "Server.log"
    first, second = native(NOW - 300, "group_rez_cast guid=1"), native(NOW - 200, "group_rez_cast guid=2")
    old.write_bytes((first + second).encode())
    new.write_bytes(native(NOW - 100, "group_rez_cast guid=3").encode())
    os.utime(old, (NOW - 200, NOW - 200))
    budget = new.stat().st_size + len(second) + 5
    tally, coverage = scan_one(tmp_path, budgets={"server": budget})
    assert tally.count == 2 and int(tally.first) == int(NOW - 200)
    assert coverage["complete"] is False and coverage["issues"]["truncated_file"] == 1
    assert int(coverage["from"]) == int(NOW - 200)
    tally, coverage = scan_one(tmp_path)
    assert tally.count == 3 and coverage["complete"] is True and coverage["files"] == 2


def test_nul_padding_crlf_and_oversized_lines(tmp_path):
    with (tmp_path / "Server.log").open("wb") as f:
        f.write(b"\0" * (9 * 1024 * 1024))
        f.write(native(NOW - 50, "group_rez_cast guid=1").replace("\n", "\r\n").encode())
        f.write(native(NOW - 40, "group_rez_cast guid=2 " + "x" * (logscan.MAX_LINE + 10)).encode())
        f.write(native(NOW - 30, "group_rez_cast guid=3").encode())
    tally, coverage = scan_one(tmp_path)
    assert tally.count == 2
    assert coverage["issues"] == {"oversized_line": 1}


@pytest.mark.parametrize("chunk", [64, 4 * 1024 * 1024])
def test_a_capped_scan_keeps_the_newest_records(tmp_path, monkeypatch, chunk):
    """Read from the end: a cap drops the oldest lines, never the completion that came last."""
    monkeypatch.setattr(logscan, "MAX_LINES", 3)
    monkeypatch.setattr(logscan, "CHUNK", chunk)
    text = "".join(native(NOW - 100 + n, f"group_rez_cast guid={n}") for n in range(6))
    (tmp_path / "Server.log").write_bytes(text.encode() + b"2026-11-24 10:00:00 group_rez_cast guid=99 still being writ")
    tally, coverage = scan_one(tmp_path)
    assert tally.count == 3 and int(tally.last) == int(NOW - 95) and int(tally.first) == int(NOW - 97)
    assert coverage["issues"] == {"match_limit": 1} and coverage["complete"] is False


def test_lines_across_small_chunks_are_read_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(logscan, "CHUNK", 7)
    text = "".join(native(NOW - 100 + n, f"group_rez_cast guid={n}") for n in range(5))
    (tmp_path / "Server.log").write_bytes(b"\0" * 50 + text.encode())
    tally, coverage = scan_one(tmp_path)
    assert tally.count == 5 and coverage["complete"] is True and coverage["issues"] == {}
    assert int(coverage["from"]) == int(NOW - 100)


def test_a_spent_deadline_reads_nothing_and_says_so(tmp_path):
    (tmp_path / "Server.log").write_bytes(native(NOW, "group_rez_cast guid=1").encode())
    tally, coverage = scan_one(tmp_path, deadline=time.monotonic() - 1)
    assert tally.count == 0 and coverage["complete"] is False and "deadline" in coverage["issues"]


def test_a_missing_folder_is_reported(tmp_path):
    tally, coverage = scan_one(tmp_path / "absent")
    assert tally.count == 0 and coverage["issues"] == {"no_directory": 1} and coverage["complete"] is False


def test_mind_records_kept_whole_and_structured_read_alike(tmp_path):
    (tmp_path / "logs").mkdir()
    path = tmp_path / "logs" / "mind.log"
    append(path, json.dumps({"ts": datetime.fromtimestamp(NOW - 9).astimezone().isoformat(), "level": "INFO",
                             "event": "log", "msg": "offline_raid_ended raid_id=3 outcome=leader_lost cause=-"}) + "\n")
    append(path, mind(NOW - 8, "offline_raid_ended", raid_id=4, outcome="too_few", cause="-"))
    append(path, "not json offline_raid_ended\n")
    sig = logscan.Signature("x.ended", "mind", b"offline_raid_ended",
                            re.compile(r"^offline_raid_ended raid_id=\d+ outcome=(?P<code>\w+) "))
    tallies, coverage = logscan.scan({"mind": tmp_path / "logs"}, [sig], deadline=time.monotonic() + 30)
    assert tallies["x.ended"].count == 2
    assert dict(tallies["x.ended"].codes) == {"leader_lost": 1, "too_few": 1}
    assert coverage["mind"]["issues"] == {"invalid_record": 1}


# -- the command ----------------------------------------------------------------------------------

def test_verify_status_prints_text_and_json(install, capsys):
    data, folder = install
    append(folder / "Server.log", native(NOW - 10, "group_rez_cast guid=1 body=2 spell=7328"))
    args = ["status", "--data-dir", str(data), "--native-dir", str(folder)]
    assert verify_main(args + ["--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert feature(result, "group_rez")["status"] == "seen_working"
    assert set(result["counts"]) == set(L.STATUSES)
    assert verify_main(args) == 0
    out = capsys.readouterr().out
    assert re.search(r"^working +group_rez - ", out, re.M)
    assert "verification ledger: " in out and out.isascii()
