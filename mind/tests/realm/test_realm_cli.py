import json
import sqlite3
from contextlib import closing

import pytest

from conftest import make_persona
from leeroy_mind.personas.io import save_persona
from leeroy_mind.realm import cli
from leeroy_mind.realm.armory import RealmArmoryUnavailable
from leeroy_mind.realm.store import record_receipt


def test_status_is_read_only_and_reports_actual_realm_rows(tmp_path, store, monkeypatch, capsys):
    from leeroy_mind.storage import db
    from leeroy_mind import app
    with store.conn:
        store.conn.execute("INSERT INTO realm_step(name,sha256,approved) VALUES ('review','abc',1)")
        store.conn.execute("INSERT INTO realm_canon(achievement,guid,reason,ts,state) VALUES (1,101,'deferred',2,'blocked')")
    record_receipt(store.conn, command_id="moving", kind="realm_move", stage="job",
                   ok=False, reason="offline", ts=3)
    # SQLite may create WAL/shared-memory sidecars even for a closed WAL-header
    # snapshot opened mode=ro. The main database's bytes and mtime stay unchanged.
    copied = tmp_path / "closed-copy"
    copied.mkdir()
    path = copied / "mind.db"
    copy_conn = sqlite3.connect(path)
    try:
        store.conn.backup(copy_conn)
    finally:
        copy_conn.close()
    before = path.read_bytes(), path.stat().st_mtime_ns
    def forbidden(*args, **kwargs):
        raise AssertionError("read-only CLI tried application/database initialization")
    monkeypatch.setattr(db, "open_db", forbidden)
    monkeypatch.setattr(app, "MindApp", forbidden)
    assert cli.realm_main(["status", "--data-dir", str(copied)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"]["realm_step"] == 1
    assert result["approvals"][0]["name"] == "review"
    assert result["failures"][0]["reason"] == "offline"
    assert result["grant_deferrals"][0]["state"] == "blocked"
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_reader_uses_ro_uri_and_query_only(tmp_path, store):
    with closing(cli._open(tmp_path)) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO realm_step(name,sha256,approved) VALUES ('x','y',1)")
        # Check the URI's mode=ro independently of the connection-level guard.
        # This changes only the disposable reader's pragma, never the DB mode.
        conn.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO realm_step(name,sha256,approved) VALUES ('x','y',1)")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_status_missing_db_does_not_create_it(tmp_path, capsys):
    assert cli.realm_main(["--data-dir", str(tmp_path), "status"]) == 1
    assert not (tmp_path / "mind.db").exists()
    assert "command unavailable" in capsys.readouterr().out


def test_status_old_schema_is_not_migrated(tmp_path, capsys):
    path = tmp_path / "mind.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_data(id INTEGER)")
    before = path.read_bytes()
    assert cli.realm_main(["status", "--data-dir", str(tmp_path)]) == 1
    assert path.read_bytes() == before
    assert "not migrated" in capsys.readouterr().out


def test_titles_reads_cache_aliases_and_actual_guild_rank(tmp_path, store, monkeypatch, capsys):
    for guid, title in [(101, "Raid Lead / DKP"), (102, "Baker"), (103, "dkperson")]:
        save_persona(store, make_persona(guid, f"Member{guid}", officer_title=title),
                     source="seed", ts=1, race="Human", class_name="Warrior")
    called = []
    class Armory:
        def guild_ranks(self, guild):
            called.append(guild)
            return [0, 2, 4]
    monkeypatch.setattr(cli, "armory_from_config", lambda path: Armory())
    assert cli.realm_main(["titles", "--data-dir", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["titles"]["raid lead"] == [[101, "Member101"]]
    assert result["titles"]["dkp"] == [[101, "Member101"]]
    assert result["titles"]["chef"] == [[102, "Member102"]]
    assert len(result["titles"]) == 10
    assert result["trial_rank_exists"] is True and called == [21]


@pytest.mark.parametrize("ranks,exists", [([], False), ([0, 1, 2], False), ([4], True)])
def test_proven_rank_absence_differs_from_unavailable(tmp_path, store, monkeypatch, capsys, ranks, exists):
    class Armory:
        def guild_ranks(self, guild):
            return ranks
    monkeypatch.setattr(cli, "armory_from_config", lambda path: Armory())
    assert cli.realm_main(["titles", "--data-dir", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["armory"] == "available" and result["trial_rank_exists"] is exists


@pytest.mark.parametrize("absent", [False, True])
def test_titles_unavailable_armory_is_partial_and_redacted(tmp_path, store, monkeypatch, capsys, absent):
    class Armory:
        def guild_ranks(self, guild):
            raise RealmArmoryUnavailable("password=private secret path")
    monkeypatch.setattr(cli, "armory_from_config", lambda path: None if absent else Armory())
    assert cli.realm_main(["titles", "--data-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["armory"] == "unavailable" and result["trial_rank_exists"] is None
    assert "titles" in result and "private" not in output and "password" not in output


def test_cli_unknown_step_exit1(tmp_path, store, clock, capsys, monkeypatch):
    assert cli.realm_main(["plan", "no-such-step", "--data-dir", str(tmp_path)], clock=clock) == 1
    output = capsys.readouterr().out
    assert "unknown step" in output and "no-such-step" not in output
    # A registered step whose module is not in this build is not available, and says so.
    monkeypatch.setitem(cli.PLAN_STEPS, "ghost", cli.Entry("no_such_realm_module", "plan_cli", False))
    monkeypatch.setitem(cli.CANON_ACTIONS, "ghost", cli.Entry("no_such_realm_module", "history_ok_cli", False))
    for argv in (["plan", "ghost"], ["canon", "ghost", "seed.json", "canon.json"]):
        assert cli.realm_main([*argv, "--data-dir", str(tmp_path)], clock=clock) == 1
        output = capsys.readouterr().out
        assert "not available in this build" in output and str(tmp_path) not in output


def test_cli_model_step_refused_in_night_window(tmp_path, store, clock, capsys, monkeypatch):
    from leeroy_mind.clock import night_of
    calls = []
    def plan_cli(ctx):
        calls.append((ctx.name, ctx.argv, ctx.root))
        return 0
    monkeypatch.setitem(cli.PLAN_STEPS, "dreamy", cli.Entry("dreamy", "plan_cli", True))
    monkeypatch.setitem(cli.PLAN_STEPS, "plain", cli.Entry("plain", "plan_cli", False))
    monkeypatch.setattr(cli, "load_entry", lambda entry: plan_cli)
    with store.conn:
        store.conn.execute("INSERT INTO night_run(night,window_start,window_end,status,started) "
                           "VALUES (?,0,?,'running',?)", (night_of(clock.local()), clock.now(), clock.now()))
    argv = ["plan", "dreamy", "--progression-name", "Rival", "--data-dir", str(tmp_path)]
    assert cli.realm_main(argv, clock=clock) == 2
    assert "night_window" in capsys.readouterr().out and calls == []
    # A step without the model runs during the night; the model step once the night is done.
    assert cli.realm_main(["plan", "plain", "--data-dir", str(tmp_path)], clock=clock) == 0
    with store.conn:
        store.conn.execute("UPDATE night_run SET status='done',finished=?", (clock.now(),))
    assert cli.realm_main(argv, clock=clock) == 0
    assert calls == [("plain", (), tmp_path), ("dreamy", ("--progression-name", "Rival"), tmp_path)]


def test_cards_show_reads_only_the_cards_directory(tmp_path, capsys):
    cards = tmp_path / "realm" / "cards"
    cards.mkdir(parents=True)
    (cards / "3001.json").write_text(json.dumps({"name": "Vex", "role": "officer"}), encoding="utf-8")
    (cards / "3002.json").write_text("{broken", encoding="utf-8")
    assert cli.realm_main(["cards", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"cards": ["3001", "3002"], "count": 2}
    assert cli.realm_main(["cards", "--show", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "cards": {"3001": {"name": "Vex", "role": "officer"}, "3002": {"error": "unreadable"}}, "count": 2}
    assert not (tmp_path / "mind.db").exists()


def test_cli_errors_do_not_echo_local_values(tmp_path, store, capsys):
    (tmp_path / "mind.toml").write_text('[realm]\ntrial_rank = "private-name"\n')
    assert cli.realm_main(["titles", "--data-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "private-name" not in output and str(tmp_path) not in output


ROOT_ID = "b0" * 16


def stored_unknown(store, command_id=ROOT_ID, guids=tuple(range(101, 207)), reason="busy:body_identity",
                   kind="rearm", created=1793900000.0):
    """An unknown rearm as an older build left it: acked, then its job failed."""
    from leeroy_mind.realm import protocol
    from leeroy_mind.realm.action_policy import classify, document
    c = classify(protocol.Rearm(command_id, guids))
    ev = {"ack": {"error": "", "ok": True}, "failed_at": created + 0.1,
          "job": {"guid": guids[27 % len(guids)], "kind": kind, "pvp_owner": "", "reason": reason,
                  "state": "failed", "value": 0}}
    with store.conn:
        store.conn.execute(
            "INSERT INTO realm_action(command_id,parent_id,role,policy,owner,rank,kind,payload,reserved_manifest,"
            "native_manifest,guild,human,state,transport_state,process_id,connection_epoch,send_error,evidence,"
            "result_ok,result_reason,created,updated,dispatched) VALUES (?,NULL,'root','ordinary','realm_day',4,?,?,?,"
            "NULL,21,1,'unknown','settled','old','1','',?,0,'job_failed',?,?,?)",
            (command_id, c.kind, c.payload, document(list(c.reserved_manifest)), document(ev),
             created, created + 0.1, created + 0.01))


def fresh(tmp_path):
    conn = sqlite3.connect((tmp_path / "mind.db").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        state = conn.execute("SELECT state,result_reason,evidence FROM realm_action WHERE command_id=?",
                             (ROOT_ID,)).fetchone()
        meta = conn.execute("SELECT value FROM meta WHERE key=?", ("realm:action:abandoned:" + ROOT_ID,)).fetchone()
        return state, None if meta is None else json.loads(meta[0])
    finally:
        conn.close()


def release(tmp_path, clock, *extra, answer=None):
    asked = []
    def ask(prompt):
        asked.append(prompt)
        return answer
    code = cli.realm_main(["release", *extra, "--data-dir", str(tmp_path)], clock=clock, ask=ask)
    return code, asked


def test_release_shows_the_root_and_releases_it_on_its_typed_id(tmp_path, store, clock, capsys):
    stored_unknown(store, created=clock.now() - 9 * 3600)
    code, asked = release(tmp_path, clock, ROOT_ID, "--reason", "native restarted; nothing holds them",
                          answer=ROOT_ID)
    assert code == 0 and len(asked) == 1
    output = capsys.readouterr().out
    for shown in ("rearm", "unknown", "job_failed", "busy:body_identity", "106 members", "9.0 hours"):
        assert shown in output
    (state, result, evidence), receipt = fresh(tmp_path)
    assert (state, result) == ("completed", "abandoned")
    assert json.loads(evidence)["abandoned"] == clock.now()
    assert receipt["reason"] == "native restarted; nothing holds them" and receipt["at"] == clock.now()
    assert receipt["members"] == list(range(101, 207)) and receipt["state"] == "unknown"
    from leeroy_mind.realm.action_store import ActionStore
    assert ActionStore(store.conn).holds() == ()


def test_release_counts_only_the_members_a_failed_leave_still_holds(tmp_path, store, clock, capsys):
    from leeroy_mind.protocol import Ack
    from leeroy_mind.realm import protocol
    from leeroy_mind.realm.action_policy import classify
    from leeroy_mind.realm.action_store import ActionSpec, ActionStore
    journal = ActionStore(store.conn)
    c = classify(protocol.LeavePvp(ROOT_ID, (101, 102, 103)))
    r = journal.prepare(ActionSpec(ROOT_ID, None, "root", c.policy, "realm_day", 4, c.kind, c.payload,
                                   c.reserved_manifest, 21, 1), now=clock.now() - 7200)
    journal.begin_dispatch(ROOT_ID, payload=r.payload, process_id="old", connection_epoch=1, now=clock.now() - 7199)
    journal.observe_ack(Ack(0, ROOT_ID, True, ""), now=clock.now() - 7198)
    journal.observe_realm_job(protocol.RealmJob(0, ROOT_ID, "leave_pvp", "failed", "cleanup_manifest_readback_changed",
                                                103, 0, released=(101, 103)), now=clock.now() - 7197)
    code, _ = release(tmp_path, clock, ROOT_ID, "--reason", "checked", "--yes")
    output = capsys.readouterr().out
    assert code == 0 and "1 members held" in output and "1 members free" in output


@pytest.mark.parametrize("answer", ["", "no", ROOT_ID[:-1], ROOT_ID + " ", None])
def test_release_without_the_exact_typed_id_changes_nothing(tmp_path, store, clock, capsys, answer):
    stored_unknown(store)
    before = fresh(tmp_path)
    code, asked = release(tmp_path, clock, ROOT_ID, "--reason", "checked", answer=answer)
    assert code == 1 and asked
    assert "not released" in capsys.readouterr().out
    assert fresh(tmp_path) == before


def test_release_yes_only_with_the_exact_id(tmp_path, store, clock, capsys):
    stored_unknown(store)
    before = fresh(tmp_path)
    for wrong in (ROOT_ID[:8], ROOT_ID.upper(), "1"):
        code, asked = release(tmp_path, clock, wrong, "--reason", "checked", "--yes")
        assert code == 1 and not asked
        output = capsys.readouterr().out
        assert "no such root" in output and wrong not in output
    assert fresh(tmp_path) == before
    code, asked = release(tmp_path, clock, ROOT_ID, "--reason", "checked", "--yes")
    assert code == 0 and not asked
    assert fresh(tmp_path)[0][:2] == ("completed", "abandoned")


def test_release_needs_the_mind_stopped(tmp_path, store, clock, capsys):
    from leeroy_mind.app import acquire_instance_lock
    stored_unknown(store)
    before = fresh(tmp_path)
    with acquire_instance_lock(tmp_path):
        code, asked = release(tmp_path, clock, ROOT_ID, "--reason", "checked", "--yes")
    assert code == 1 and not asked
    assert "mind.lock is held" in capsys.readouterr().out
    assert fresh(tmp_path) == before


def test_release_refuses_a_root_that_is_not_unknown(tmp_path, store, clock, capsys):
    stored_unknown(store)
    with store.conn:
        store.conn.execute("UPDATE realm_action SET state='dispatched',result_ok=NULL,result_reason='',"
                           "transport_state='inflight' WHERE command_id=?", (ROOT_ID,))
    before = fresh(tmp_path)
    code, asked = release(tmp_path, clock, ROOT_ID, "--reason", "checked", answer=ROOT_ID)
    assert code == 1 and not asked
    assert "only an unknown root can be released; this one is dispatched" in capsys.readouterr().out
    assert fresh(tmp_path) == before


def test_release_needs_a_reason_and_an_unmigrated_db_is_left_alone(tmp_path, clock, capsys):
    with pytest.raises(SystemExit):
        cli.realm_main(["release", ROOT_ID, "--data-dir", str(tmp_path)], clock=clock, ask=lambda p: ROOT_ID)
    path = tmp_path / "mind.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_data(id INTEGER)")
    before = path.read_bytes()
    code, asked = release(tmp_path, clock, ROOT_ID, "--reason", "checked", "--yes")
    assert code == 1 and path.read_bytes() == before
    assert "not migrated" in capsys.readouterr().out


def test_release_help_says_a_contradicted_release_goes_to_conflict(capsys):
    with pytest.raises(SystemExit):
        cli.realm_main(["release", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "conflict included" in text
    assert "native evidence later contradicts goes to conflict and stays held" in text


def test_release_refuses_a_conflicted_root(tmp_path, store, clock, capsys):
    stored_unknown(store)
    with store.conn:
        store.conn.execute("UPDATE realm_action SET state='conflict',result_reason='conflict' WHERE command_id=?",
                           (ROOT_ID,))
    code, asked = release(tmp_path, clock, ROOT_ID, "--reason", "checked", answer=ROOT_ID)
    assert code == 1 and not asked
    assert "only an unknown root can be released; this one is conflict" in capsys.readouterr().out
