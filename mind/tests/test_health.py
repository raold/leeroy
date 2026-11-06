"""The health check (leeroy_mind.health): each check against a scratch install, with fakes for
the process lists, the TCP table, the control endpoint, HTTP and the voice service; the toast's
suppression, without a toast; and its rules: it never dials the bridge's port, never prints the
control key and writes nothing but health-state.json, and only with --toast."""
import json
import os
import socket
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import seed_test_cast
from leeroy_mind import endpoint, health, lifecycle
from leeroy_mind.config import load_config
from leeroy_mind.console.netinfo import ESTABLISHED, LISTEN, Conn
from leeroy_mind.endpoint import KEY_FILE
from leeroy_mind.health import FAIL, NOTE, OK, Probes, Server, alert, read_state
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock

NOW = 1_793_900_000.0  # 2026-11-05
WORLD_START = NOW - 3600
MIND_START = NOW - 1800
KEY = "K" * 43  # the shape of secrets.token_urlsafe(32)
CONTROL_PORT = 45000
MODELS = load_config(Path("missing.toml")).models
CLOCK = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
MIND_PID, LAUNCHER_PID, VOICE_PID = 101, 100, 4242


def filetime(t: float) -> int:
    return (int(t) + health.FILETIME_EPOCH) * 10**7


def stamp(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def record(t: float, event: str, level: str = "INFO", **fields) -> str:
    ts = datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")
    return json.dumps({"ts": ts, "level": level, "event": event, **fields})


class Processes:
    def __init__(self, rows):
        self.rows = list(rows)
        self.snapshots = 0

    def snapshot(self):
        self.snapshots += 1
        return list(self.rows)


class Endpoint:
    """endpoint.request's fake: the calls it got and the reply it gives."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or {"ok": True, "v": 1, "pid": MIND_PID, "stopping": False,
                               "raid": None, "bridge_connected": True, "cast": 111,
                               "schema_version": 17}

    def __call__(self, port, key, op, args=None, *, timeout):
        self.calls.append((port, key, op, args))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Voice:
    def __init__(self, state="running", *, code=0, missing=None, where=None):
        self.state, self.code, self._missing, self._where = state, code, missing, where

    def missing(self):
        return self._missing

    def where(self):
        if isinstance(self._where, Exception):
            raise self._where
        return lifecycle.VoiceWhere(Path("C:/voice-data"), 7879, 7880)

    def status(self):
        if self.state == "stopped":
            return lifecycle.Outcome(0, "voice stopped", "stopped")
        return lifecycle.Outcome(self.code, f"voice pid {VOICE_PID} (running)", "running",
                                 VOICE_PID)


class Rig:
    """A scratch install: build/bin with Server.log and mod_leeroy.conf, a data dir with
    mind.toml, a migrated mind.db with a cast and personas, mind.log and control.json, and
    probes that see a healthy install. Each test breaks one thing."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "install"
        self.bin = health.bin_dir(self.root)
        (self.bin / "configs" / "modules").mkdir(parents=True)
        self.data = tmp_path / "mind-data"
        (self.data / "logs").mkdir(parents=True)
        self.python = lifecycle.venv_python(self.root)
        self.base = tmp_path / "base" / "python.exe"
        self.toml = "[training]\nenabled = true\n\n[voice]\nenabled = true\n"
        self.write_toml()
        self.conf("Leeroy.Telemetry.Enable = 1\n")
        store = Store(open_db(self.data / "mind.db"), clock=FakeClock(CLOCK))
        try:
            seed_test_cast(store)
        finally:
            store.close()
        self.pull(NOW - 3600)
        self.server_log([(WORLD_START + 1, "AzerothCore rev. test (worldserver-daemon)"),
                         (WORLD_START + 60, "mod-leeroy: realm on, cast guard on, places 19, "
                                            "items 49, max actions/tick 5"),
                         (WORLD_START + 60, "mod-leeroy: cast 111 members, pinned 111 (keep=1, "
                                            "online=1), ollama filter on, bridge 127.0.0.1:7878 "
                                            "listening")])
        self.mind_log([record(MIND_START + 1, "mind_started"),
                       record(MIND_START + 2, "bridge_connected"),
                       record(MIND_START + 3, "bridge_hello", server="mod-leeroy")])
        (self.data / KEY_FILE).write_text(json.dumps(
            {"v": 1, "pid": MIND_PID, "port": CONTROL_PORT, "key": KEY}), encoding="ascii")
        args = (str(self.python), "-m", "leeroy_mind", "run", "--data-dir", str(self.data))
        self.mind = [lifecycle.Process(LAUNCHER_PID, 20, filetime(MIND_START), str(self.python),
                                       args),
                     lifecycle.Process(MIND_PID, LAUNCHER_PID, filetime(MIND_START) + 10,
                                       str(self.base), args)]
        self.servers = [Server("authserver.exe", 300, WORLD_START,
                               str(self.bin / "authserver.exe")),
                        Server("worldserver.exe", 301, WORLD_START,
                               str(self.bin / "worldserver.exe"))]
        self.held = True
        self.conns = [Conn("127.0.0.1", CONTROL_PORT, "0.0.0.0", 0, LISTEN, MIND_PID),
                      Conn("127.0.0.1", 7879, "0.0.0.0", 0, LISTEN, VOICE_PID),
                      Conn("127.0.0.1", 7880, "0.0.0.0", 0, LISTEN, VOICE_PID),
                      Conn("127.0.0.1", 50000, "127.0.0.1", 7878, ESTABLISHED, MIND_PID)]
        self.endpoint = Endpoint()
        self.console = 200
        self.ollama = {"models": [{"name": MODELS.live, "size": 1, "size_vram": 1},
                                  {"name": "other:latest", "size": 1, "size_vram": 1}]}
        self.fetched = []
        self.voice = Voice()

    # -- the files ------------------------------------------------------------------------

    def write_toml(self, extra: str = "") -> None:
        (self.data / "mind.toml").write_text(self.toml + extra, encoding="utf-8")

    def conf(self, text: str) -> None:
        (self.bin / "configs" / "modules" / "mod_leeroy.conf").write_text(text, encoding="utf-8")

    def server_log(self, lines) -> None:
        (self.bin / "Server.log").write_text("".join(f"{stamp(t)} {text}\n" for t, text in lines),
                                             encoding="utf-8")

    def mind_log(self, records, name: str = "mind.log") -> None:
        (self.data / "logs" / name).write_text("".join(r + "\n" for r in records),
                                               encoding="utf-8")

    def db(self, sql: str, params=()) -> None:
        conn = sqlite3.connect(self.data / "mind.db")
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()

    def pull(self, received: float) -> None:
        self.db("INSERT INTO tg_pull (id, context, map, instance_id, boss_id, difficulty, size, "
                "outcome, start_ms, end_ms, members, assisted, boss_entry, upd_mean_ms, kind, "
                "complete, scoreable, received) VALUES (?, '631:0:0', 631, 5, 0, 0, 10, "
                "'killed', 1, 2, 10, 0, 36612, 1, 'offline', 1, 1, ?)",
                (f"pull-{received}", received))

    def action(self, kind: str, state: str, transport: str, manifest, created: float) -> None:
        self.db("INSERT INTO realm_action (command_id, role, policy, owner, rank, kind, payload, "
                "reserved_manifest, guild, human, state, transport_state, created, updated) "
                "VALUES (?, 'root', 'ordinary', 'realm', 1, ?, '{}', ?, 21, 1, ?, ?, ?, ?)",
                (f"{kind}-{created}", kind, json.dumps(manifest), state, transport, created,
                 created))

    # -- the probes -----------------------------------------------------------------------

    def http_status(self, url, timeout):
        assert url == f"http://127.0.0.1:{health.CONSOLE_PORT}/"
        if isinstance(self.console, Exception):
            raise self.console
        return self.console

    def fetch_json(self, url, timeout):
        self.fetched.append(url)
        if isinstance(self.ollama, Exception):
            raise self.ollama
        return self.ollama

    def servers_now(self):
        if isinstance(self.servers, Exception):
            raise self.servers
        return list(self.servers)

    def probes(self, **over) -> Probes:
        self.processes = Processes(self.mind)
        kw = dict(now=lambda: NOW, servers=self.servers_now, processes=self.processes,
                  locked=lambda data: self.held, python=self.python, base_python=str(self.base),
                  tcp=lambda: self.conns, request=self.endpoint, http_status=self.http_status,
                  fetch_json=self.fetch_json, voice=self.voice,
                  open_log=lambda path: open(path, "rb"))
        kw.update(over)
        return Probes(**kw)

    def run(self, **over) -> dict:
        checks = health.run_checks(self.root, self.data, probes=self.probes(**over))
        names = [c.name for c in checks]
        assert len(names) == len(set(names)), names  # one line per check
        return {c.name: c for c in checks}


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# -- a healthy install ---------------------------------------------------------------------------

def test_a_healthy_install_is_all_ok_with_its_notes(rig):
    checks = rig.run()
    assert {n: c.status for n, c in checks.items()} == {
        "authserver": OK, "worldserver": OK, "server-log": OK, "realm-lists": OK, "mind": OK,
        "mind-log": OK, "mind-critical": OK, "endpoint": OK, "console": OK, "voice": OK,
        "ollama": OK, "ollama-models": NOTE, "telemetry": OK, "realm-holds": OK, "unvoiced": OK}
    assert checks["server-log"].text == "Server.log: cast 111 members, pinned 111 (floor 100)"
    assert checks["mind"].text.startswith(f"mind: mind pid {MIND_PID} (running)")
    assert "bridge_hello" in checks["mind-log"].text
    assert checks["endpoint"].text == ("control endpoint: status answered: bridge connected, "
                                       "cast 111, schema v17")
    assert checks["voice"].text == (f"voice service: voice pid {VOICE_PID} (running), "
                                    "listening on 7879 and 7880")
    assert checks["ollama-models"].text == f"ollama: 2 loaded: {MODELS.live} (live), other:latest"
    # the endpoint got the key once, at the port control.json names, for status
    assert rig.endpoint.calls == [(CONTROL_PORT, KEY, "status", None)]
    assert rig.fetched == ["http://127.0.0.1:11434/api/ps"]  # Ollama: /api/ps only
    assert rig.processes.snapshots == 1  # one process list for the verdict and the start


def test_the_command_prints_one_line_each_or_json_and_exits_by_the_fails(rig, capsys):
    argv = ["--root", str(rig.root), "--data-dir", str(rig.data)]
    assert health.main(argv, probes=rig.probes()) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 15 and lines[0].startswith("ok   authserver.exe running")
    assert all(line[:5] in ("ok   ", "note ") for line in lines)
    assert health.main(argv + ["--json"], probes=rig.probes()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and len(out["checks"]) == 15
    assert out["checks"][0] == {"name": "authserver", "status": "ok",
                                "text": out["checks"][0]["text"]}
    rig.servers = [s for s in rig.servers if s.image != "authserver.exe"]
    assert health.main(argv + ["--json"], probes=rig.probes()) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert [c["name"] for c in out["checks"] if c["status"] == FAIL] == ["authserver"]
    assert health.main(argv, probes=rig.probes()) == 1
    assert capsys.readouterr().out.splitlines()[0].startswith("FAIL authserver.exe is not running")


def test_a_data_dir_without_the_mind_is_one_fail(tmp_path):
    checks = health.run_checks(tmp_path, tmp_path / "nowhere", probes=None)
    assert [(c.name, c.status) for c in checks] == [("data-dir", FAIL)]


# -- the game servers and Server.log -------------------------------------------------------------

def test_servers_down_fail_and_server_log_is_not_checked(rig):
    rig.servers = []
    checks = rig.run()
    assert checks["authserver"].status == checks["worldserver"].status == FAIL
    assert checks["worldserver"].text == f"worldserver.exe is not running from {rig.bin}"
    assert checks["server-log"].status == NOTE and "realm-lists" not in checks


def test_no_process_list_fails_both_servers(rig):
    rig.servers = OSError("no CIM")
    checks = rig.run()
    assert checks["authserver"].status == checks["worldserver"].status == FAIL
    assert "process list is unavailable (OSError)" in checks["worldserver"].text


def test_a_worldserver_from_another_tree_is_not_the_live_one(rig, tmp_path):
    rig.servers = [rig.servers[0], Server("worldserver.exe", 302, WORLD_START,
                                          str(tmp_path / "dojo-runtime" / "worldserver.exe"))]
    checks = rig.run()
    assert checks["worldserver"].status == FAIL
    assert checks["worldserver"].text.endswith("(1 running from elsewhere)")


def test_the_cast_line_must_be_newer_than_worldservers_start(rig):
    rig.servers[1] = Server("worldserver.exe", 301, NOW - 600, str(rig.bin / "worldserver.exe"))
    checks = rig.run()  # Server.log's lines are from the run before
    assert checks["server-log"].status == FAIL
    assert "no `mod-leeroy: cast N members, pinned N` line since" in checks["server-log"].text


def test_a_fresh_worldserver_without_its_cast_line_yet_is_a_note(rig):
    rig.servers[1] = Server("worldserver.exe", 301, NOW - 30, str(rig.bin / "worldserver.exe"))
    rig.server_log([(NOW - 29, "AzerothCore rev. test (worldserver-daemon)")])
    checks = rig.run()
    assert checks["server-log"].status == NOTE
    assert checks["server-log"].text == ("Server.log: no cast line yet; worldserver started 30 s "
                                         "ago")


def test_the_cast_floor_comes_from_health_with_a_default(rig):
    rig.server_log([(WORLD_START + 60, "mod-leeroy: cast 111 members, pinned 3 (keep=1)")])
    checks = rig.run()
    assert checks["server-log"].status == FAIL
    assert checks["server-log"].text == ("Server.log: cast 111 members, pinned 3, below the "
                                         "floor of 100 ([health] min_cast)")
    rig.write_toml("\n[health]\nmin_cast = 3\n")
    assert rig.run()["server-log"].status == OK
    rig.write_toml("\n[health]\nmin_cast = 112\n")
    assert rig.run()["server-log"].status == FAIL


def test_a_bad_health_table_fails_and_the_defaults_are_used(rig):
    rig.write_toml("\n[health]\nmin_cast = \"lots\"\n")
    checks = rig.run()
    assert checks["health-config"].status == FAIL
    assert checks["health-config"].text.startswith("mind.toml health.min_cast: expected an integer")
    assert checks["server-log"].text.endswith("(floor 100)")
    rig.write_toml("\n[health]\ncolour = 1\n")
    assert rig.run()["health-config"].status == FAIL


def test_a_realm_list_that_did_not_load_since_the_start_fails(rig):
    before = (WORLD_START - 600, "mod-leeroy: realm places file x is missing, so no places")
    cast = (WORLD_START + 60, "mod-leeroy: cast 111 members, pinned 111 (keep=1)")
    rig.server_log([before, cast])
    assert rig.run()["realm-lists"].status == OK  # an earlier run's line does not count
    rig.server_log([cast, (WORLD_START + 61, "mod-leeroy: realm items file x cannot be read, "
                                             "so no items")])
    checks = rig.run()
    assert checks["realm-lists"].status == FAIL
    assert "the realm items file did not load" in checks["realm-lists"].text


# -- the mind and mind.log -----------------------------------------------------------------------

def test_a_stopped_mind_fails_and_what_needs_it_is_not_checked(rig):
    rig.mind, rig.held = [], False
    checks = rig.run()
    assert checks["mind"].status == FAIL and checks["mind"].text == "mind: stopped"
    for name in ("mind-log", "endpoint"):
        assert checks[name].status == NOTE and "the mind is not running" in checks[name].text
    assert rig.endpoint.calls == []


def test_a_held_lock_without_this_checkouts_mind_fails(rig):
    rig.mind = []
    checks = rig.run()
    assert checks["mind"].status == FAIL
    assert "mind.lock is held but its mind process is unidentified" in checks["mind"].text


def test_a_starting_mind_is_a_note(rig):
    rig.held = False
    checks = rig.run()
    assert checks["mind"].status == NOTE and "starting" in checks["mind"].text


def test_the_bridge_must_have_come_up_since_the_minds_start(rig):
    rig.mind_log([record(MIND_START - 60, "bridge_hello"), record(MIND_START + 1, "mind_started")])
    checks = rig.run()
    assert checks["mind-log"].status == FAIL
    assert "no bridge_connected or bridge_hello since the mind started" in checks["mind-log"].text
    rig.mind_log([record(MIND_START + 2, "bridge_hello"),
                  record(MIND_START + 900, "bridge_disconnected")])
    checks = rig.run()
    assert checks["mind-log"].status == FAIL and "disconnected" in checks["mind-log"].text


def test_a_fresh_mind_waiting_for_its_bridge_is_a_note(rig):
    start = NOW - 20
    rig.mind = [lifecycle.Process(p.pid, p.parent, filetime(start) + i, p.exe, p.argv)
                for i, p in enumerate(rig.mind)]
    rig.mind_log([record(start + 1, "mind_started")])
    checks = rig.run()
    assert checks["mind-log"].status == NOTE
    assert checks["mind-log"].text.endswith("the mind started 20 s ago")


def test_the_rotated_log_since_the_start_is_read_too(rig):
    start = NOW - 3 * 86400
    rig.mind = [lifecycle.Process(p.pid, p.parent, filetime(start) + i, p.exe, p.argv)
                for i, p in enumerate(rig.mind)]
    day = time.strftime("%Y-%m-%d", time.localtime(start))
    rig.mind_log([record(start + 2, "bridge_hello")], name=f"mind.log.{day}")
    rig.mind_log([record(NOW - 60, "voice_connect_failed")])
    rig.mind_log([record(start - 86400, "bridge_hello")], name="mind.log.2020-01-01")
    checks = rig.run()
    assert checks["mind-log"].status == OK and "bridge_hello" in checks["mind-log"].text


def test_a_critical_record_since_the_start_fails(rig):
    rig.mind_log([record(MIND_START - 10, "loop_died", level="CRITICAL"),
                  record(MIND_START + 3, "bridge_hello")])
    assert rig.run()["mind-critical"].status == OK  # before this run
    rig.mind_log([record(MIND_START + 3, "bridge_hello"),
                  record(MIND_START + 50, "loop_died", level="CRITICAL", msg="what was said")])
    checks = rig.run()
    assert checks["mind-critical"].status == FAIL
    assert checks["mind-critical"].text == ("mind.log: 1 CRITICAL record since the mind started "
                                            "(last loop_died)")


# -- the control endpoint ------------------------------------------------------------------------

def test_the_key_goes_only_to_the_running_minds_own_listener(rig):
    rig.conns = [c for c in rig.conns if c.local_port != CONTROL_PORT] + [
        Conn("127.0.0.1", CONTROL_PORT, "0.0.0.0", 0, LISTEN, 9999)]  # another program
    checks = rig.run()
    assert checks["endpoint"].status == FAIL
    assert "not listening where control.json says" in checks["endpoint"].text
    assert rig.endpoint.calls == []
    (rig.data / KEY_FILE).write_text(json.dumps(
        {"v": 1, "pid": 4321, "port": CONTROL_PORT, "key": KEY}), encoding="ascii")
    assert rig.run()["endpoint"].status == FAIL and rig.endpoint.calls == []


def test_the_endpoint_reports_a_down_bridge_and_a_short_cast(rig):
    rig.endpoint.reply = dict(rig.endpoint.reply, bridge_connected=False)
    checks = rig.run()
    assert checks["endpoint"].status == FAIL and "bridge NOT connected" in checks["endpoint"].text
    rig.endpoint.reply = dict(rig.endpoint.reply, bridge_connected=True, cast=40)
    checks = rig.run()
    assert checks["endpoint"].status == FAIL
    assert checks["endpoint"].text.endswith("cast 40, schema v17, below the floor of 100")


def test_an_older_mind_answers_without_the_new_fields(rig):
    rig.endpoint.reply = {"ok": True, "v": 1, "pid": MIND_PID, "stopping": False, "raid": None}
    checks = rig.run()
    assert checks["endpoint"].status == OK and "does not report" in checks["endpoint"].text


def test_an_endpoint_that_does_not_answer_or_is_off(rig):
    rig.endpoint.reply = ConnectionResetError("gone")
    assert rig.run()["endpoint"].status == FAIL
    rig.write_toml("\n[control]\nenabled = false\n")
    checks = rig.run()
    assert checks["endpoint"].status == NOTE and "off in mind.toml" in checks["endpoint"].text


# -- the console, the voice service and Ollama ---------------------------------------------------

def test_the_console_is_a_note_when_it_is_not_running(rig):
    import urllib.error
    rig.console = urllib.error.URLError(ConnectionRefusedError("refused"))
    checks = rig.run()
    assert checks["console"].status == NOTE
    assert checks["console"].text == "console: not running on 127.0.0.1:7881"
    rig.console = 403
    assert rig.run()["console"].status == FAIL
    rig.console = TimeoutError("hung")
    assert rig.run()["console"].status == FAIL


def test_the_voice_service_stopped_or_disabled_is_a_note_and_broken_fails(rig):
    rig.voice = Voice("stopped")
    assert rig.run()["voice"].status == NOTE
    rig.voice = Voice(missing="no voice venv at X")
    assert rig.run()["voice"].status == NOTE
    rig.voice = Voice(where=lifecycle.OwnershipError("cannot read the voice config"))
    assert rig.run()["voice"].status == NOTE
    rig.voice = Voice()
    rig.conns = [c for c in rig.conns if c.local_port != 7880]
    checks = rig.run()
    assert checks["voice"].status == FAIL and "port 7880 is not listening" in checks["voice"].text
    rig.voice = Voice(code=1)
    assert rig.run()["voice"].status == FAIL
    rig.toml = "[training]\nenabled = true\n"
    rig.write_toml()
    checks = rig.run()
    assert checks["voice"].status == NOTE and checks["voice"].text.endswith("disabled in mind.toml")


def test_ollama_unreachable_fails_and_the_loaded_models_are_a_note(rig):
    rig.ollama = {"models": []}
    checks = rig.run()
    assert checks["ollama"].status == OK
    assert checks["ollama-models"].text == "ollama: no model loaded"
    rig.ollama = ConnectionRefusedError("down")
    checks = rig.run()
    assert checks["ollama"].status == FAIL and "ollama-models" not in checks
    assert set(rig.fetched) == {"http://127.0.0.1:11434/api/ps"}


# -- activity in mind.db -------------------------------------------------------------------------

def test_telemetry_on_without_a_pull_in_48_hours_fails(rig):
    rig.db("DELETE FROM tg_pull")
    checks = rig.run()
    assert checks["telemetry"].status == FAIL
    assert checks["telemetry"].text == "telemetry: on, but no tg_pull in 48 h (last never)"
    rig.pull(NOW - 49 * 3600)
    assert rig.run()["telemetry"].status == FAIL
    rig.pull(NOW - 47 * 3600)
    assert rig.run()["telemetry"].status == OK


def test_telemetry_off_either_side_is_a_note(rig):
    rig.db("DELETE FROM tg_pull")
    rig.conf("Leeroy.Telemetry.Enable = 0\n")
    assert rig.run()["telemetry"].status == NOTE
    rig.conf("# no key: native's default, off\n")
    assert rig.run()["telemetry"].status == NOTE
    rig.conf("Leeroy.Telemetry.Enable = 1\n")
    rig.toml = "[voice]\nenabled = true\n"  # [training] is off by default
    rig.write_toml()
    checks = rig.run()
    assert checks["telemetry"].status == NOTE and "off in mind.toml" in checks["telemetry"].text


def test_a_realm_hold_or_unknown_root_older_than_an_hour_fails(rig):
    rig.action("rearm", "dispatched", "inflight", [101, 102], NOW - 600)  # still young
    rig.action("announce", "unknown", "settled", [], NOW - 300)
    rig.action("grow_done", "completed", "settled", [103], NOW - 9000)  # settled
    assert rig.run()["realm-holds"].status == OK
    rig.action("leave_pvp", "unknown", "settled", [101, 102, 103], NOW - 7200)
    rig.action("guild_grow", "unknown", "settled", [], NOW - 5000)
    checks = rig.run()
    assert checks["realm-holds"].status == FAIL
    assert checks["realm-holds"].text == (
        f"realm: 1 body hold (3 members) and 2 unknown roots older than 1 h, since "
        f"{health._when(NOW - 7200)}: guild_grow 1, leave_pvp 1")


def test_cast_without_a_persona_is_a_note(rig):
    store = Store(open_db(rig.data / "mind.db"), clock=FakeClock(CLOCK))
    try:
        store.upsert_cast([Member(guid=106, name="Fenna", race=1, class_=1, gender=1, level=80,
                                  rank=3, online=True, map=0, zone=0, spec_tab=-1)], 2000.0)
    finally:
        store.close()
    checks = rig.run()
    assert checks["unvoiced"].status == NOTE
    assert checks["unvoiced"].text.startswith("cast without a persona 1: Fenna (106)")


def test_a_mind_db_without_the_newer_tables_is_unavailable_not_failed(rig):
    for table in ("tg_pull", "realm_action"):
        rig.db(f"DROP TABLE {table}")
    checks = rig.run()
    assert checks["telemetry"].status == NOTE and "unavailable" in checks["telemetry"].text
    assert checks["realm-holds"].status == NOTE and "unavailable" in checks["realm-holds"].text


# -- the toast -----------------------------------------------------------------------------------

def checks_failing(*names):
    return [health.Check(n, FAIL, f"{n} failed") for n in names] + [
        health.Check("console", OK, "fine")]


def test_one_toast_per_new_failure_until_it_clears(tmp_path):
    shown = []

    def show(title, body):
        shown.append((title, body))
        return True
    state = tmp_path / health.STATE_FILE
    assert alert(checks_failing(), tmp_path, show=show) is False
    assert not state.exists() and shown == []  # nothing failing, nothing written
    assert alert(checks_failing("worldserver", "authserver"), tmp_path, show=show) is True
    assert shown == [("Leeroy health: 2 checks failing", "authserver, worldserver")]
    assert read_state(state) == {"authserver", "worldserver"}
    written = state.read_bytes()
    assert alert(checks_failing("authserver", "worldserver"), tmp_path, show=show) is False
    assert len(shown) == 1 and state.read_bytes() == written  # the same set: quiet, unwritten
    assert alert(checks_failing("worldserver"), tmp_path, show=show) is False  # one cleared
    assert len(shown) == 1 and read_state(state) == {"worldserver"}
    assert alert(checks_failing("worldserver", "authserver"), tmp_path, show=show) is True
    assert shown[-1] == ("Leeroy health: 2 checks failing", "authserver, worldserver")
    assert alert(checks_failing(), tmp_path, show=show) is False  # all clear
    assert read_state(state) == set() and len(shown) == 2
    assert alert(checks_failing("mind"), tmp_path, show=show) is True  # back: a new toast
    assert shown[-1] == ("Leeroy health: 1 check failing", "mind")


def test_a_toast_that_could_not_be_shown_is_tried_again(tmp_path):
    tries = []
    assert alert(checks_failing("mind"), tmp_path, show=lambda t, b: tries.append(b) or False) \
        is False
    assert not (tmp_path / health.STATE_FILE).exists()
    assert alert(checks_failing("mind"), tmp_path, show=lambda t, b: tries.append(b) or True)
    assert tries == ["mind", "mind"]


def test_an_unreadable_state_file_reads_as_nothing_failing(tmp_path):
    state = tmp_path / health.STATE_FILE
    for text in ("", "not json", "[]", '{"failing": "mind"}', '{"failing": [1, "mind"]}'):
        state.write_text(text, encoding="utf-8")
        assert read_state(state) <= {"mind"}


def test_toast_only_with_the_flag_and_never_in_the_text(rig, capsys):
    rig.servers = []
    shown = []
    argv = ["--root", str(rig.root), "--data-dir", str(rig.data)]
    assert health.main(argv, probes=rig.probes(), show=lambda t, b: shown.append(b)) == 1
    assert shown == [] and not (rig.data / health.STATE_FILE).exists()
    assert health.main(argv + ["--toast"], probes=rig.probes(),
                       show=lambda t, b: shown.append(b) or True) == 1
    assert shown == ["authserver, worldserver"]  # names only


# -- the rules -----------------------------------------------------------------------------------

# SQLite's own WAL index: a mode=ro reader makes them when no writer has mind.db open, as
# doctor's and the console's readers do; they hold no data
SQLITE_INDEX = ("mind.db-wal", "mind.db-shm")


def _files(*roots):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for root in roots
            for p in root.rglob("*") if p.is_file() and p.name not in SQLITE_INDEX}


def test_it_writes_nothing_but_health_state_and_only_with_toast(rig, capsys):
    rig.servers = []  # something fails, so --toast writes its state
    before = _files(rig.root, rig.data)
    argv = ["--root", str(rig.root), "--data-dir", str(rig.data)]
    assert health.main(argv + ["--json"], probes=rig.probes()) == 1
    assert _files(rig.root, rig.data) == before
    assert health.main(argv + ["--toast"], probes=rig.probes(), show=lambda t, b: True) == 1
    after = _files(rig.root, rig.data)
    assert set(after) - set(before) == {rig.data / health.STATE_FILE}
    assert {p: v for p, v in after.items() if p in before} == before


def test_it_never_prints_the_control_key(rig, capsys):
    rig.endpoint.reply = {"ok": False, "error": "forbidden"}
    argv = ["--root", str(rig.root), "--data-dir", str(rig.data)]
    health.main(argv, probes=rig.probes())
    health.main(argv + ["--json"], probes=rig.probes())
    out = capsys.readouterr()
    assert KEY not in out.out and KEY not in out.err


def test_it_never_dials_the_bridge_port(rig, monkeypatch):
    """With the real endpoint client and the real HTTP code under a socket that records every
    dial and refuses it: control.json, the TCP table and [ollama] url all name the bridge's port,
    and nothing dials it. The console's port is dialled, so the patch saw the dials."""
    dialled = []

    def create_connection(address, *args, **kwargs):
        dialled.append(address[1])
        raise ConnectionRefusedError("refused by the test")

    def connect(self, address):
        dialled.append(address[1])
        raise ConnectionRefusedError("refused by the test")
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect)
    from leeroy_mind.console import status
    for port in (7878, 17878):
        rig.write_toml(f"\n[bridge]\nport = {port}\n\n[ollama]\n"
                       f"url = \"http://127.0.0.1:{port}\"\n")
        (rig.data / KEY_FILE).write_text(json.dumps(
            {"v": 1, "pid": MIND_PID, "port": port, "key": KEY}), encoding="ascii")
        rig.conns = [Conn("127.0.0.1", port, "0.0.0.0", 0, LISTEN, MIND_PID)]
        checks = rig.run(request=endpoint.request, http_status=health.http_status,
                         fetch_json=status._fetch_json)
        assert checks["endpoint"].status == FAIL and "not dialled" in checks["endpoint"].text
        assert checks["ollama"].status == FAIL and "not dialled" in checks["ollama"].text
        assert checks["console"].status == NOTE  # refused: dialled, so the patch is in force
        assert port not in dialled and 7878 not in dialled
    assert dialled and set(dialled) == {health.CONSOLE_PORT}


def test_the_scheduled_interpreter_finds_the_minds_python(monkeypatch, tmp_path):
    monkeypatch.setattr("sys._base_executable", str(tmp_path / "Python314" / "pythonw.exe"),
                        raising=False)
    assert health.base_python() == str(tmp_path / "Python314" / "python.exe")
    monkeypatch.setattr("sys._base_executable", str(tmp_path / "Python314" / "python.exe"),
                        raising=False)
    assert health.base_python() == str(tmp_path / "Python314" / "python.exe")


@pytest.mark.skipif(os.name != "nt", reason="the toast is Windows PowerShell's")
def test_the_toast_text_goes_through_the_environment_only(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, env=kw["env"])
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(health.subprocess, "run", run)
    assert health.show_toast("Leeroy health: 1 check failing", "mind") is True
    assert seen["argv"] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                            health.TOAST_PS]  # the script is fixed: nothing interpolated
    assert seen["env"]["LEEROY_HEALTH_TITLE"] == "Leeroy health: 1 check failing"
    assert seen["env"]["LEEROY_HEALTH_BODY"] == "mind"
