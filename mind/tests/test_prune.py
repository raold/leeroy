"""Retention (storage/prune.py): what each rule takes, what it never takes, the dry run, the CLI
and the files."""
import json
import os
import sqlite3

import pytest

from leeroy_mind.storage import prune, prune_cli

NOW = 1_800_000_000.0
DAY = 86400


def event(store, kind, age_days, payload=None, guid=101):
    return store.conn.execute(
        "INSERT INTO game_event(ts,type,guid,payload) VALUES (?,?,?,?)",
        (NOW - age_days * DAY, kind, guid, json.dumps(payload or {"x": "y" * 20}))).lastrowid


def fact(store, n, age_days, *, kind="pvp_kill", policy="pvp_result_v1", guild=7):
    """A world fact with its event, source, origin and one guild that knows it; its id."""
    ts = NOW - age_days * DAY
    ev = event(store, "pvp_kill" if policy == "pvp_result_v1" else "bg_end", age_days)
    key = f"{n:064x}"
    source = store.conn.execute(
        "INSERT INTO realm_source(source_key,protocol_version,event_type,game_event_id,row_sha256,"
        "server_ms,received_at,guild,human,top_keys_json) VALUES (?,1,'pvp_kill',?,?,?,?,?,1,'[]')",
        (key, ev, key, int(ts * 1000), ts, guild)).lastrowid
    ident = store.conn.execute(
        "INSERT INTO realm_fact(kind,text,source,ref,public,ts) VALUES (?,?,'event',?,1,?)",
        (kind, f"fact {n}", f"event:{ev}", ts)).lastrowid
    store.conn.execute(
        "INSERT INTO realm_fact_origin(fact,producer_key,source,policy,assurance,proof_json,"
        "qualification_json,text_sha256,revision,state) VALUES (?,?,?,?,'observed_event',?,'{}',?,1,'active')",
        (ident, f"event:{key}:{policy}", source, policy, json.dumps({"pad": "p" * 300}, separators=(",", ":")), key))
    store.conn.execute("INSERT INTO realm_knows(fact,guild,since,how) VALUES (?,?,?,'present')",
                       (ident, guild, ts))
    return ident, source, ev


def rows(store, table):
    return store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def by_rule(findings):
    return {f.rule: f.rows for f in findings}


def test_events_past_their_days_go_and_everything_cited_or_unlisted_stays(store):
    with store.transaction():
        old = {k: event(store, k, d + 1) for k, d in prune.KEEP_DAYS.items() if k != "death"}
        young = {k: event(store, k, d - 0.5) for k, d in prune.KEEP_DAYS.items()}
        boss = event(store, "boss", 400)
        loot = event(store, "loot_won", 400)
        cited = event(store, "zone", 90)
        store.conn.execute("INSERT INTO meta(key,value) VALUES ('test:cite',?)",
                           (json.dumps([f"event:{cited}"]),))
        world_death = event(store, "death", 40, {"map": 571})
        raid_death = event(store, "death", 40, {"map": 631})
        odd_death = event(store, "death", 40, {"nothing": 1})
    found = by_rule(prune.plan(store.conn, NOW))
    assert found == {"events:pvp_flag": 1, "events:resurrect": 1, "events:pvp_kill": 1,
                     "events:zone": 1, "events:death": 1, "realm_facts": 0, "review": 0}
    assert rows(store, "game_event") == len(old) + len(young) + 6      # the dry run wrote nothing
    assert by_rule(prune.apply(store.conn, NOW))["events:death"] == 1
    left = {r[0] for r in store.conn.execute("SELECT id FROM game_event")}
    assert left == {*young.values(), boss, loot, cited, raid_death, odd_death}
    assert world_death not in left and not set(old.values()) & left
    assert not any(f.rows for f in prune.plan(store.conn, NOW))        # nothing left to take


def test_a_reference_inside_a_longer_number_or_a_hash_protects_only_its_own_event(store):
    with store.transaction():
        first = event(store, "zone", 90)
        second = event(store, "zone", 90)
        store.conn.execute("INSERT INTO meta(key,value) VALUES ('test:cite',?)",
                           (f'["event:{second}", "revent:{first}", "event:{first}abc"]',))
    prune.apply(store.conn, NOW)
    assert [r[0] for r in store.conn.execute("SELECT id FROM game_event")] == [second]


def test_old_world_facts_go_with_their_provenance_and_newest_fifty_stay(store):
    with store.transaction():
        old = [fact(store, n, 60 + n * 0.001) for n in range(1, 61)]         # 60 old ones
        fresh = fact(store, 100, 5)
        other = fact(store, 101, 90, kind="bg_result", policy="bg_result_v1")
    found = by_rule(prune.plan(store.conn, NOW))
    # guild 7 knows 62 facts: its newest 50 stay (fresh, and 49 of the old pvp ones; the
    # battleground result is older than all of them and is not a pvp fact)
    assert found["realm_facts"] == 11
    store.conn.execute("PRAGMA foreign_keys=ON")
    done = by_rule(prune.apply(store.conn, NOW))
    assert done["realm_facts"] == 11
    assert rows(store, "realm_fact") == 62 - 11 == rows(store, "realm_fact_origin") == rows(store, "realm_knows")
    assert rows(store, "realm_source") == 51
    kept = {r[0] for r in store.conn.execute("SELECT id FROM realm_fact")}
    assert fresh[0] in kept and other[0] in kept
    gone_events = [e for i, _, e in old if i not in kept]
    assert len(gone_events) == 11
    assert not store.conn.execute(
        f"SELECT 1 FROM game_event WHERE id IN ({','.join('?' * 11)})", gone_events).fetchone()
    # the event behind a kept source stays, however old: the source stands on it
    assert rows(store, "game_event") == 51
    assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_fact_anything_refers_to_stays(store):
    with store.transaction():
        facts = [fact(store, n, 60 + n) for n in range(1, 56)]
        oldest = sorted(facts, key=lambda f: f[0])[-3:]   # ids of the three oldest
        posted, remembered, quoted = (f[0] for f in oldest)
        store.conn.execute("INSERT INTO realm_post(ts,author,author_kind,thread,text,fact) "
                           "VALUES (?,101,'member','general','a post',?)", (NOW, posted))
        store.conn.execute("INSERT INTO meta(key,value) VALUES ('test:cite',?)",
                           (json.dumps([f"realm:fact:{remembered}"]),))
        store.conn.execute("INSERT INTO realm_post(ts,author,author_kind,thread,text) "
                           "VALUES (?,101,'member',?,'another')", (NOW, f"fact:{quoted}"))
    prune.apply(store.conn, NOW)
    kept = {r[0] for r in store.conn.execute("SELECT id FROM realm_fact")}
    assert {posted, remembered, quoted} <= kept and len(kept) == 50 + 3


def test_the_realm_journal_and_its_receipts_are_never_touched(store):
    with store.transaction():
        for key in ("realm:action:abandoned:abc", "realm:action:forgotten:abc", "realm:guild_name",
                    "discord:line:5", "nightly:vocab-line:5", "nightly:human-world:reaction:5"):
            store.conn.execute("INSERT INTO meta(key,value) VALUES (?, 'kept')", (key,))
    before = store.conn.execute("SELECT key FROM meta ORDER BY key").fetchall()
    prune.apply(store.conn, NOW + 400 * DAY)
    assert store.conn.execute("SELECT key FROM meta ORDER BY key").fetchall() == before
    assert not any("realm_action" in sql or "realm_direct" in sql
                   for sql in (open(prune.__file__, encoding="utf-8").read(),))


def test_receipts_of_old_lines_and_closed_raids_go(store):
    with store.transaction():
        old = store.conn.execute("INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin) "
                                 "VALUES (?,'guild',101,'Arkon',0,'old','mind')", (NOW - 40 * DAY,)).lastrowid
        new = store.conn.execute("INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin) "
                                 "VALUES (?,'guild',101,'Arkon',0,'new','mind')", (NOW - DAY,)).lastrowid
        for line in (old, new):
            for prefix in ("nightly:lexicon-line:", "nightly:human-world:lands:",
                           "nightly:human-world:reaction:"):
                store.conn.execute("INSERT INTO meta(key,value) VALUES (?, '[]')", (prefix + str(line),))
        store.conn.execute("INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,"
                           "lead_guid,tank_guid,loot_guid) VALUES (1,?,?,'naxx',10,0,0,101,101,101),"
                           "(2,?,NULL,'naxx',10,0,0,101,101,101)", (NOW - 41 * DAY, NOW - 40 * DAY, NOW - 41 * DAY))
        for raid in (1, 2):
            store.conn.execute("INSERT INTO meta(key,value) VALUES (?, '1')", (f"guild:raid:{raid}:bench-memory:102",))
            store.conn.execute("INSERT INTO meta(key,value) VALUES (?, '1')", (f"guild:raid:{raid}:attendance",))
    done = by_rule(prune.apply(store.conn, NOW))
    assert (done["meta:lexicon-line"], done["meta:lands"], done["meta:bench-memory"]) == (1, 1, 1)
    keys = {r[0] for r in store.conn.execute("SELECT key FROM meta WHERE key LIKE 'nightly:%' OR key LIKE 'guild:raid:%'")}
    assert keys == {f"nightly:lexicon-line:{new}", f"nightly:human-world:lands:{new}",
                    f"nightly:human-world:reaction:{old}", f"nightly:human-world:reaction:{new}",
                    "guild:raid:2:bench-memory:102", "guild:raid:1:attendance", "guild:raid:2:attendance"}


def test_old_drift_flags_are_resolved_and_kept(store):
    with store.transaction():
        for age in (20, 3):
            store.conn.execute("INSERT INTO review_queue(guid,kind,text,evidence,reason,created) "
                               "VALUES (101,'drift_line','a line','[]','a reason',?)", (NOW - age * DAY,))
    assert by_rule(prune.plan(store.conn, NOW))["review"] == 1
    prune.apply(store.conn, NOW)
    assert store.conn.execute("SELECT resolved FROM review_queue ORDER BY id").fetchall() == [(NOW,), (None,)] \
        or [tuple(r) for r in store.conn.execute("SELECT resolved FROM review_queue ORDER BY id")] == [(NOW,), (None,)]


def test_a_bound_takes_that_many_rows_a_rule_and_the_next_pass_goes_on(store):
    with store.transaction():
        for _ in range(30):
            event(store, "pvp_flag", 5)
            event(store, "resurrect", 20)
    assert by_rule(prune.apply(store.conn, NOW, limit=12)) == {"events:pvp_flag": 12, "events:resurrect": 12}
    assert rows(store, "game_event") == 36
    prune.apply(store.conn, NOW)
    assert rows(store, "game_event") == 0


def test_the_migration_adds_the_indexes_the_deletes_and_the_type_reads_need(store):
    assert prune.indexed(store.conn)
    plans = [r[3] for sql in (
        "SELECT id FROM game_event WHERE type='death' ORDER BY id DESC LIMIT 1",
        "SELECT payload FROM game_event WHERE type='boss' AND ts>=1 AND ts<2",
        "SELECT max(ts) FROM game_event WHERE session_id=3 AND type<>'channel_chat'")
        for r in store.conn.execute("EXPLAIN QUERY PLAN " + sql)]
    assert all("USING" in p and "INDEX" in p for p in plans), plans
    assert "game_event_type_ts" in plans[1] and "game_event_session" in plans[2]


# -- files ---------------------------------------------------------------------------------------

def aged(path, days, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (NOW - days * DAY, NOW - days * DAY))
    return path


def test_orphan_sidecars_scratch_copies_and_a_stale_lab_copy_go_and_nothing_else(tmp_path):
    snaps, scratch = tmp_path / "snapshots", tmp_path / "scratch"
    keep = [aged(snaps / "mind-start-20261101-000000.db", 9), aged(snaps / "mind-start-20261101-000000.db-wal", 9),
            aged(snaps / "pre-release-20261101-000000-1.db", 30), aged(snaps / "notes.txt", 30),
            aged(scratch / "mind-eval-new.db", 1), aged(scratch / "mind-eval-new.db-wal", 1),
            aged(scratch / "selfies-try" / "a.jpg", 90), aged(scratch / "locks" / "x.lock", 90),
            aged(scratch / "enrich-2026-11-05.json", 90), aged(scratch / "lab" / "runs" / "1" / "control.json", 90),
            aged(scratch / "lab" / ".leeroy-sandbox", 90), aged(scratch / "lab" / "child.log", 90)]
    gone = [aged(snaps / "mind-start-20261001-000000.db-wal", 9), aged(snaps / "mind-start-20261001-000000.db-shm", 9),
            aged(scratch / "mind-eval-old.db", 8), aged(scratch / "mind-eval-old.db-shm", 8),
            aged(scratch / "mind-selfies-try-gone.db-wal", 1),
            aged(scratch / "lab" / "mind-data" / "library.db", 3, 100), aged(scratch / "lab" / "snapshot" / "mind.db", 3)]
    for folder in (scratch / "lab" / "mind-data", scratch / "lab" / "snapshot"):
        os.utime(folder, (NOW - 3 * DAY, NOW - 3 * DAY))
    found = {f.rule: (f.rows, f.bytes) for f in prune.plan_files(tmp_path, NOW)}
    assert found == {"files:snapshot-sidecar": (2, 20), "files:scratch-db": (2, 30), "files:lab-copy": (2, 110)}
    assert all(p.exists() for p in gone)                       # the dry run removed nothing
    prune.prune_files(tmp_path, NOW)
    assert all(p.exists() for p in keep) and not any(p.exists() for p in gone)
    assert prune.plan_files(tmp_path, NOW) == []


def test_a_lab_copy_in_use_and_copies_under_the_cap_stay_and_the_oldest_go_above_it(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    aged(scratch / "lab" / ".leeroy-sandbox", 0)
    live = aged(scratch / "lab" / "mind-data" / "mind.db", 0)
    copies = [aged(scratch / f"mind-try-{n}.db", n, 100) for n in (1, 2, 3)]
    assert prune.plan_files(tmp_path, NOW) == []
    monkeypatch.setattr(prune, "SCRATCH_CAP", 150)
    prune.prune_files(tmp_path, NOW)
    assert [p.exists() for p in copies] == [True, False, False] and live.exists()


def test_a_snapshot_label_pruned_takes_its_sidecars_along(tmp_path, store):
    from leeroy_mind.storage.db import snapshot
    folder = tmp_path / "snaps"
    first = snapshot(store.conn, folder, "start", keep=1, now=NOW)
    for suffix in ("-wal", "-shm"):
        (folder / (first.name + suffix)).write_bytes(b"left by a reader")
    second = snapshot(store.conn, folder, "start", keep=1, now=NOW + 5)
    assert sorted(p.name for p in folder.iterdir()) == [second.name]


# -- the command -----------------------------------------------------------------------------------

@pytest.fixture
def root(tmp_path, store):
    """A data dir whose mind.db holds old rows."""
    import time
    with store.transaction():
        for _ in range(3):
            store.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,?,?,?)",
                               (time.time() - 3 * DAY, "pvp_flag", 101, "{}"))
    from leeroy_mind.storage.db import snapshot
    path = snapshot(store.conn, tmp_path / "data", "x")
    os.replace(path, tmp_path / "data" / "mind.db")
    return tmp_path / "data"


def count(root):
    with sqlite3.connect(root / "mind.db") as conn:
        return conn.execute("SELECT count(*) FROM game_event").fetchone()[0]


def test_the_dry_run_is_the_default_prints_counts_and_writes_nothing(root, capsys):
    assert prune_cli.run(root) == 0
    out = capsys.readouterr().out
    assert "events:pvp_flag" in out and " 3 rows" in out and "dry run, nothing written" in out
    assert count(root) == 3 and not (root / "snapshots").exists()


def test_as_of_counts_a_later_day_and_is_refused_with_go(root, capsys):
    assert prune_cli.run(root, as_of="2001-01-01") == 0
    assert "events:pvp_flag" not in capsys.readouterr().out
    assert prune_cli.run(root, go=True, as_of="2030-01-01") == 1 and count(root) == 3


def test_go_takes_a_snapshot_first_then_removes_and_vacuums(root, capsys):
    assert prune_cli.run(root, go=True, vacuum=True) == 0
    out = capsys.readouterr().out
    assert "snapshot mind-pre-prune-" in out and "written; snapshot taken" in out and "vacuum:" in out
    assert count(root) == 0
    saved = list((root / "snapshots").glob("mind-pre-prune-*.db"))
    assert len(saved) == 1
    with sqlite3.connect(saved[0]) as conn:
        assert conn.execute("SELECT count(*) FROM game_event").fetchone()[0] == 3


def test_go_is_refused_while_the_mind_runs_and_before_the_migration(root, capsys, monkeypatch):
    from leeroy_mind import app
    with app.acquire_instance_lock(root):
        assert prune_cli.run(root, go=True) == 1
    assert "mind service is running" in capsys.readouterr().out and count(root) == 3
    with sqlite3.connect(root / "mind.db") as conn:
        conn.execute("DROP INDEX realm_source_game_event")
    assert prune_cli.run(root, go=True) == 1
    assert "db migrate first" in capsys.readouterr().out and count(root) == 3


# -- the night ---------------------------------------------------------------------------------------

def night(store, clock, data_dir, cfg):
    import asyncio
    from types import SimpleNamespace
    from leeroy_mind.nightly import prune as stage
    done, failed = set(), []

    def commit(name, unit, write):
        with store.transaction():
            write(store)
        done.add((name, unit))
        return True
    ctx = SimpleNamespace(store=store, clock=clock, night="2026-10-28", done=lambda s, u: (s, u) in done,
                          commit=commit, skip=lambda s, u: commit(s, u, lambda _: None),
                          fail=lambda s, u, e: failed.append(str(e)),
                          settings=SimpleNamespace(data_dir=data_dir))
    asyncio.run(stage.make_stage(cfg)(ctx))
    return done, failed


def test_the_night_takes_its_bounded_share_and_the_files(store, clock, tmp_path):
    from pathlib import Path
    from leeroy_mind.nightly import prune as stage
    data_dir = Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent
    now = clock.now()
    with store.transaction():
        for _ in range(25):
            store.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,?,?,?)",
                               (now - 3 * DAY, "pvp_flag", 101, "{}"))
        store.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,?,?,?)",
                           (now - 400 * DAY, "boss", 101, "{}"))
    orphan = data_dir / "snapshots" / "mind-start-20261001-000000.db-wal"
    orphan.parent.mkdir(exist_ok=True)
    orphan.write_bytes(b"x")
    done, failed = night(store, clock, data_dir, stage.PruneCfg(True, 10))
    assert done == {("prune", "rows"), ("prune", "files")} and not failed
    assert rows(store, "game_event") == 16 and not orphan.exists()
    night(store, clock, data_dir, stage.PruneCfg(True, 100))
    assert [r[0] for r in store.conn.execute("SELECT type FROM game_event")] == ["boss"]


def test_the_stage_is_off_unless_asked_and_off_for_a_bad_table(tmp_path):
    from leeroy_mind.nightly import prune as stage
    path = tmp_path / "mind.toml"
    assert stage.load(path) == stage.PruneCfg(False, 20_000)
    for text, want in (("[prune]\nenabled = true\n", stage.PruneCfg(True, 20_000)),
                       ("[prune]\nenabled = true\nrows_per_night = 500\n", stage.PruneCfg(True, 500)),
                       ("[prune]\nenabled = true\nrows_per_night = 0\n", stage.PruneCfg()),
                       ("[prune]\nenabled = 1\n", stage.PruneCfg()),
                       ("[prune]\nenabled = true\nbogus = 1\n", stage.PruneCfg()),
                       ("prune = 3\n", stage.PruneCfg())):
        path.write_text(text, encoding="utf-8")
        assert stage.load(path) == want, text
    added = []
    runner = type("R", (), {"add_stage": lambda self, name, fn, **kw: added.append((name, kw))})()
    stage.attach(runner, tmp_path)
    assert added == []
    path.write_text("[prune]\nenabled = true\n", encoding="utf-8")
    stage.attach(runner, tmp_path)
    assert added == [("prune", {"after": "eval", "model": None})]
