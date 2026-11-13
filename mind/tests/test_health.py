"""The health check (leeroy_mind.health): each check against a scratch install, with fakes for
the process lists, the TCP table, the control endpoint, HTTP and the voice service; the toast's
suppression, without a toast; and its rules: it never dials the bridge's port, never takes
mind.lock or voice.lock, never prints the control key and writes nothing but health-state.json,
and only with --toast."""
import dataclasses
from contextlib import closing
import json
import logging
import os
import random
import socket
import sqlite3
import subprocess
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
from dbtemplate import open_migrated
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
    """VoiceControl's surface the check may use: missing() and where(). Its lock-taking
    status() and held() refuse."""

    def __init__(self, data_dir, *, missing=None, where=None):
        self.data_dir, self._missing, self._where = Path(data_dir), missing, where

    def missing(self):
        return self._missing

    def where(self):
        if isinstance(self._where, Exception):
            raise self._where
        return lifecycle.VoiceWhere(self.data_dir, 7879, 7880)

    def status(self):
        raise AssertionError("status() probes voice.lock")

    def held(self):
        raise AssertionError("held() probes voice.lock")


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
        store = Store(open_migrated(self.data / "mind.db"), clock=FakeClock(CLOCK))
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
        self.conns = [Conn("127.0.0.1", CONTROL_PORT, "0.0.0.0", 0, LISTEN, MIND_PID),
                      Conn("127.0.0.1", 7879, "0.0.0.0", 0, LISTEN, VOICE_PID),
                      Conn("127.0.0.1", 7880, "0.0.0.0", 0, LISTEN, VOICE_PID),
                      Conn("127.0.0.1", 50000, "127.0.0.1", 7878, ESTABLISHED, MIND_PID)]
        self.endpoint = Endpoint()
        self.console = 200
        self.ollama = {"models": [{"name": MODELS.live, "size": 1, "size_vram": 1},
                                  {"name": "other:latest", "size": 1, "size_vram": 1}]}
        self.fetched = []
        self.voice_data = tmp_path / "voice-data"
        self.voice_data.mkdir()
        self.voice_pid(VOICE_PID)
        self.voice = Voice(self.voice_data)

    # -- the files ------------------------------------------------------------------------

    def voice_pid(self, pid) -> None:
        path = self.voice_data / "voice.pid"
        if pid is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(f"{pid}\n", encoding="ascii")

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
                  python=self.python, base_python=str(self.base),
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
        "ollama": OK, "ollama-models": NOTE, "telemetry": OK, "realm-holds": OK, "unvoiced": OK,
        "offline-raid": OK, "guild-raids": OK, "night-failures": NOTE,
        "evidence-cycles": OK, "selfie-queue": OK, "realm-screen": OK, "weekly-eval": OK,
        "secret-gate": OK, "offline-days": OK, "training-lab": OK, "gpu-pause": OK}
    assert checks["server-log"].text == "Server.log: cast 111 members, pinned 111 (floor 100)"
    assert checks["mind"].text.startswith(f"mind: pid {MIND_PID} running (control.json names it)")
    assert "bridge_hello" in checks["mind-log"].text
    assert checks["endpoint"].text == ("control endpoint: status answered: bridge connected, "
                                       "cast 111, schema v17")
    assert checks["voice"].text == f"voice service: pid {VOICE_PID}, listening on 7879 and 7880"
    assert checks["ollama-models"].text == f"ollama: 2 loaded: {MODELS.live} (live), other:latest"
    # the endpoint got the key once, at the port control.json names, for status
    assert rig.endpoint.calls == [(CONTROL_PORT, KEY, "status", None)]
    assert rig.fetched == ["http://127.0.0.1:11434/api/ps"]  # Ollama: /api/ps only
    assert rig.processes.snapshots == 1  # one process list for the family and its start


def test_the_command_prints_one_line_each_or_json_and_exits_by_the_fails(rig, capsys):
    argv = ["--root", str(rig.root), "--data-dir", str(rig.data)]
    assert health.main(argv, probes=rig.probes()) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 26 and lines[0].startswith("ok   authserver.exe running")
    assert all(line[:5] in ("ok   ", "note ") for line in lines)
    assert health.main(argv + ["--json"], probes=rig.probes()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and len(out["checks"]) == 26
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


def test_a_server_log_reopened_by_a_config_reload_is_a_note(rig):
    # `.reload config` rebuilds the appenders, and Server.log's `w` mode starts it empty
    rig.server_log([(WORLD_START + 1800, "mod-leeroy: realm on, cast guard on, places 19, items 49, "
                                         "max actions/tick 5")])
    checks = rig.run()
    assert checks["server-log"].status == NOTE
    assert checks["server-log"].text == (
        f"Server.log: reopened at {health._when(WORLD_START + 1800)}, after worldserver started "
        f"at {health._when(WORLD_START)} (a `.reload config` starts it empty), so the cast line "
        "is gone; the control endpoint's cast stands in")
    assert checks["realm-lists"].status == OK


def test_a_log_that_starts_with_worldserver_still_needs_its_cast_line(rig):
    rig.server_log([(WORLD_START + 1, "AzerothCore rev. test (worldserver-daemon)"),
                    (WORLD_START + 1800, "mod-leeroy: realm on, cast guard on, places 19, items 49, "
                                         "max actions/tick 5")])
    assert rig.run()["server-log"].status == FAIL


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
    rig.mind = []  # control.json left by a crash names a pid that is no Python process now
    checks = rig.run()
    assert checks["mind"].status == FAIL and checks["mind"].text == "mind: stopped"
    for name in ("mind-log", "endpoint"):
        assert checks[name].status == NOTE and "the mind is not running" in checks[name].text
    assert rig.endpoint.calls == []
    (rig.data / KEY_FILE).unlink()
    assert rig.run()["mind"].text == "mind: stopped"


def test_control_json_naming_a_mind_of_another_checkout_fails(rig, tmp_path):
    other = tmp_path / "other" / "mind" / ".venv" / "Scripts" / "python.exe"
    rig.mind = [lifecycle.Process(MIND_PID, 20, filetime(MIND_START), str(other),
                                  (str(other), "-m", "leeroy_mind", "run", "--data-dir",
                                   str(rig.data)))]
    checks = rig.run()
    assert checks["mind"].status == FAIL
    assert checks["mind"].text.startswith(f"mind: not running from this checkout, but "
                                          f"control.json names pid {MIND_PID}")


def test_a_mind_without_its_control_json_yet_is_starting_then_running(rig):
    start = NOW - 20
    rig.mind = [lifecycle.Process(p.pid, p.parent, filetime(start) + i, p.exe, p.argv)
                for i, p in enumerate(rig.mind)]
    (rig.data / KEY_FILE).unlink()
    checks = rig.run()
    assert checks["mind"].status == NOTE
    assert checks["mind"].text.startswith(f"mind: pid {MIND_PID} starting (no control.json")
    assert checks["endpoint"].status == NOTE  # still within the grace
    rig.mind = [lifecycle.Process(p.pid, p.parent, filetime(MIND_START) + i, p.exe, p.argv)
                for i, p in enumerate(rig.mind)]
    checks = rig.run()  # long since started: running, and its missing endpoint fails
    assert checks["mind"].status == OK and "(control.json is not its own)" in checks["mind"].text
    assert checks["endpoint"].status == FAIL
    rig.write_toml("\n[control]\nenabled = false\n")
    checks = rig.run()
    assert checks["mind"].status == OK and "(its control endpoint is off)" in checks["mind"].text
    assert checks["endpoint"].status == NOTE


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


def voice_ports(rig, *owners):
    """The TCP table without the voice ports, then (port, pid) listeners."""
    rig.conns = [c for c in rig.conns if c.local_port not in (7879, 7880)] + [
        Conn("127.0.0.1", port, "0.0.0.0", 0, LISTEN, pid) for port, pid in owners]


def test_a_stopped_voice_service_is_a_note(rig):
    rig.voice_pid(None)
    voice_ports(rig)
    checks = rig.run()
    assert checks["voice"].status == NOTE
    assert checks["voice"].text == "voice service: stopped (ports 7879 and 7880 closed)"
    rig.voice_pid(VOICE_PID)  # written at its start, removed at its end: starting, or a crash
    checks = rig.run()
    assert checks["voice"].status == NOTE
    assert checks["voice"].text.endswith(f"voice.pid names pid {VOICE_PID} (starting, or left by "
                                         "a crash)")


def test_a_voice_service_whose_pid_does_not_hold_both_ports_fails(rig):
    voice_ports(rig, (7879, VOICE_PID))
    checks = rig.run()
    assert checks["voice"].status == FAIL
    assert checks["voice"].text == f"voice service: pid {VOICE_PID}, but port 7880 is not listening"
    voice_ports(rig, (7879, VOICE_PID), (7880, 9999))
    assert rig.run()["voice"].text.endswith("port 7880 is held by pid 9999")
    rig.voice_pid(None)
    voice_ports(rig, (7879, VOICE_PID), (7880, VOICE_PID))
    checks = rig.run()
    assert checks["voice"].status == FAIL and "no voice.pid" in checks["voice"].text
    rig.voice_pid("not a pid")
    assert rig.run()["voice"].status == FAIL


def test_the_voice_service_disabled_missing_or_unreadable_is_a_note(rig):
    rig.voice = Voice(rig.voice_data, missing="no voice venv at X")
    assert rig.run()["voice"].status == NOTE
    rig.voice = Voice(rig.voice_data, where=lifecycle.OwnershipError("cannot read the voice "
                                                                      "config"))
    assert rig.run()["voice"].status == NOTE
    rig.voice = Voice(rig.voice_data)
    checks = rig.run(tcp=lambda: None)
    assert checks["voice"].status == NOTE and "no TCP table" in checks["voice"].text
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

def gate(rig, phase="passed", raids=12, verdict_ms=0.4) -> None:
    """meta tg_gate as training.gate.Gate saves it."""
    rig.db("INSERT OR REPLACE INTO meta (key, value) VALUES ('tg_gate', ?)",
           (json.dumps({"phase": phase, "raids": raids, "verdict_ms": verdict_ms}),))


def test_once_the_gate_has_passed_no_pull_in_48_hours_fails(rig):
    gate(rig, "passed")
    rig.db("DELETE FROM tg_pull")
    checks = rig.run()
    assert checks["telemetry"].status == FAIL
    assert checks["telemetry"].text == ("telemetry: on and the overhead gate passed, but no "
                                        "tg_pull in 48 h (last never)")
    rig.pull(NOW - 49 * 3600)
    assert rig.run()["telemetry"].status == FAIL
    rig.pull(NOW - 47 * 3600)
    checks = rig.run()
    assert checks["telemetry"].status == OK and checks["telemetry"].text.endswith("(gate passed)")


def test_in_the_gates_ab_phase_no_pull_in_48_hours_is_a_note(rig):
    rig.db("DELETE FROM tg_pull")
    checks = rig.run()  # no tg_gate yet: phase ab, as `training gate` reads it
    assert checks["telemetry"].status == NOTE
    assert checks["telemetry"].text == (
        "telemetry: overhead gate in phase ab (A/B, 0 offline raids counted): only alternate "
        "bosses of offline raids are recorded, so no tg_pull in 48 h is expected (last never)")
    gate(rig, "ab", raids=3, verdict_ms=None)
    rig.pull(NOW - 49 * 3600)
    checks = rig.run()
    assert checks["telemetry"].status == NOTE and "3 offline raids counted" in checks[
        "telemetry"].text
    rig.pull(NOW - 3600)
    checks = rig.run()
    assert checks["telemetry"].status == OK and checks["telemetry"].text.endswith("(gate ab)")


def test_a_failed_or_unreadable_gate_records_nothing_and_is_a_note(rig):
    rig.db("DELETE FROM tg_pull")
    gate(rig, "failed", verdict_ms=3.5)
    assert rig.run()["telemetry"].status == NOTE
    for value in ("not json", json.dumps({"phase": "passed", "raids": 12}),  # no verdict
                  json.dumps({"phase": "won", "raids": 1, "verdict_ms": None}), "[]"):
        rig.db("INSERT OR REPLACE INTO meta (key, value) VALUES ('tg_gate', ?)", (value,))
        checks = rig.run()  # as training.gate.Gate reads it: failed, never re-armed
        assert checks["telemetry"].status == NOTE
        assert "overhead gate failed (or its state is unreadable)" in checks["telemetry"].text


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
    assert checks["realm-holds"].text.startswith(
        f"realm: 1 body hold (3 members) and 2 unknown roots older than 1 h, since "
        f"{health._when(NOW - 7200)}: guild_grow 1, leave_pvp 1")
    assert f"kind=leave_pvp members=3: realm release leave_pvp-{NOW - 7200}" in checks["realm-holds"].text
    assert "with the mind stopped" in checks["realm-holds"].text


def test_realm_hold_reports_released_members_and_safe_readback_codes(rig):
    rig.action("leave_pvp", "unknown", "settled", [101, 102, 103], NOW - 7200)
    evidence = {"job": {"state": "failed", "released": [101, 103], "readback_guid": 102, "check": "owner"}}
    rig.db("UPDATE realm_action SET evidence=?", (json.dumps(evidence),))
    check = rig.run()["realm-holds"]
    assert check.status == FAIL and "1 body hold (1 members)" in check.text
    assert "members=1:" in check.text and "readback_guid=102 check=owner" in check.text
    assert "supports realm_forget" in check.text
    evidence["job"].update(released=[101, 101], check="private text")
    rig.db("UPDATE realm_action SET evidence=?", (json.dumps(evidence),))
    text = rig.run()["realm-holds"].text
    assert "members=3:" in text and "private text" not in text and "readback_guid=" not in text


def test_an_unknown_leave_native_released_wholly_is_a_note_not_a_fail(rig):
    rig.action("leave_pvp", "unknown", "settled", [101, 102, 103], NOW - 7200)
    evidence = {"job": {"state": "failed", "released": [101, 102, 103], "readback_guid": 103, "check": "owner"}}
    rig.db("UPDATE realm_action SET evidence=?", (json.dumps(evidence),))
    check = rig.run()["realm-holds"]
    assert check.status == NOTE
    assert check.text.startswith("realm: 1 unknown root older than 1 h holds nobody (native released every member)")
    assert f"realm release leave_pvp-{NOW - 7200} --reason reviewed_stale_hold" in check.text
    assert "readback_guid=103 check=owner" in check.text
    # Any other hold or unknown root still fails, the wholly released one named beside it.
    rig.action("rearm", "unknown", "settled", [104], NOW - 5000)
    check = rig.run()["realm-holds"]
    assert check.status == FAIL and "1 body hold (1 members) and 1 unknown root" in check.text
    assert "; 1 more unknown root older than 1 h holds nobody" in check.text
    # Without valid release proof for every member it is held, as before.
    evidence["job"]["released"] = [101, 102]
    rig.db("UPDATE realm_action SET evidence=? WHERE kind='leave_pvp'", (json.dumps(evidence),))
    assert "2 body holds (2 members) and 2 unknown roots" in rig.run()["realm-holds"].text


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


def readonly_check(rig, function, *args, **kwargs):
    path = rig.data / "mind.db"
    before = path.stat().st_mtime_ns
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        result = function(conn, *args, **kwargs)
    assert path.stat().st_mtime_ns == before
    assert "PRIVATE_SENTINEL" not in result.text
    return result


@pytest.mark.parametrize("phase,overdue,status", [
    ("accepted", 1800, OK), ("running", 1801, FAIL), ("accepted", -30, OK),
    ("complete", 99999, OK), ("rejected", 99999, OK),
])
def test_offline_raid_deadline_and_terminal_states(rig, phase, overdue, status):
    receipt = {"raid_id": 42, "phase": phase, "deadline": NOW - overdue}
    rig.db("INSERT INTO meta(key,value) VALUES ('guild:offline:owned',?)", (json.dumps(receipt),))
    result = readonly_check(rig, health._offline_raid, NOW)
    assert result.status == status
    if status == FAIL:
        assert "42" in result.text and "deadline" in result.text and "inspect" in result.text


@pytest.mark.parametrize("phase", ["acquiring", "awaiting_ack", "accepted", "uncertain"])
@pytest.mark.parametrize("overdue,status", [(-30, OK), (1800, OK), (1801, FAIL)])
def test_owned_command_before_local_raid_keeps_its_deadline(rig, phase, overdue, status):
    command = "0123456789abcdef0123456789abcdef"
    receipt = {"id": command, "raid_id": None, "phase": phase, "deadline": NOW - overdue}
    rig.db("INSERT INTO meta(key,value) VALUES ('guild:offline:owned',?)", (json.dumps(receipt),))
    result = readonly_check(rig, health._offline_raid, NOW)
    assert result.status == status and f"command {command}" in result.text
    assert "deadline" in result.text
    if status == FAIL:
        assert "inspect" in result.text


@pytest.mark.parametrize("changes", [
    {"id": None}, {"id": "PRIVATE_SENTINEL"}, {"phase": "running"},
    {"raid_id": False}, {"deadline": None}, {"deadline": True},
    {"deadline": float("nan")}, {"deadline": float("inf")},
])
def test_pre_session_receipt_still_requires_command_identity_and_finite_deadline(rig, changes):
    receipt = {"id": "0123456789abcdef0123456789abcdef", "raid_id": None,
               "phase": "awaiting_ack", "deadline": NOW + 60, **changes}
    rig.db("INSERT INTO meta(key,value) VALUES ('guild:offline:owned',?)", (json.dumps(receipt),))
    result = readonly_check(rig, health._offline_raid, NOW)
    assert result.status == FAIL and "invalid owned receipt" in result.text


@pytest.mark.parametrize("receipt", ['PRIVATE_SENTINEL', '[]', '{"phase":"running"}',
                                      '{"phase":"accepted","raid_id":42,"deadline":NaN}'])
def test_invalid_offline_raid_receipt_is_reported_without_contents(rig, receipt):
    rig.db("INSERT INTO meta(key,value) VALUES ('guild:offline:owned',?)", (receipt,))
    assert readonly_check(rig, health._offline_raid, NOW).status == FAIL


def test_guild_raid_open_six_hour_boundary_and_ended_runs(rig):
    sql = ("INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,lead_guid,"
           "tank_guid,loot_guid) VALUES (?,?,?,?,10,0,1,101,102,103)")
    rig.db(sql, (41, NOW - 6 * 3600, None, "synthetic"))
    rig.db(sql, (42, NOW - 9 * 3600, NOW - 7 * 3600, "synthetic"))
    assert readonly_check(rig, health._guild_raids, NOW).status == OK
    rig.db(sql, (43, NOW - 6 * 3600 - 1, None, "synthetic"))
    result = readonly_check(rig, health._guild_raids, NOW)
    assert result.status == FAIL and "43 open 6 h" in result.text
    assert "42 open" not in result.text


def finished_night(rig, error, *, night="2026-11-05", started=NOW - 1000, finished=NOW - 100):
    rig.db("INSERT INTO night_run(night,window_start,window_end,status,started,finished,error) "
           "VALUES (?,?,?,'done',?,?,?)", (night, started, finished, started, finished, error))


@pytest.mark.parametrize("counts,status", [({}, OK), ({"critic": 1}, NOTE),
                                          ({"critic": 9}, NOTE), ({"critic": 9, "gossip": 1}, FAIL)])
def test_stored_night_failures_thresholds_and_error_code_only(rig, counts, status):
    finished_night(rig, json.dumps(counts))
    rig.mind_log([record(NOW - 500, "nightly_unit_failed", night="2026-11-05", stage="critic",
                         error="ValueError", message="PRIVATE_SENTINEL", unit="PRIVATE_SENTINEL")])
    result = readonly_check(rig, health._night_failures, rig.data, lambda p: open(p, "rb"))
    assert result.status == status and f"{sum(counts.values())} failed units (stored)" in result.text
    if counts:
        assert "top error=ValueError" in result.text and "critic=" in result.text


def test_latest_finished_night_and_log_fallback_include_rotation(rig):
    finished_night(rig, '{"critic":50}', night="2026-11-04", finished=NOW - 5000)
    finished_night(rig, None)
    rig.mind_log([record(NOW - 400, "nightly_unit_failed", night="2026-11-05", stage="gossip",
                         error="ProvenanceError", message="PRIVATE_SENTINEL"),
                  record(NOW - 99, "nightly_unit_failed", night="2026-11-05", stage="gossip"),
                  record(NOW - 400, "nightly_unit_failed", night="other", stage="gossip")])
    rotated = rig.data / "logs" / ("mind.log." + time.strftime("%Y-%m-%d", time.localtime(NOW - 1000)))
    rotated.write_text(record(NOW - 1001, "mind_started") + "\n" +
                       record(NOW - 500, "nightly_unit_failed", night="2026-11-05",
                              stage="critic", error="ProvenanceError") + "\n", encoding="utf-8")
    result = readonly_check(rig, health._night_failures, rig.data, lambda p: open(p, "rb"))
    assert result.status == NOTE and "2 failed units (logs)" in result.text
    assert "critic=1, gossip=1" in result.text and "ProvenanceError (2 logged)" in result.text


@pytest.mark.parametrize("age_days", [0, 30])
@pytest.mark.parametrize("records", [[], [record(NOW - 1, "mind_started")]])
def test_empty_or_newer_logs_cannot_clear_a_finished_night(rig, age_days, records):
    finished_night(rig, None, started=NOW - 1000 - age_days * 86400,
                   finished=NOW - 100 - age_days * 86400)
    rig.mind_log(records)
    result = readonly_check(rig, health._night_failures, rig.data, lambda p: open(p, "rb"))
    assert result.status == NOTE and "historical log coverage incomplete" in result.text
    assert "0 observed failed units (logs)" in result.text


@pytest.mark.parametrize("bounds", [(True, False), (False, True), (True, True)])
def test_log_only_zero_requires_both_interval_bounds(rig, bounds):
    finished_night(rig, None)
    records = [record(NOW - 500, "nightly_stage", night="2026-11-05")]
    if bounds[0]:
        records.insert(0, record(NOW - 1001, "mind_started"))
    if bounds[1]:
        records.append(record(NOW - 99, "nightly_done", night="2026-11-05"))
    rig.mind_log(records)
    result = readonly_check(rig, health._night_failures, rig.data, lambda p: open(p, "rb"))
    assert result.status == (OK if all(bounds) else NOTE)
    assert ("historical log coverage incomplete" in result.text) == (not all(bounds))


def test_missing_rotation_within_the_night_is_an_incomplete_observation(rig):
    started, finished = NOW - 3 * 86400, NOW - 100
    finished_night(rig, None, started=started, finished=finished)
    rig.mind_log([record(finished + 1, "nightly_done", night="2026-11-05")])
    old_name = "mind.log." + time.strftime("%Y-%m-%d", time.localtime(started))
    rig.mind_log([record(started - 1, "mind_started")], name=old_name)
    result = readonly_check(rig, health._night_failures, rig.data, lambda p: open(p, "rb"))
    assert result.status == NOTE and "historical log coverage incomplete" in result.text


def test_retained_daily_rotations_can_cover_a_resumed_night(rig):
    started, finished = NOW - 3 * 86400, NOW - 100
    finished_night(rig, None, started=started, finished=finished)
    for at in (started - 1, started + 86400, started + 2 * 86400):
        name = "mind.log." + time.strftime("%Y-%m-%d", time.localtime(at))
        rig.mind_log([record(at, "mind_started")], name=name)
    rig.mind_log([record(finished + 1, "nightly_done", night="2026-11-05")])
    result = readonly_check(rig, health._night_failures, rig.data, lambda p: open(p, "rb"))
    assert result.status == OK and "0 failed units (logs)" in result.text
    assert "incomplete" not in result.text


def test_unreadable_record_inside_covered_logs_is_not_a_clean_night(rig):
    finished_night(rig, None)
    rig.mind_log([record(NOW - 1001, "mind_started"), "PRIVATE_SENTINEL",
                  record(NOW - 99, "nightly_done", night="2026-11-05")])
    result = readonly_check(rig, health._night_failures, rig.data, lambda p: open(p, "rb"))
    assert result.status == NOTE and "log scan incomplete" in result.text
    assert "0 observed failed units (logs)" in result.text


def test_incomplete_log_counts_can_still_fail_the_night(rig):
    finished_night(rig, None)
    rig.mind_log([record(NOW - 500, "nightly_unit_failed", night="2026-11-05",
                         stage="critic", error="ValueError", message="PRIVATE_SENTINEL")
                  for _ in range(10)])
    result = readonly_check(rig, health._night_failures, rig.data, lambda p: open(p, "rb"))
    assert result.status == FAIL and "10 observed failed units (logs)" in result.text
    assert "historical log coverage incomplete" in result.text


def test_log_budget_is_an_incomplete_observation(rig, monkeypatch):
    finished_night(rig, None)
    monkeypatch.setattr(health, "NIGHT_LOG_BYTES", 10)
    result = readonly_check(rig, health._night_failures, rig.data, lambda p: open(p, "rb"))
    assert result.status == NOTE and "incomplete" in result.text


def insert_health_memory(rig, ident, refs):
    rig.db("INSERT INTO memory(id,owner,kind,text,t_event,t_created,t_accessed,importance,valence,"
           "source,evidence) VALUES (?,101,'event','PRIVATE_SENTINEL',1,1,1,5,0,'game',?)",
           (ident, json.dumps(refs)))


@pytest.mark.parametrize("count,status", [(0, OK), (1, NOTE), (49, NOTE), (50, FAIL)])
def test_evidence_cycle_thresholds_count_memories(rig, count, status):
    for ident in range(1, count + 1):
        insert_health_memory(rig, ident, [f"memory:{ident}"])
    result = readonly_check(rig, health._evidence_cycles)
    assert result.status == status and f"{count} cyclic memories" in result.text


def test_long_cycle_shared_tail_and_acyclic_diamond(rig):
    for ident, refs in {1: ["memory:2"], 2: ["memory:3"], 3: ["memory:0002"],
                        4: ["memory:2"], 5: ["memory:6", "memory:7"],
                        6: ["memory:8"], 7: ["memory:8"], 8: ["owner:synthetic"]}.items():
        insert_health_memory(rig, ident, refs)
    result = readonly_check(rig, health._evidence_cycles)
    assert result.status == NOTE and "4 cyclic memories among 8 checked" in result.text


def test_cycle_uses_shared_chat_recall_edges(rig):
    insert_health_memory(rig, 1, ["chat:1"])
    rig.db("INSERT INTO chat_line(id,ts,channel,from_guid,from_name,to_guid,text,origin) "
           "VALUES (1,1000,'guild',101,'Synthetic',0,'PRIVATE_SENTINEL','mind')")
    rig.db("INSERT INTO utterance(ts,guid,channel,kind,text,memory_ids,to_guid) "
           "VALUES (1000,101,'guild','open','PRIVATE_SENTINEL','[1]',0)")
    result = readonly_check(rig, health._evidence_cycles)
    assert result.status == NOTE and "1 cyclic memories" in result.text


def test_cycle_budget_or_invalid_evidence_never_claims_complete(rig):
    insert_health_memory(rig, 1, ["memory:2"])
    insert_health_memory(rig, 2, ["memory:1"])
    for options in ({"row_budget": 1}, {"seconds": 0}):
        result = readonly_check(rig, health._evidence_cycles, **options)
        assert result.status == NOTE and "incomplete (budget" in result.text
    rig.db("UPDATE memory SET evidence='PRIVATE_SENTINEL' WHERE id=2")
    result = readonly_check(rig, health._evidence_cycles)
    assert result.status == NOTE and "invalid provenance" in result.text


def test_cycle_counts_match_independent_transitive_closure():
    rng = random.Random(8042)
    for _ in range(40):
        size = 12
        graph = [{j for j in range(size) if rng.random() < 0.13} for _ in range(size)]
        reach = [set(children) for children in graph]
        for via in range(size):
            for root in range(size):
                if via in reach[root]:
                    reach[root].update(reach[via])
        cycle_nodes = {i for i in range(size) if i in reach[i]}
        expected = sum(bool(children & cycle_nodes) for children in reach)
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE memory(id INTEGER PRIMARY KEY,evidence TEXT)")
            conn.executemany("INSERT INTO memory VALUES (?,?)", [
                (i + 1, json.dumps([f"memory:{j + 1}" for j in children]))
                for i, children in enumerate(graph)])
            conn.execute("PRAGMA query_only=ON")
            result = health._evidence_cycles(conn)
            assert f"{expected} cyclic memories among {size} checked" in result.text


def test_cycle_walk_handles_a_deep_chain_without_recursion():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE memory(id INTEGER PRIMARY KEY,evidence TEXT)")
        conn.executemany("INSERT INTO memory VALUES (?,?)", [
            (i, json.dumps([f"memory:{i + 1}" if i < 1500 else "memory:1499"]))
            for i in range(1, 1501)])
        result = health._evidence_cycles(conn)
        assert result.status == FAIL and "1500 cyclic memories among 1500 checked" in result.text


def test_selfie_queued_age_and_ready_sets(rig):
    rig.db("INSERT INTO selfie_set(guid,set_no,reason,status,requested) VALUES (101,1,'first','queued',?)",
           (NOW - 30 * 3600,))
    assert readonly_check(rig, health._selfie_queue, NOW).status == OK
    rig.db("UPDATE selfie_set SET requested=?", (NOW - 30 * 3600 - 1,))
    result = readonly_check(rig, health._selfie_queue, NOW)
    assert result.status == NOTE and "1 sets older than 30 h" in result.text
    rig.db("UPDATE selfie_set SET status='ready', rendered=?", (NOW,))
    assert readonly_check(rig, health._selfie_queue, NOW).status == OK


def test_new_checks_on_an_empty_old_schema(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)):
        pass
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        for fn, args in [(health._offline_raid, (NOW,)), (health._guild_raids, (NOW,)),
                         (health._night_failures, (tmp_path, lambda p: open(p, "rb"))),
                         (health._evidence_cycles, ()), (health._selfie_queue, (NOW,)),
                         (health._weekly_eval, ()), (health._secret_gate, ()),
                         (health._offline_days, (NOW,))]:
            result = fn(conn, *args)
            assert result.status == NOTE and "not set up" in result.text


def realm_on(rig, blocklist="blockedword\n", guild={"guild": 50, "name": "Testguild", "at": 1.0}):
    rig.write_toml("\n[realm]\nenabled = true\n")
    if blocklist is not None:
        (rig.data / "realm").mkdir(exist_ok=True)
        (rig.data / "realm" / "blocklist.txt").write_text(blocklist, encoding="utf-8")
    if guild is not None:
        rig.db("INSERT INTO meta(key, value) VALUES ('realm:guild_name', ?)", (json.dumps(guild),))


def test_the_realm_screen_is_checked_only_while_the_realm_is_on(rig):
    assert readonly_check(rig, health._realm_screen, rig.data).text == "realm screen: the Realm is off in mind.toml"
    realm_on(rig)
    result = readonly_check(rig, health._realm_screen, rig.data)
    assert result.status == OK and result.text == "realm screen: ready (blocklist and guild name)"
    rig.write_toml("\n[realm]\nenabled = 3\n")
    assert readonly_check(rig, health._realm_screen, rig.data).status == NOTE


@pytest.mark.parametrize("blocklist, guild, says", [
    (None, {"guild": 50, "name": "Testguild"}, ["blocklist.txt is missing"]),
    ("# only a comment\n\n", {"guild": 50, "name": "Testguild"}, ["blocklist.txt has no entry"]),
    ("word\n", None, ["guild name is not cached"]),
    ("word\n", {"guild": 50, "name": "  "}, ["guild name is not cached"]),
    ("word\n", {"guild": 0, "name": "Testguild"}, ["guild name is not cached"]),
    (None, None, ["blocklist.txt is missing", "guild name is not cached"])])
def test_a_realm_screen_that_cannot_be_ready_fails(rig, blocklist, guild, says):
    realm_on(rig, blocklist=blocklist, guild=guild)
    result = readonly_check(rig, health._realm_screen, rig.data)
    assert result.status == FAIL and result.text.startswith("realm screen: the Realm is on in mind.toml")
    assert all(part in result.text for part in says)
    assert rig.run()["realm-screen"].status == FAIL


def eval_result(rig, week, complete):
    rig.db("INSERT INTO meta(key, value) VALUES (?, ?)", (f"nightly:eval-result:{week}", json.dumps(
        {"complete": complete, "status": "complete" if complete else "partial", "requested_llm": True})))


def test_the_newest_weekly_evaluation_incomplete_is_a_note(rig):
    eval_result(rig, "2026-W44", True)
    assert readonly_check(rig, health._weekly_eval).text == "weekly evaluation 2026-W44: complete"
    eval_result(rig, "2026-W45", False)
    result = readonly_check(rig, health._weekly_eval)
    assert result.status == NOTE and result.text.startswith("weekly evaluation 2026-W45: incomplete")
    rig.db("UPDATE meta SET value='not json' WHERE key='nightly:eval-result:2026-W45'")
    assert readonly_check(rig, health._weekly_eval).status == NOTE
    eval_result(rig, "2026-W46", True)
    assert readonly_check(rig, health._weekly_eval).status == OK


def test_an_empty_probe_run_is_a_note_only_while_a_secret_waits(rig):
    assert readonly_check(rig, health._secret_gate).text == (
        "secret probe: none run yet, and no secret is waiting on it")
    insert_health_memory(rig, 1, [])
    rig.db("UPDATE memory SET scope='secret' WHERE id=1")
    result = readonly_check(rig, health._secret_gate)
    assert result.status == NOTE and "probe_run is empty" in result.text
    rig.db("INSERT INTO probe_run(ts, model, n, leaks, rate, details) VALUES (?, 'live', 36, 0, 0, '[]')",
           (NOW - 86400,))
    result = readonly_check(rig, health._secret_gate)
    assert result.status == OK and result.text.startswith("secret probe: 1 run, newest ")


def offline_day(rig, day, **state):
    rig.db("INSERT INTO meta(key, value) VALUES (?, ?)", (f"guild:offline:day:{day}", json.dumps(
        {"attempts": 8, "next_try": 0, "blocked": False} | state)))


def test_offline_raids_blocked_on_each_of_the_last_three_days_is_a_note(rig):
    from datetime import timedelta
    today = datetime.fromtimestamp(NOW).date()
    days = [(today - timedelta(days=n)).isoformat() for n in (1, 2, 3)]
    offline_day(rig, days[0], blocked=True, refused="in_instance:363")
    offline_day(rig, days[1], blocked=True, failed="setup:grouping:timeout")
    assert readonly_check(rig, health._offline_days, NOW).status == OK  # two days are not three
    offline_day(rig, days[2], blocked=True, refused="bad code with spaces")
    result = readonly_check(rig, health._offline_days, NOW)
    assert result.status == NOTE
    assert (f"{days[0]} in_instance:363, {days[1]} setup:grouping:timeout, {days[2]} ?"
            in result.text)
    offline_day(rig, today.isoformat(), blocked=False)  # today is not a whole day yet
    assert readonly_check(rig, health._offline_days, NOW).status == NOTE
    rig.db("UPDATE meta SET value=? WHERE key=?", (json.dumps({"blocked": False, "accepted": 1}),
                                                   f"guild:offline:day:{days[1]}"))
    assert readonly_check(rig, health._offline_days, NOW).status == OK


def test_new_check_json_matches_text_and_never_writes(rig, capsys):
    insert_health_memory(rig, 1, ["memory:1"])
    before = (rig.data / "mind.db").stat().st_mtime_ns
    args = ["--root", str(rig.root), "--data-dir", str(rig.data), "--json"]
    assert health.main(args, probes=rig.probes()) == 0
    output = json.loads(capsys.readouterr().out)
    expected = rig.run()
    for name in ("offline-raid", "guild-raids", "night-failures", "evidence-cycles", "selfie-queue"):
        actual, = (c for c in output["checks"] if c["name"] == name)
        assert actual == expected[name].as_dict()
        assert "PRIVATE_SENTINEL" not in actual["text"]
    assert (rig.data / "mind.db").stat().st_mtime_ns == before


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


def test_a_partial_clear_toasts_again_only_for_what_comes_back(tmp_path):
    shown = []

    def show(title, body):
        shown.append(body)
        return True
    assert alert(checks_failing("A", "B"), tmp_path, show=show) is True
    assert shown == ["A, B"]  # one toast for both
    assert alert(checks_failing("A"), tmp_path, show=show) is False
    assert shown == ["A, B"]  # B cleared: no toast for A, still failing
    assert alert(checks_failing("A", "B"), tmp_path, show=show) is True
    assert shown == ["A, B", "A, B"]  # B is back: a toast for it
    assert alert(checks_failing("A", "B"), tmp_path, show=show) is False
    assert len(shown) == 2


def test_a_state_file_that_cannot_be_written_logs_one_line(tmp_path, caplog):
    (tmp_path / health.STATE_FILE).mkdir()  # a folder where the file goes: every write fails
    shown = []
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        for _ in range(2):
            caplog.clear()
            alert(checks_failing("worldserver"), tmp_path,
                  show=lambda t, b: shown.append(b) or True)
            records = [r for r in caplog.records if r.name == "leeroy_mind"]
            assert len(records) == 1 and records[0].levelno == logging.WARNING
            assert records[0].getMessage().startswith("health_state_unwritten: error=")
            assert "worldserver" not in records[0].getMessage()
    assert shown == ["worldserver", "worldserver"]  # why the same toast came twice


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


def test_it_never_takes_mind_lock_or_voice_lock(rig, monkeypatch):
    """Every lock-taking probe refuses: the mind's lock probe (app.instance_lock_held, the
    InstanceLock it acquires, lifecycle.mind_action's status that calls it) and the voice
    service's (VoiceControl.held and status, which run `leeroy_voice held`). With a real
    VoiceControl whose only command may be `where`, every state still comes out."""
    from leeroy_mind import app

    def refuse(*args, **kwargs):
        raise AssertionError("a lock-taking probe was called")
    monkeypatch.setattr(app, "instance_lock_held", refuse)
    monkeypatch.setattr(app.InstanceLock, "acquire", refuse)
    monkeypatch.setattr(lifecycle, "mind_action", refuse)
    monkeypatch.setattr(lifecycle.VoiceControl, "held", refuse)
    monkeypatch.setattr(lifecycle.VoiceControl, "status", refuse)
    asked = []

    def run(argv, **kwargs):
        asked.append(tuple(argv[1:]))
        if tuple(argv[1:]) != ("-m", "leeroy_voice", "where"):
            raise AssertionError(f"voice command {argv[1:]} may take voice.lock")
        return subprocess.CompletedProcess(argv, 0, f"data_dir={rig.voice_data}\nmind_port=7879"
                                                    "\nui_port=7880\n", "")
    scripts = rig.root / "voice" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    for exe in ("python.exe", "pythonw.exe"):
        (scripts / exe).write_bytes(b"")
    voice = lifecycle.VoiceControl(rig.root, processes=Processes(rig.mind), run=run)
    assert "locked" not in {f.name for f in dataclasses.fields(Probes)}
    checks = rig.run(voice=voice)  # all running
    assert (checks["mind"].status, checks["voice"].status) == (OK, OK)
    rig.voice_pid(None)
    voice_ports(rig)
    rig.mind = []
    checks = rig.run(voice=voice)  # both stopped
    assert (checks["mind"].status, checks["voice"].status) == (FAIL, NOTE)
    assert set(asked) == {("-m", "leeroy_voice", "where")}


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


# -- the training lab ----------------------------------------------------------------------------

def lab_record(rig, **changes):
    owned = {"id": "a" * 32, "phase": "blocked", "halt": "native_cleanup_unproved", "night": "2026-11-04",
             "guids": [1924, 2984], "started": NOW - 2 * 86400, "halted_at": NOW - 86401,
             "current": {"id": "b" * 32}} | changes
    rig.db("INSERT INTO meta(key,value) VALUES ('tg:lab:owned',?) "
           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(owned),))


def test_a_lab_blocked_for_longer_than_a_night_fails_with_the_recovery_command(rig):
    assert readonly_check(rig, health._training_lab, NOW).status == OK
    lab_record(rig)
    result = readonly_check(rig, health._training_lab, NOW)
    assert result.status == FAIL
    assert "halt=native_cleanup_unproved" in result.text and "night 2026-11-04" in result.text
    assert f"trial {'b' * 32}" in result.text and "2 members held" in result.text
    assert "with the mind stopped: training lab-clear" in result.text
    assert rig.run()["training-lab"].status == FAIL


def test_a_fresh_block_is_a_note_and_a_finished_lab_is_ok(rig):
    lab_record(rig, halted_at=NOW - 86400)
    assert readonly_check(rig, health._training_lab, NOW).status == NOTE
    lab_record(rig, halted_at=None)  # An older record dates from its start.
    result = readonly_check(rig, health._training_lab, NOW)
    assert result.status == FAIL and "blocked since" in result.text
    lab_record(rig, phase="complete")
    assert readonly_check(rig, health._training_lab, NOW).status == OK


@pytest.mark.parametrize("changes", [{"halted_at": "PRIVATE_SENTINEL", "started": "PRIVATE_SENTINEL"},
                                     {"halt": "PRIVATE_SENTINEL; rm", "night": "PRIVATE_SENTINEL"}])
def test_an_odd_lab_record_never_echoes_its_contents(rig, changes):
    lab_record(rig, **changes)
    result = readonly_check(rig, health._training_lab, NOW)
    assert result.status == FAIL


def test_an_unreadable_lab_record_fails_without_contents(rig):
    rig.db("INSERT INTO meta(key,value) VALUES ('tg:lab:owned','PRIVATE_SENTINEL')")
    result = readonly_check(rig, health._training_lab, NOW)
    assert result.status == FAIL and "invalid lab record" in result.text
