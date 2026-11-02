"""Opt-in real-model smoke over an entirely synthetic, copied CLI trial.

Default collection deselects this test. To opt in, close WoW, stop the mind/voice services
outside pytest, prevent their supervisors from restarting them, and set
LEEROY_NIGHTLY_SMOKE=1 and LEEROY_NIGHTLY_SMOKE_IDLE=1. Run with -m ollama. This test never
stops a process and never reads canonical mind-data; tests/live_guard.py stays fully armed.
Windows process inventory conservatively refuses any other Python process, including a
mind launched under Python. The operator's idle acknowledgement is still required because
a process scan cannot prevent an unrelated service from starting after the scan.

The synthetic source database must remain byte-for-byte unchanged while the default CLI
creates/migrates its trial copy. Canonical snapshot/migration rehearsal is a separate
controller operation outside pytest. The bounded pipeline here runs exactly one reflection,
one confidential-thread review, and one encounter, using the configured default real models.
Set LEEROY_NIGHTLY_SMOKE_GPU_RESIDENCY=1 separately to assert full GPU placement of both the
reflection and embedding models while resident. No placement claim is made otherwise.
"""
import asyncio
import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _process_preflight():
    if os.name != "nt":
        pytest.skip("this opt-in smoke's process admission is implemented for Windows")
    result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                            text=True, check=True, timeout=10)
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if not rows or any(len(row) < 2 or not row[1].isdigit() for row in rows):
        pytest.fail("smoke refused: process inventory was incomplete", pytrace=False)
    owned = {os.getpid(), os.getppid()}
    for row in rows:
        name, pid = row[0].casefold(), int(row[1])
        if name in {"wow.exe", "wow-64.exe", "wowclassic.exe", "wowclassic_t.exe", "wowb.exe"}:
            pytest.skip("smoke refused: WoW is running; no process was stopped")
        if (pid not in owned and (name.startswith(("python", "pypy"))
                or name in {"leeroy-mind.exe", "leeroy_mind.exe", "leeroy-voice.exe", "leeroy_voice.exe"})):
            pytest.skip("smoke refused: another Python/mind/voice process is running")


def _ps(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url.rstrip("/") + "/api/ps", timeout=10) as reply:
        value = json.loads(reply.read(1024 * 1024))
    if not isinstance(value, dict) or not isinstance(value.get("models"), list):
        pytest.fail("smoke refused: Ollama residency response was incomplete", pytrace=False)
    return value["models"]


def _gpu_preflight(cfg):
    from leeroy_mind.nightly.gpu import norm

    # Two immediate readings avoid admitting a GPU merely between short active kernels.
    for _ in range(2):
        result = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu",
                                 "--format=csv,noheader,nounits"], capture_output=True,
                                text=True, check=True, timeout=10)
        values = [int(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        if not values or any(value > 10 for value in values):
            pytest.skip("smoke refused: the GPU is busy or its utilization is unavailable")
        time.sleep(.25)
    allowed = {norm(cfg.models.live), norm(cfg.models.reflection), norm(cfg.models.embedding)}
    if any(not isinstance(row.get("name"), str) or norm(row["name"]) not in allowed
           for row in _ps(cfg.ollama.url)):
        pytest.skip("smoke refused: an unrelated Ollama model is resident")


def _full_gpu(cfg):
    from leeroy_mind.nightly.gpu import norm

    rows = {norm(row["name"]): row for row in _ps(cfg.ollama.url)}
    for model in (cfg.models.reflection, cfg.models.embedding):
        row = rows.get(norm(model))
        assert row is not None, "an expected model was not resident during the placement check"
        assert (type(row.get("size")) is int and row["size"] > 0
                and type(row.get("size_vram")) is int and row["size_vram"] >= row["size"]), \
            "the requested full-GPU placement check found a CPU/GPU split or missing accounting"


def _fingerprint(path):
    out = []
    for item in (path, Path(str(path) + "-wal")):
        out.append(None if not item.exists() else
                   (item.stat().st_size, item.stat().st_mtime_ns, hashlib.sha256(item.read_bytes()).hexdigest()))
    return tuple(out)


def _seed(h):
    """Three invented guildmates, public memories, and one plain private whisper thread."""
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM persona_core WHERE guid NOT IN (101,102,103)")
    h.woman(101, "drawn")
    for guid in (101, 102, 103):
        for n, text in enumerate(("Repaired my armor before the raid.",
                                  "Restocked flasks before the next pull.",
                                  "Sorted my bags at the bank after the raid.")):
            at = h.T - 3600 + (guid - 101) * 60 + n
            line = h.say(at, guid, text, channel="guild")
            h.mem(guid, text, t=at, evidence=(f"chat:{line}",), actors=(guid,))
    lines = []
    for n, (guid, text) in enumerate(((101, "got the bakery job today! my trial shift is tomorrow morning"),
                                     (1, "that's great, congratulations!"),
                                     (101, "thanks, I was nervous about the interview all week"))):
        lines.append(h.say(h.T + n * 60, guid, text, channel="whisper", to=1 if guid == 101 else 101))
    with h.store.transaction():
        tid = h.store.open_thread("whisper:1:101", h.T)
        h.store.set_thread(lines, tid)
        h.store.close_thread(tid, h.T + 300, "extracted")
        h.mem(101, "Told Testy I got the bakery job and my trial shift is tomorrow.",
              t=h.T + 1, scope="private", scope_with=1, actors=(1,), evidence=(f"chat:{lines[0]}",))
        h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status) "
            "VALUES (?,0,102,103,'restocking flasks together at the Orgrimmar bank','planned')", (h.night,))
    return tid


def test_smoke_seed_is_valid_without_any_model_or_process_access(h):
    tid = _seed(h)
    assert h.store.thread_status(tid) == "extracted"
    assert h.count("encounter") == 1
    assert h.store.conn.execute("SELECT count(*) FROM memory WHERE scope='private' AND scope_with=1").fetchone()[0] == 1


@pytest.mark.ollama
@pytest.mark.skipif(os.environ.get("LEEROY_NIGHTLY_SMOKE") != "1", reason="set LEEROY_NIGHTLY_SMOKE=1 explicitly")
def test_real_reflection_confide_encounter_in_copied_cli_trial(h, monkeypatch, capsys):
    if os.environ.get("LEEROY_NIGHTLY_SMOKE_IDLE") != "1":
        pytest.skip("explicit stopped-service/idle-GPU preflight acknowledgement is required")
    _process_preflight()
    from leeroy_mind.app import instance_lock_held
    from leeroy_mind.config import load_config
    from leeroy_mind.nightly import cli, pipeline
    from leeroy_mind.nightly.confide import confide_thread
    from leeroy_mind.nightly.encounters import run_encounter
    from leeroy_mind.nightly.knowledge import scope_leaks
    from leeroy_mind.nightly.reflect import reflect_member
    from leeroy_mind.nightly.runner import Stage, stage_snapshot
    from leeroy_mind.nightly.settings import night_bounds
    from leeroy_mind.storage.db import latest_version
    from leeroy_mind.storage.store import Store
    from nightly.conftest import no_real_name

    root = h.settings.data_dir
    # This path is the harness's temporary synthetic directory, never canonical mind-data.
    (root / "mind.toml").write_text('[human]\nnickname="Testy"\nreal_name="Quillon"\npronouns="he"\n', encoding="utf-8")
    cfg = load_config(root / "mind.toml")
    _gpu_preflight(cfg)
    assert not instance_lock_held(root), "synthetic source already has a writer"
    tid = _seed(h)
    end = night_bounds(h.night, timezone.utc)[1]
    assert end < datetime.now(timezone.utc).timestamp(), "smoke requires a past completed night"
    h.clock.set(end + 60)
    monkeypatch.setattr(cli, "SystemClock", lambda: h.clock)
    placement = os.environ.get("LEEROY_NIGHTLY_SMOKE_GPU_RESIDENCY") == "1"

    def limited(ctx):
        ctx.settings = replace(ctx.settings, parallel=1, questions=1, digest_size=6,
                               retrieve_k=3, max_outputs=2)

    async def confide(ctx):
        limited(ctx)
        await confide_thread(ctx, tid)

    async def reflect(ctx):
        limited(ctx)
        await reflect_member(ctx, 102)

    async def encounter(ctx):
        limited(ctx)
        row = ctx.store.conn.execute("SELECT * FROM encounter WHERE night=? AND idx=0", (ctx.night,)).fetchone()
        await run_encounter(ctx, row)
        if placement:
            await asyncio.to_thread(_full_gpu, cfg)

    # Limit the real-model spend, preserving the actual CLI copy, client, writer, runner,
    # arbitration, grounding, and transaction paths for the plan's three smoke units.
    monkeypatch.setattr(pipeline, "STAGES", (Stage("snapshot", stage_snapshot, None),
        Stage("confide", confide, "reflection"), Stage("reflect", reflect, "reflection"),
        Stage("encounters", encounter, "reflection")))
    execute = cli._run

    async def bounded(*args, **kwargs):
        return await asyncio.wait_for(execute(*args, **kwargs), 1200)

    monkeypatch.setattr(cli, "_run", bounded)
    source = root / "mind.db"
    schema = h.store.conn.execute("PRAGMA user_version").fetchone()[0]
    before = _fingerprint(source)
    _process_preflight()
    try:
        code = cli.nightly_main(["run", "--night", h.night, "--data-dir", str(root)])
    finally:
        assert _fingerprint(source) == before, "the CLI mutated its synthetic source database"
        assert h.store.conn.execute("PRAGMA user_version").fetchone()[0] == schema
    output = capsys.readouterr()
    assert code == 0, "real-model CLI smoke failed; inspect the captured synthetic diagnostics"
    assert "ReflectionError" not in output.err
    assert h.count("night_step") == 0 and h.count("grounding_log") == 0
    trial = root / "scratch" / f"mind-{h.night}-trial.db"
    conn = sqlite3.connect(trial.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    copied = Store(conn, clock=h.clock)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version()
        assert conn.execute("SELECT status FROM night_run WHERE night=?", (h.night,)).fetchone()[0] == "done"
        stages = {row[0]: row[1] for row in conn.execute("SELECT stage,count(*) FROM night_stage WHERE night=? GROUP BY stage", (h.night,))}
        assert stages == {"snapshot": 1, "confide": 1, "reflect": 1, "encounters": 1}
        steps = {tuple(row) for row in conn.execute("SELECT stage,unit FROM night_step WHERE night=?", (h.night,))}
        assert {("confide", str(tid)), ("reflect", "102"), ("encounters", "enc:0")} <= steps
        logged = {row[0] for row in conn.execute("SELECT DISTINCT stage FROM grounding_log WHERE night=?", (h.night,))}
        assert {"confide", "reflect", "encounters"} <= logged
        assert scope_leaks(copied) == [] and no_real_name(copied) == []
        assert conn.execute("SELECT count(*) FROM memory WHERE owner=1").fetchone()[0] == 0
    finally:
        conn.close()  # Store.close() would checkpoint a deliberately read-only connection.
