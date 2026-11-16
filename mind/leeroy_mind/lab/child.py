"""The lab's sandbox child and the battery run against it.

The sandbox (sandbox/README.md) is a real MindApp on a copy of the guild, the test suite's fake
bridge and the shared Ollama's live model. The lab's copy lives in <data dir>/scratch/lab
(prepare.lab_layout): the snapshot is taken once a night at the lab's start (take), then each arm
restores it (`--reuse`; the control first, each variant through POST /reset with its piece's
overlay in the copy's prompts/, never in the live one). `--fast`: answers in model time.

The copy's mind.toml is the sandbox's (prepare.QUIET) with the lab's own settings over it (EXTRA):
the learn bandit's prompt families frozen (a lab arm already live never mixes into the control),
and `[life] availability` 0 (members answer at once; the lab measures words, not timing).

SubprocessChild starts `python -m leeroy_mind sandbox --lab --reuse --fast` with the mind's own
interpreter and environment; a cancel kills it at once (the night's stop budget), a normal end
asks it to stop. Tests use an in-process child (the same Sandbox and Api)."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from leeroy_mind.sandbox import prepare

from . import rules

START_S = 600.0     # the child's start: restore, migrate, library, the mind's start checks
STOP_S = 60.0
SAY_WAIT_S = 90     # seconds per line of his, the sandbox's /say wait
RESET_S = 600.0


def extra_toml(live: Path) -> dict:
    """The lab's settings over the sandbox's quiet mind.toml (see the module docstring)."""
    import tomllib
    from leeroy_mind.learn.arms import prompt_families
    try:
        doc = tomllib.loads((Path(live) / prepare.TOML).read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        doc = {}
    learn = doc.get("learn") if isinstance(doc.get("learn"), dict) else {}
    frozen = [f for f in learn.get("frozen_families", []) if isinstance(f, str)]
    return {"learn": {"frozen_families": sorted(set(frozen) | set(prompt_families()))},
            "life": {"availability": 0}}


def take(live: Path) -> prepare.Layout:
    """The night's copy for the lab: <data dir>/scratch/lab/snapshot, library.db copied once."""
    layout = prepare.lab_layout(live)
    prepare.make_root(layout)
    prepare.take_snapshot(Path(live), layout, extra=extra_toml(live))
    prepare.copy_library(Path(live), layout)
    return layout


def _client(url: str):
    from leeroy_mind.sandbox.cli import Client
    return Client(url)


async def _call(client, method: str, path: str, body=None, *, timeout: float = 60.0):
    return await asyncio.to_thread(client.call, method, path, body, timeout=timeout)


class SubprocessChild:
    """`python -m leeroy_mind sandbox --data-dir D --lab --reuse --fast --seed S`."""

    def __init__(self, live: Path, *, seed: int, python: str | None = None):
        self.live = Path(live)
        self.seed = seed
        self.python = python or sys.executable
        self.layout = prepare.lab_layout(self.live)
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None

    def command(self) -> list[str]:
        return [self.python, "-m", "leeroy_mind", "sandbox", "--data-dir", str(self.live),
                "--lab", "--reuse", "--fast", "--seed", str(self.seed)]

    def env(self) -> dict:
        import leeroy_mind
        env = os.environ.copy()
        root = str(Path(leeroy_mind.__file__).resolve().parents[1])
        parts = [root, *[p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != root]]
        env.update(PYTHONPATH=os.pathsep.join(parts), PYTHONUNBUFFERED="1",
                   LEEROY_MIND_DATA=str(self.layout.data))
        env.pop("PYTHONHOME", None)
        return env

    async def _stop_stale(self) -> None:
        """A child left from an earlier run (the mind died under it): ask it to stop."""
        try:
            info = json.loads(self.layout.info.read_bytes())
            url = info.get("url")
        except (OSError, ValueError, AttributeError):
            return
        if isinstance(url, str) and url.startswith("http://127.0.0.1:"):
            try:
                await _call(_client(url), "POST", "/stop", {}, timeout=5)
                await asyncio.sleep(3)
            except (RuntimeError, OSError):
                pass

    async def start(self) -> str:
        await self._stop_stale()
        self.layout.info.unlink(missing_ok=True)
        log_path = self.layout.root / "child.log"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        launched = time.time()
        with log_path.open("ab") as out:
            self.proc = subprocess.Popen(self.command(), env=self.env(), stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=subprocess.STDOUT, creationflags=flags,
                                         close_fds=True, start_new_session=os.name != "nt")
        deadline = time.monotonic() + START_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"lab_child_exited:{self.proc.returncode}")
            try:
                # the sandbox writes its own pid, which a venv's launcher does not share: a file
                # written since this launch (the old one was removed) is this child's
                info = json.loads(self.layout.info.read_bytes())
                if (isinstance(info.get("url"), str) and isinstance(info.get("started"), (int, float))
                        and info["started"] >= launched - 1):
                    self.url = info["url"]
                    return self.url
            except (OSError, ValueError):
                pass
            await asyncio.sleep(0.5)
        raise TimeoutError("lab_child_start_timeout")

    def kill(self) -> None:
        """The child's whole process tree, at once: a venv's python.exe is a launcher whose own
        child is the interpreter, and killing the launcher alone would leave that running."""
        if self.proc is None:
            return
        kill_tree(self.proc)

    async def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.url and self.proc.poll() is None:
                try:
                    await _call(_client(self.url), "POST", "/stop", {}, timeout=10)
                except (RuntimeError, OSError):
                    pass
                deadline = time.monotonic() + STOP_S
                while self.proc.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
        finally:
            self.kill()


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    if proc.poll() is None:
        proc.kill()


# -- the battery against a running child ------------------------------------------------------

def _her(answers: list[dict], guid: int) -> list[dict]:
    return [a for a in answers if a.get("guid") == guid]


def _applied(events: list[dict], guid: int, piece: str | None) -> bool:
    return piece is not None and any(
        e.get("event") == "lab_piece" and str(e.get("guid")) == str(guid) and e.get("piece") == piece
        for e in events)


async def run_arm(url: str, plan: list[dict], scripts: dict, names: dict[int, str], *,
                  piece: str | None, overlays: dict | None, reset: bool, deadline: float,
                  clock=time.monotonic, wait: int = SAY_WAIT_S,
                  out: list | None = None) -> tuple[list[dict], bool]:
    """One arm of the battery: reset to the snapshot (with `overlays`) when `reset`, then every
    pair of `plan` in order. ([pair transcripts], complete): a pair is not begun once `clock()`
    passes `deadline`. A transcript: the pair's plan fields and its turns, each with his line,
    her lines and whether a guard rewrote them or the variant's piece was in her prompt. Each
    finished pair is appended to `out` as it finishes, so a cancel keeps what was done."""
    client = _client(url)
    if reset:
        await _call(client, "POST", "/reset", {"overlays": overlays or {}}, timeout=RESET_S)
    online = False
    out = [] if out is None else out
    for pair in plan:
        if clock() >= deadline:
            return out, False
        guid, channel = pair["guid"], pair["channel"]
        script = scripts[pair["script"]]
        name = names.get(guid, "")
        if channel != "dm" and not online:
            await _call(client, "POST", "/login", {})
            online = True
        if channel == "party":
            await _call(client, "POST", "/group", {"members": [guid]})
        turns = []
        try:
            for line in script.lines:
                text = line.text.replace("{name}", name).strip()
                body = {"channel": channel, "text": text, "wait": wait}
                if channel in ("dm", "whisper"):
                    body["to"] = guid
                res = await _call(client, "POST", "/say", body, timeout=wait + 120)
                hers = _her(res.get("answers", []), guid)
                turns.append({"his": text, "ask": line.ask, "nouns": list(line.nouns),
                              "lines": [str(a.get("text") or "") for a in hers],
                              "reply": " ".join(str(a.get("text") or "") for a in hers).strip(),
                              "rewritten": any((a.get("guard") or {}).get("rewritten") for a in hers),
                              "applied": _applied(res.get("events", []), guid, piece),
                              "settled": bool(res.get("settled"))})
        finally:
            if channel == "party":
                await _call(client, "POST", "/group", {"members": []})
        out.append({**pair, "turns": turns})
    return out, True


def score_rows(transcripts: list[dict]) -> list[dict]:
    """lab_score rows for an arm's transcripts: the rule checks and word counts, model-free."""
    rows = []
    for t in transcripts:
        earlier: list[str] = []
        for i, turn in enumerate(t["turns"], 1):
            flags = rules.check(turn["his"], turn["reply"], ask=turn["ask"],
                                nouns=tuple(turn["nouns"]), earlier=earlier,
                                rewritten=turn["rewritten"])
            rows.append({"pair": t["pair"], "turn": i, "guid": t["guid"], "kind": t["kind"],
                         "channel": t["channel"], "answered": int(bool(turn["reply"])),
                         "applied": int(bool(turn["applied"])), "rules": flags,
                         "words": len(rules.words(turn["reply"])),
                         "his_words": len(rules.words(turn["his"]))})
            if turn["reply"]:
                earlier.append(turn["reply"])
    return rows
