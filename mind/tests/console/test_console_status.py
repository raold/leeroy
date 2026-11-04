"""How the console derives the mind's state and the owner's game state, reads mind.log without
its content, and parses Ollama's and nvidia-smi's answers. Fakes and scratch files only."""
import json
from types import SimpleNamespace

import pytest

from leeroy_mind.console import mindlog, netinfo, status
from leeroy_mind.console.status import IN_GAME, NOT_IN_GAME, UNKNOWN, game_view, mind_view

FAMILY = [SimpleNamespace(pid=101), SimpleNamespace(pid=100)]


# -- the mind's state ----------------------------------------------------------------------------

def test_running_starting_and_stopping_come_from_the_family_and_its_lock():
    v = mind_view(FAMILY, True)
    assert (v.state, v.pid, v.pids, v.lock) == ("running", 101, (101, 100), True)
    assert mind_view(FAMILY, False).state == "starting"
    assert mind_view(FAMILY, False, stopping=True).state == "stopping"


def test_stopped_and_crashed_need_evidence():
    assert mind_view([], False).state == "stopped"
    failure = {"event": "mind_failed", "ts": "2026-11-04T21:00:00"}
    v = mind_view([], False, run_failed=True, failure=failure)
    assert v.state == "crashed" and "mind_failed" in v.detail
    v = mind_view([], False, launch_exit=0xC0000005)  # an access violation
    assert v.state == "crashed" and "3221225477" in v.detail
    assert mind_view([], False, launch_exit=-11).state == "crashed"
    # 1 is also what a stop from elsewhere (ac-server.sh) leaves: said, not called a crash
    v = mind_view([], False, launch_exit=1)
    assert v.state == "stopped" and "code 1" in v.detail
    assert mind_view([], False, launch_exit=0xC0000005, stopping=True).state == "stopped"
    assert mind_view([], False, launch_exit=0) == mind_view([], False)


def test_an_unidentified_lock_or_ambiguity_is_unknown_never_stopped():
    v = mind_view([], True)
    assert v.state == "unknown" and "not this checkout's mind" in v.detail
    v = mind_view(None, True, error="multiple mind process families match")
    assert v.state == "unknown" and v.detail.startswith("multiple")
    assert mind_view(None, True).state == "running"  # no process list: the lock alone
    assert mind_view(None, False).state == "stopped"


# -- the owner's game state ----------------------------------------------------------------------

def test_game_state_is_only_known_while_the_mind_runs_with_its_bridge():
    assert game_view("running", True, {"id": 3, "started": 10.0}) == status.GameView(
        IN_GAME, since=10.0)
    assert game_view("running", True, None).state == NOT_IN_GAME
    for mind in ("stopped", "crashed"):
        g = game_view(mind, True, {"id": 3, "started": 10.0})
        assert (g.state, g.reason) == (UNKNOWN, "the mind is not running")
    assert game_view("starting", None, None).reason == "the mind is starting"
    assert game_view("running", False, {"id": 1, "started": 1}).reason == \
        "the mind's game bridge is down"
    assert game_view("running", None, None).state == UNKNOWN


def test_the_bridge_link_is_an_established_connection_owned_by_the_mind():
    conns = [netinfo.Conn("127.0.0.1", 50000, "127.0.0.1", 7878, netinfo.ESTABLISHED, 101),
             netinfo.Conn("127.0.0.1", 7880, "0.0.0.0", 0, netinfo.LISTEN, 500)]
    assert netinfo.linked(conns, 7878, [101, 100]) is True
    assert netinfo.linked(conns, 7878, [999]) is False
    assert netinfo.linked(conns, 7879, [101]) is False
    assert netinfo.linked(None, 7878, [101]) is None and netinfo.linked(conns, 7878, []) is None
    assert netinfo.listening(conns, 7880) is True and netinfo.listening(conns, 7881) is False


# -- mind.log ------------------------------------------------------------------------------------

def rec(event, level="INFO", **fields):
    return {"ts": "2026-11-04T21:00:00.000+00:00", "level": level, "event": event, **fields}


def test_content_free_keeps_events_and_counts_never_text():
    r = rec("say_sent", guid=101, line=7, kind="greet", why="reply:named", topic="dragons",
            bit="'a joke about Arkon'", detail="leftover words", msg="whole message",
            data_dir="C:/x", ok=True, ms=1.5)
    out = mindlog.content_free(r)
    assert out == {"ts": r["ts"], "level": "INFO", "event": "say_sent",
                   "fields": {"guid": 101, "line": 7, "kind": "greet", "ok": True, "ms": 1.5}}
    out = mindlog.content_free(rec("x", reason="no candidates here", error="ValueError"))
    assert out["fields"] == {"error": "ValueError"}  # a phrase is text, not an identifier
    out = mindlog.content_free({"event": "Not An Event", "level": "LOUD", "ts": 5})
    assert (out["event"], out["level"], out["ts"]) == ("log", "INFO", "")


def test_content_free_keeps_only_an_exceptions_class():
    exc = 'Traceback (most recent call last):\n  File "x.py"\nKeyError: \'Arkon said hi\''
    assert mindlog.content_free(rec("tick_failed", "ERROR", exc=exc))["exc"] == "KeyError"
    exc = "Traceback\nleeroy_mind.llm.client.LLMError: model output"
    assert mindlog.content_free(rec("x", exc=exc))["exc"] == "leeroy_mind.llm.client.LLMError"


def test_scan_finds_the_latest_runs_failure_and_bridge():
    view = mindlog.scan([rec("mind_started"), rec("bridge_connected"),
                         rec("loop_stopped", "ERROR", loop="bridge"), rec("shutdown_complete")])
    assert view.run_failed and view.failure["event"] == "loop_stopped" and view.bridge is True
    view = mindlog.scan([rec("mind_failed", "ERROR"), rec("mind_started"),
                         rec("bridge_connected"), rec("bridge_disconnected")])
    assert not view.run_failed and view.bridge is False
    view = mindlog.scan([rec("mind_started"), rec("shutdown_complete"),
                         rec("config_invalid", "ERROR")])
    assert view.run_failed  # a later start that never got going
    assert [e["event"] for e in view.errors] == ["config_invalid"]


def lines(*records):
    return "".join(json.dumps(r) + "\n" for r in records).encode()


def test_the_follower_reads_appended_lines_and_keeps_a_partial_one(tmp_path):
    path = tmp_path / "mind.log"
    path.write_bytes(lines(rec("a")) + b'{"event": "b", "le')
    f = mindlog.LogFollower(path)
    assert f.poll() and [r["event"] for r in f.records] == ["a"]
    assert not f.poll()
    with path.open("ab") as fh:
        fh.write(b'vel": "INFO"}\n' + lines(rec("c")))
    assert f.poll() and [r["event"] for r in f.records] == ["a", "b", "c"]


def test_the_follower_starts_again_after_rotation_and_skips_a_cut_line(tmp_path):
    path = tmp_path / "mind.log"
    path.write_bytes(lines(*[rec(f"e{i}") for i in range(50)]))
    f = mindlog.LogFollower(path, seed_bytes=300)
    f.poll()
    events = [r["event"] for r in f.records]
    assert events and events[-1] == "e49" and "e0" not in events
    assert all(e.startswith("e") for e in events)  # no half line parsed
    path.unlink()
    assert f.poll() and not f.records
    path.write_bytes(lines(rec("new")))
    assert f.poll() and [r["event"] for r in f.records] == ["new"]
    path.write_bytes(lines(rec("x")))  # replaced by a smaller file
    f.poll()
    assert [r["event"] for r in f.records] == ["x"]


def test_the_follower_tail_is_content_free(tmp_path):
    path = tmp_path / "mind.log"
    path.write_bytes(lines(rec("heard", text="something private", guid=5)))
    f = mindlog.LogFollower(path)
    f.poll()
    assert f.tail() == [{"ts": rec("x")["ts"], "level": "INFO", "event": "heard",
                         "fields": {"guid": 5}}]


# -- Ollama and the GPU --------------------------------------------------------------------------

def test_ollama_models_marks_the_minds_roles():
    reply = {"models": [{"name": "live:q4", "size": 16_000_000_000, "size_vram": 15_000_000_000,
                         "expires_at": "2026-11-04T22:00:00Z"},
                        {"name": "embed:latest", "size": 1, "size_vram": 1},
                        {"name": "other", "size": "big"}, "junk"]}
    seen = []
    out = status.ollama_models("http://127.0.0.1:11434/", {"live": "live:q4", "embedding": "embed",
                                                           "reflection": "r"},
                               fetch=lambda url, t: seen.append(url) or reply)
    assert seen == ["http://127.0.0.1:11434/api/ps"]
    assert [(m["name"], m["role"]) for m in out["models"]] == [
        ("live:q4", "live"), ("embed:latest", "embedding"), ("other", None)]
    assert out["models"][2]["size"] is None


def test_ollama_models_reports_an_unreachable_ollama():
    def down(url, t):
        raise OSError("refused")
    out = status.ollama_models("http://127.0.0.1:11434", {}, fetch=down)
    assert out == {"ok": False, "url": "http://127.0.0.1:11434",
                   "error": "not reachable (OSError)", "models": []}
    out = status.ollama_models("u", {}, fetch=lambda url, t: {"models": "x"})
    assert out["ok"] is False


def test_gpu_headroom_parses_nvidia_smi():
    def run(argv, **kw):
        assert argv[0] == "nvidia-smi.exe" and "--format=csv,noheader,nounits" in argv
        return SimpleNamespace(returncode=0,
                               stdout="NVIDIA GeForce RTX 4090, 24564, 20000, 4564, 37\nbad line\n")
    out = status.gpu_headroom(which=lambda n: "nvidia-smi.exe", run=run)
    assert out == {"ok": True, "gpus": [{"name": "NVIDIA GeForce RTX 4090", "total_mb": 24564,
                                         "used_mb": 20000, "free_mb": 4564, "util": 37}]}
    assert status.gpu_headroom(which=lambda n: None)["error"] == "nvidia-smi not found"
    out = status.gpu_headroom(which=lambda n: "x",
                              run=lambda *a, **k: SimpleNamespace(returncode=9, stdout=""))
    assert out["ok"] is False and "code 9" in out["error"]


def test_a_running_minds_kept_config_warning_is_not_a_failure():
    """A running mind that cannot reload an edited mind.toml logs config_invalid as a WARNING
    and keeps its config; a start that never got going logs it as an ERROR."""
    view = mindlog.scan([rec("mind_started"), rec("config_invalid", "WARNING")])
    assert not view.run_failed and [e["event"] for e in view.errors] == ["config_invalid"]
    # records that begin inside a run (a long one, or a midnight rotation)
    assert not mindlog.scan([rec("config_invalid", "WARNING")]).run_failed
    assert mindlog.scan([rec("mind_started"), rec("config_invalid", "ERROR")]).run_failed
    assert mindlog.scan([rec("config_invalid", "ERROR")]).run_failed
    assert mindlog.scan([rec("mind_started"), rec("shutdown_complete"),
                         rec("config_invalid", "WARNING")]).run_failed  # between runs
    assert mindlog.scan([rec("mind_started"), rec("loop_stopped", "WARNING")]).run_failed
