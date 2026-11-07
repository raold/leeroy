"""The Dojo tab: the dojo's own files read read-only (dojoview), its route and gates. Synthetic
layouts and files under tmp_path only; the live dojo-data and dojo-runtime are never read."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from console.rig import http, make_app, running
from leeroy_mind.console import dojoview
from leeroy_mind.console.netinfo import LISTEN, Conn

WORLD = 18085


def put(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def make_layout(tmp_path, **over):
    data, runtime = tmp_path / "dojo-data", tmp_path / "rt"
    data.mkdir(exist_ok=True)
    runtime.mkdir(exist_ok=True)
    layout = {"format": 1, "marker": dojoview.MARKER, "instance": "dojo-01",
              "roots": {"data": str(data), "runtime": str(runtime), "build": str(tmp_path / "b")},
              "provenance": str(data / "manifests" / "provenance.json"),
              "database": {"password": "SECRETDB"}, "ports": {"world": WORLD}}
    layout.update(over)
    return put(tmp_path / "layout.json", layout), data, runtime


def build(data, stamp, variant="training", exit_code=0, problems=(), verified=None):
    put(data / "builds" / f"build-{stamp}-{variant}.json", {
        "variant": variant, "created_utc": "2026-11-05T10:00:00Z", "exit_code": exit_code,
        "compiler_errors": 0, "problems": list(problems),
        "verified": verified if verified is not None else {"a": True, "b": True},
        "core": "c0ffee1234567890", "playerbots": "badcafe1234567890"})


def provenance(data, root):
    base = {k: {"path": str(root / k), "head": "abc1234567"} for k in ("install_root", "core", "playerbots")}
    put(data / "manifests" / "provenance.json", {"created_utc": "2026-11-01T00:00:00Z", "baseline": base})


def run_dir(data, stamp, kind, report=None, name=None, md=None):
    d = data / "runs" / f"{stamp}-{kind}"
    d.mkdir(parents=True, exist_ok=True)
    if report is not None:
        put(d / (name or dojoview.report_name(kind)), report)
    if md is not None:
        (d / "G1.md").write_bytes(md.encode("utf-8"))
    return d


def episode(name, ok=True):
    return {"name": name, "policies": ["a", "b"], "outcome": {"reason": "death", "winner": 0},
            "replay": {"ok": ok, "usable": ok}}


def view(layout, conns=None, git=lambda *a: None, **kw):
    return dojoview.dojo_view(layout, conns=conns, git=git, cache={}, **kw)


def test_no_layout_is_no_dojo_set_up(tmp_path):
    v = view(tmp_path / "nope.json")
    assert v["setup"] is False and v["why"] == f"no layout at {tmp_path / 'nope.json'}"
    assert dojoview.dojo_view(None, conns=None, cache={})["setup"] is False

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path), dojo_layout=tmp_path / "nope.json")
        await app.start()
        try:
            r = await http(app, "GET", "/api/dojo")
            assert r.status == 200 and r.json()["setup"] is False
            assert (await http(app, "GET", "/api/dojo", key=False)).status == 403
        finally:
            await app.close()
    asyncio.run(go())


def test_a_layout_that_is_not_the_dojos_is_refused(tmp_path):
    layout, data, runtime = make_layout(tmp_path)
    assert view(layout)["setup"] is True
    bad = "that file is not a dojo training layout"
    cases = [{"marker": "something-else"}, {"format": 2},
             {"roots": {"data": "relative/data", "runtime": str(runtime)}},
             {"roots": {"data": str(data), "runtime": "rt"}},
             {"provenance": str(tmp_path / "elsewhere" / "provenance.json")},
             {"provenance": str(data / ".." / "x.json")}, {"ports": {"world": "x"}}]
    for over in cases:
        layout, _, _ = make_layout(tmp_path, **over)
        assert view(layout) == {"setup": False, "why": bad}, over
    put(tmp_path / "layout.json", "[1, 2")
    assert view(tmp_path / "layout.json")["why"] == bad
    put(tmp_path / "layout.json", "x" * (dojoview.MAX_JSON + 1))
    assert view(tmp_path / "layout.json")["why"] == bad
    layout, _, _ = make_layout(tmp_path, roots={"data": str(tmp_path / "gone"), "runtime": str(runtime)},
                               provenance=str(tmp_path / "gone" / "p.json"))
    assert view(layout) == {"setup": False, "why": "the layout's data root is missing"}


def test_the_newest_build_and_the_runs(tmp_path):
    layout, data, _ = make_layout(tmp_path)
    build(data, "20261101_100000", "disabled", exit_code=1)
    build(data, "20261105_100000")
    run_dir(data, "20261105_181500", "g1", {"gate": "G1", "ok": True, "episodes": [episode("e1"), episode("e2", False)]})
    run_dir(data, "20261105_175300", "g1", {"gate": "G1", "ok": False, "episodes": []})
    run_dir(data, "20261104_090000", "realm-acceptance", {"ok": True})
    v = view(layout)
    b = v["build"]
    assert b["variant"] == "training" and b["exit_code"] == 0 and b["created_utc"] == "2026-11-05T10:00:00Z"
    assert b["verified"] == {"passed": 2, "total": 2} and b["ok"] is True
    assert b["core"] == "c0ffee1234567890" and b["compiler_errors"] == 0
    assert [(r["stamp"], r["kind"], r["ok"]) for r in v["runs"]] == [
        ("20261105_181500", "g1", True), ("20261105_175300", "g1", False),
        ("20261104_090000", "realm-acceptance", True)]
    eps = v["runs"][0]["episodes"]
    assert [(e["name"], e["policies"], e["outcome"], e["winner"], e["replay_ok"], e["usable"]) for e in eps] == [
        ("e1", ["a", "b"], "death", 0, True, True), ("e2", ["a", "b"], "death", 0, False, False)]
    assert v["later"] == {"policies": "not yet", "curves": "not yet", "live_adapter": "not yet"}
    assert "SECRETDB" not in json.dumps(v)


def test_runs_are_capped(tmp_path):
    layout, data, _ = make_layout(tmp_path)
    for i in range(dojoview.RUNS + 5):
        run_dir(data, f"20261001_{i:06d}", "realm-acceptance", {"ok": True})
    assert len(view(layout)["runs"]) == dojoview.RUNS


def test_gates_g0_to_g5(tmp_path):
    layout, data, runtime = make_layout(tmp_path)
    provenance(data, tmp_path)
    build(data, "20261105_100000")
    run_dir(data, "20261105_181500", "g1", {"gate": "G1", "ok": True, "episodes": [],
                                            "inputs": {"scenario": {"id": "arena-1", "sha256": "ab" * 32}}},
            md="# G1 report\nall good")
    run_dir(data, "20261105_170000", "realm-acceptance", {"ok": True})
    run_dir(data, "20261105_171000", "control-acceptance", {"ok": True})
    run_dir(data, "20261105_172000", "reset-acceptance", {"ok": False})
    run_dir(data, "20261106_090000", "g2", {"gate": "G2", "ok": False})
    run_dir(data, "20261106_100000", "g3", {"gate": "G9", "ok": True})
    v = view(layout)
    gates = {g["gate"]: g for g in v["gates"]}
    assert list(gates) == list(dojoview.GATES)
    assert gates["G0"]["state"] == "evidence"
    assert gates["G1"]["state"] == "passed" and gates["G1"]["run"] == "20261105_181500"
    for word in ("realm", "control", "reset"):
        assert word in gates["G1"]["detail"]
    assert "20261105_172000" in gates["G1"]["detail"] and "failed" in gates["G1"]["detail"]
    assert gates["G2"]["state"] == "failed" and gates["G2"]["run"] == "20261106_090000"
    assert gates["G3"]["state"] == "not yet"
    assert gates["G4"]["state"] == gates["G5"]["state"] == "not yet"
    assert v["scenario"] == {"id": "arena-1", "sha256": "ab" * 32}
    assert v["g1_md"] == "# G1 report\nall good"


def test_g0_needs_a_clean_build_and_the_manifest(tmp_path):
    layout, data, _ = make_layout(tmp_path)
    build(data, "20261105_100000")
    assert view(layout)["gates"][0]["state"] == "not yet"
    provenance(data, tmp_path)
    assert view(layout)["gates"][0]["state"] == "evidence"
    build(data, "20261106_100000", problems=["x"])
    assert view(layout)["gates"][0]["state"] == "not yet"
    build(data, "20261107_100000", verified={"a": True, "b": False})
    assert view(layout)["gates"][0]["state"] == "not yet"


def test_running_needs_the_receipts_pid_listening(tmp_path):
    layout, data, runtime = make_layout(tmp_path)
    put(runtime / "run" / "worldserver.pid", "4242\n")
    put(runtime / "run" / "dojo-endpoint.json", {"pid": 4242, "host": "127.0.0.1", "port": 19999,
                                                   "scenario": "arena-1", "scenario_sha256": "cd" * 32})
    up = [Conn("0.0.0.0", WORLD, "0.0.0.0", 0, LISTEN, 4242), Conn("127.0.0.1", 19999, "0.0.0.0", 0, LISTEN, 4242)]
    r = view(layout, up)["realm"]
    assert (r["state"], r["pid"], r["port"]) == ("running", 4242, WORLD)
    assert r["endpoint"] == {"pid": 4242, "port": 19999, "scenario": "arena-1", "sha256": "cd" * 32, "up": True}
    put(runtime / "run" / "worldserver.pid", "99")
    r = view(layout, up)["realm"]
    assert r["state"] == "stopped" and r["pid"] == 99
    assert view(layout, [])["realm"]["state"] == "stopped"
    assert view(layout, None)["realm"]["state"] == "unknown"
    other = [Conn("0.0.0.0", WORLD + 1, "0.0.0.0", 0, LISTEN, 99)]
    assert view(layout, other)["realm"]["state"] == "stopped"


def test_partial_and_hostile_files_are_rows_not_errors(tmp_path):
    layout, data, runtime = make_layout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    put(outside / "secret.json", {"ok": True, "gate": "G2", "mark": "OUTSIDE"})
    run_dir(data, "20261105_100000", "g1")  # no report
    d = run_dir(data, "20261105_110000", "realm-acceptance")
    (d / "acceptance.json").write_text('{"ok": tru', encoding="utf-8")  # truncated
    d = run_dir(data, "20261105_120000", "control-acceptance")
    (d / "report.json").write_bytes(b"x" * (dojoview.MAX_JSON + 1))
    d = run_dir(data, "20261105_130000", "reset-acceptance")
    linked = True
    try:
        os.symlink(outside / "secret.json", d / "report.json")
    except (OSError, NotImplementedError):
        linked = False
    run_dir(data, "20261105_140000", "g2", [1, 2])  # not an object
    (data / "runs" / "20261105_150000-g3" / "g3-report.json").mkdir(parents=True)  # a directory
    run_dir(data, "20261105_160000", "g1", {"gate": "G1", "ok": True, "episodes": []},
            md="x" * (dojoview.MAX_TEXT + 1))
    put(data / "manifests" / "provenance.json", "{not json")
    v = view(layout)
    rows = {r["stamp"]: r for r in v["runs"]}
    assert rows["20261105_100000"]["why"] == "no report" and rows["20261105_100000"]["ok"] is None
    assert rows["20261105_110000"]["why"] == "unreadable"
    assert rows["20261105_120000"]["why"] == "too large to read"
    if linked:
        assert rows["20261105_130000"]["why"] == "unreadable" and rows["20261105_130000"]["ok"] is None
    assert rows["20261105_140000"]["why"] == "unreadable"
    assert rows["20261105_150000"]["why"] == "unreadable"
    assert v["g1_md"] is None and v["g1_md_why"] == "too large to read"
    assert v["provenance"] is None
    assert "OUTSIDE" not in json.dumps(v)
    gates = {g["gate"]: g for g in v["gates"]}
    assert gates["G2"]["state"] == "not yet" and gates["G0"]["state"] == "not yet"


def test_a_run_folder_linked_out_of_the_data_root_is_never_read(tmp_path):
    layout, data, _ = make_layout(tmp_path)
    outside = tmp_path / "outside"
    put(outside / "20261105_100000-g1" / "g1-report.json", {"gate": "G1", "ok": True, "episodes": []})
    (data / "runs").mkdir()
    try:
        os.symlink(outside / "20261105_100000-g1", data / "runs" / "20261105_100000-g1", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("the OS refuses symlinks")
    v = view(layout)
    assert v["runs"] == [] and v["gates"][1]["state"] == "not yet"


def test_baselines_moved_or_not(tmp_path):
    layout, data, _ = make_layout(tmp_path)
    provenance(data, tmp_path)
    for k in ("install_root", "core", "playerbots"):
        (tmp_path / k).mkdir()

    def fake(now, ancestor, known=True):
        def git(path, *args):
            if args[:2] == ("rev-parse", "--verify"):
                return now
            if args[0] == "merge-base":
                return "" if ancestor else None
            if args[0] == "cat-file":
                return "" if known else None
            return None
        return git

    def moved(git):
        v = view(layout, git=git)
        return [b["moved"] for b in v["provenance"]["baselines"]], v["provenance"]
    assert moved(fake("abc1234567", True))[0] == ["same"] * 3
    got, prov = moved(fake("f" * 40, True))
    assert got == ["past"] * 3 and prov["created_utc"] == "2026-11-01T00:00:00Z"
    assert prov["baselines"][0]["name"] == "install_root" and prov["baselines"][0]["head"] == "abc1234567"
    assert prov["baselines"][0]["now"] == "f" * 40 and prov["baselines"][0]["path"].endswith("install_root")
    assert moved(fake("f" * 40, False))[0] == ["diverged"] * 3
    assert moved(fake("f" * 40, False, known=False))[0] == ["unknown"] * 3
    assert moved(fake(None, False))[0] == ["unknown"] * 3


def test_git_runs_only_for_a_plain_head_and_a_real_folder(tmp_path):
    layout, data, _ = make_layout(tmp_path)
    put(data / "manifests" / "provenance.json", {"created_utc": "x", "baseline": {
        "core": {"path": str(tmp_path), "head": "--upload-pack=evil"},
        "playerbots": {"path": "relative", "head": "abc1234"},
        "install_root": {"path": str(tmp_path / "missing"), "head": "abc1234"}}})
    calls = []
    v = view(layout, git=lambda *a: calls.append(a))
    assert calls == []
    assert [b["moved"] for b in v["provenance"]["baselines"]] == ["unknown"] * 3


def test_git_is_cached_with_the_rest(tmp_path):
    layout, data, _ = make_layout(tmp_path)
    provenance(data, tmp_path)
    for k in ("install_root", "core", "playerbots"):
        (tmp_path / k).mkdir()
    calls = []
    cache, t = {}, [100.0]

    def ask():
        dojoview.dojo_view(layout, conns=None, git=lambda *a: calls.append(a), cache=cache, clock=lambda: t[0])
    ask()
    n = len(calls)
    assert n
    ask()
    assert len(calls) == n
    t[0] += dojoview.CACHE_S + 1
    ask()
    assert len(calls) == 2 * n


def snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in root.rglob("*"))


def test_nothing_is_written_under_the_roots(tmp_path):
    layout, data, runtime = make_layout(tmp_path)
    provenance(data, tmp_path)
    build(data, "20261105_100000")
    run_dir(data, "20261105_181500", "g1", {"gate": "G1", "ok": True, "episodes": [episode("e")]}, md="x")
    put(runtime / "run" / "worldserver.pid", "1")
    before = snapshot(tmp_path)
    view(layout, [])
    view(layout, None)
    assert snapshot(tmp_path) == before


def test_no_git_installed_means_none(tmp_path, monkeypatch):
    monkeypatch.setattr(dojoview.shutil, "which", lambda name: None)
    assert dojoview.run_git(tmp_path, "rev-parse", "--verify", "HEAD") is None


def test_git_runs_without_a_console_window(tmp_path, monkeypatch):
    """The logon task's pythonw console must never flash a git window (as status.py and
    health.py run their tools)."""
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return SimpleNamespace(returncode=0, stdout="abc\n")
    monkeypatch.setattr(dojoview.shutil, "which", lambda name: "git")
    monkeypatch.setattr(dojoview.subprocess, "run", fake_run)
    assert dojoview.run_git(tmp_path, "rev-parse", "HEAD") == "abc"
    assert seen["creationflags"] == getattr(dojoview.subprocess, "CREATE_NO_WINDOW", 0)


def test_the_route_reads_the_layout_and_the_console_tcp_table(tmp_path):
    layout, data, runtime = make_layout(tmp_path)
    build(data, "20261105_100000")
    put(runtime / "run" / "worldserver.pid", "4242")

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path), dojo_layout=layout,
                       tcp=lambda: [Conn("0.0.0.0", WORLD, "0.0.0.0", 0, LISTEN, 4242)])
        await app.start()
        try:
            for _ in range(100):
                if app._conns is not None:
                    break
                await asyncio.sleep(0.05)
            r = await http(app, "GET", "/api/dojo")
            assert r.status == 200
            v = r.json()
            assert v["setup"] is True and v["build"]["variant"] == "training"
            assert v["realm"]["state"] == "running"
        finally:
            await app.close()
    asyncio.run(go())
