"""The sandbox's lab additions: the lab's place under scratch, its mind.toml, overlays written
into the copy on reset (never the live prompts dir), and the CLI's --lab."""
import asyncio
import json
import os
import tomllib

import pytest

from lab.rig import InProcessChild, digest, make_live
from leeroy_mind.lab import child
from leeroy_mind.lab.config import load as load_lab
from leeroy_mind.learn.arms import prompt_families
from leeroy_mind.learn.config import load_learn_config
from leeroy_mind.sandbox import prepare
from leeroy_mind.sandbox.cli import sandbox_main
from leeroy_mind.talk import pieces
from leeroy_mind.talk.beat import MARK

VARIANT = pieces.builtin("reply_human").replace("Respond to what", "Plainly respond to what", 1)


def answer(model, messages):
    last = messages[-1]["content"]
    if MARK in last:
        return json.dumps({"read": "hi", "lands": "ok", "want": "share", "use": None})
    return "plain" if "Plainly respond" in last else "usual"


def test_the_lab_copy_lives_in_scratch_with_its_own_settings(tmp_path):
    live = make_live(tmp_path)
    layout = child.take(live)
    assert layout.root == (live / "scratch" / "lab").resolve()
    doc = tomllib.loads((layout.snapshot / "mind.toml").read_text("utf-8"))
    assert doc["lab"]["enabled"] is False and doc["lab"]["overlays"] is True
    assert doc["life"]["availability"] == 0
    assert set(prompt_families()) <= set(doc["learn"]["frozen_families"])
    path = layout.snapshot / "mind.toml"
    assert load_lab(path).overlays and not load_lab(path).enabled
    assert set(prompt_families()) <= set(load_learn_config(path).frozen_families)
    # again: the same place, a fresh snapshot; a stranger's folder or a junction is refused
    assert child.take(live).root == layout.root
    (tmp_path / "other").mkdir()
    other = make_live(tmp_path / "other")
    (other / "scratch" / "lab").mkdir(parents=True)
    (other / "scratch" / "lab" / "x.txt").write_bytes(b"x")
    with pytest.raises(prepare.Refused):
        prepare.lab_layout(other)


def test_reset_writes_overlays_into_the_copy_only(tmp_path):
    live = make_live(tmp_path)
    before = digest(live / "prompts")
    child.take(live)

    async def go():
        c = InProcessChild(live, 1, answer, tmp_path)
        url = await c.start()
        try:
            client = child._client(url)
            plain = await child._call(client, "POST", "/say",
                                      {"channel": "dm", "to": 103, "text": "hey you", "wait": 60})
            await child._call(client, "POST", "/reset", {"overlays": {"reply_human": VARIANT}},
                              timeout=120)
            varied = await child._call(client, "POST", "/say",
                                       {"channel": "dm", "to": 103, "text": "hey you", "wait": 60})
            if not varied["answers"]:  # a loaded machine can time out a fresh mind's first store
                varied = await child._call(client, "POST", "/say",
                                           {"channel": "dm", "to": 103, "text": "hey", "wait": 60})
            refused = []
            for bad in ({"nope": "x"}, {"reply_human": "no placeholders"}):
                try:
                    await child._call(client, "POST", "/reset", {"overlays": bad}, timeout=60)
                except RuntimeError as e:
                    refused.append(str(e))
            return plain, varied, refused
        finally:
            await c.stop()
    plain, varied, refused = asyncio.run(go())
    assert [a["text"] for a in plain["answers"]] == ["usual"]
    assert [a["text"] for a in varied["answers"]] == ["plain"]
    assert any(e.get("event") == "lab_piece" and e.get("piece") == "reply_human"
               for e in varied["events"])
    assert len(refused) == 2 and "not a prompt piece" in refused[0]
    copy = live / "scratch" / "lab" / "mind-data" / "prompts"
    assert (copy / "lab_reply_human.txt").read_bytes().decode() == VARIANT
    assert digest(live / "prompts") == before and not (live / "prompts" / "lab_reply_human.txt").exists()


def test_the_cli_takes_out_or_lab_never_both(tmp_path, capsys):
    live = make_live(tmp_path)
    assert sandbox_main(["--data-dir", str(live)]) == 1
    assert sandbox_main(["--data-dir", str(live), "--lab", "--out", str(tmp_path / "x")]) == 1
    assert "--out DIR, or --lab" in capsys.readouterr().err


def test_the_subprocess_child_runs_this_code_on_the_lab_copy(tmp_path):
    live = make_live(tmp_path)
    c = child.SubprocessChild(live, seed=7, python="py")
    assert c.command() == ["py", "-m", "leeroy_mind", "sandbox", "--data-dir", str(live), "--lab",
                           "--reuse", "--fast", "--seed", "7"]
    env = c.env()
    import leeroy_mind
    from pathlib import Path
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(Path(leeroy_mind.__file__).resolve().parents[1])
    assert env["LEEROY_MIND_DATA"] == str(c.layout.data)


def test_the_child_env_keeps_drive_letter_paths_whole(tmp_path, monkeypatch):
    """PYTHONPATH is split on the platform's own separator: on Windows `F:\\x` is one entry, not
    `F` and `\\x` (the test once split on ':' and read the code path back as `F`)."""
    live = make_live(tmp_path)
    c = child.SubprocessChild(live, seed=1, python="py")
    root = c.env()["PYTHONPATH"].split(os.pathsep)[0]
    mine = "F:\\azerothcore\\other\\mind" if os.pathsep == ";" else "/mnt/f/other/mind"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([mine, root, "/srv/x"]))
    assert c.env()["PYTHONPATH"].split(os.pathsep) == [root, mine, "/srv/x"]


def test_score_rows_run_the_rules_per_turn():
    rows = child.score_rows([{"pair": 1, "guid": 101, "kind": "question", "channel": "dm", "turns": [
        {"his": "you free tonight?", "ask": "yesno", "nouns": [], "reply": "yeah after 9",
         "rewritten": False, "applied": True},
        {"his": "cool", "ask": None, "nouns": [], "reply": "", "rewritten": False, "applied": False}]}])
    assert [(r["turn"], r["answered"], r["applied"], r["rules"]["silent"]) for r in rows] == [
        (1, 1, 1, 0), (2, 0, 0, 1)]
    assert rows[0]["words"] == 3 and rows[0]["his_words"] == 3


def test_the_subprocess_child_finds_a_new_sandbox_file_and_kills_its_whole_tree(tmp_path):
    """A venv's python.exe is a launcher: the sandbox writes the interpreter's pid, not the
    launcher's. Any sandbox.json written since the launch is the child's; kill takes the tree."""
    import sys
    import time
    live = make_live(tmp_path)
    child.take(live)
    info = child.prepare.lab_layout(live).info
    info.write_bytes(json.dumps({"url": "http://127.0.0.1:1", "pid": 1, "started": 1.0}).encode())
    script = ("import json,os,subprocess,sys,time;"
              "kid=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
              f"open({str(info)!r},'w').write(json.dumps({{'url':'http://127.0.0.1:9','pid':kid.pid,"
              "'started':time.time()}));time.sleep(60)")

    class Fake(child.SubprocessChild):
        def command(self):
            return [sys.executable, "-c", script]

        async def _stop_stale(self):
            pass

    c = Fake(live, seed=1)

    async def go():
        url = await c.start()
        kid = json.loads(info.read_bytes())["pid"]
        c.kill()
        return url, kid
    url, kid = asyncio.run(go())
    assert url == "http://127.0.0.1:9"
    deadline = time.time() + 20
    while c.proc.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    assert c.proc.poll() is not None
    import os
    if os.name == "nt":
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {kid}"], capture_output=True, text=True).stdout
        assert str(kid) not in out
