"""`python -m leeroy_mind sandbox` (start a sandbox mind) and `python -m leeroy_mind sandbox-talk`
(a scripted conversation against one). README.md says how to use them."""
import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import prepare
from .runtime import CHANNELS

log = logging.getLogger("leeroy_mind")


# -- sandbox ---------------------------------------------------------------------------------------

def _logging(log_dir: Path, level: str):
    """The sandbox mind's log in DIR/mind-data/logs/mind.log (the mind's JSON lines); only
    errors reach the terminal, which shows the sandbox's own lines."""
    from leeroy_mind.logs import KEEP_DAYS, LOG_FILE, ConsoleFormatter, JsonFormatter
    log_dir.mkdir(parents=True, exist_ok=True)
    to_file = logging.handlers.TimedRotatingFileHandler(
        log_dir / LOG_FILE, when="midnight", backupCount=KEEP_DAYS, encoding="utf-8", delay=True)
    to_file.setLevel(level)
    to_file.setFormatter(JsonFormatter())
    console = logging.StreamHandler()
    console.setLevel(logging.ERROR)
    console.setFormatter(ConsoleFormatter())
    old = log.level
    log.addHandler(to_file)
    log.addHandler(console)
    log.setLevel(level)

    def teardown():
        for handler in (to_file, console):
            log.removeHandler(handler)
            handler.close()
        log.setLevel(old)
    return teardown


async def serve(layout: prepare.Layout, *, port: int = 0, make_llm=None, online: bool = False,
                seed: int | None = None, clock=None, ollama_conf: Path | None = None,
                ready=None, say=print) -> int:
    """Start the sandbox mind and its API, serve until POST /stop or a cancel (Ctrl+C), then
    stop both. `ready(sandbox, api)` is told once both run (tests)."""
    from .api import Api
    from .runtime import Sandbox
    sandbox = Sandbox(layout, make_llm=make_llm, online=online, seed=seed, clock=clock,
                      ollama_conf=ollama_conf)
    api = Api(sandbox)
    try:
        await sandbox.start()
        await api.start(port)
        layout.info.write_bytes(json.dumps(
            {"url": api.url, "pid": os.getpid(), "started": time.time()}).encode("utf-8"))
        status = sandbox.status()
        say(f"sandbox: {api.url}  (data {layout.data}; {status['cast']} cast members; "
            f"model ready: {status['model']['ready']}"
            + ("; the copy has the GPU paused (Free GPU): members stay silent"
               if status["gpu_paused"] else "") + ")")
        say("sandbox: POST /stop or Ctrl+C stops it")
        if ready is not None:
            ready(sandbox, api)
        await api.stop_requested.wait()
        return 0
    finally:
        await api.close()
        await sandbox.close()
        layout.info.unlink(missing_ok=True)


def _sandbox_parser():
    from leeroy_mind.config import default_data_dir
    p = argparse.ArgumentParser(
        prog="leeroy_mind sandbox",
        description="A sandbox mind on a private copy of the live data: a fake bridge, the real "
                    "Ollama's live model, nothing acting on its own; driven over HTTP as the owner.")
    p.add_argument("--data-dir", type=Path, default=None,
                   help=f"the live data dir to copy (read only; default {default_data_dir()})")
    p.add_argument("--out", type=Path, required=True,
                   help="the sandbox's own directory (outside the data dir and any git checkout)")
    p.add_argument("--port", type=int, default=0, help="the API's port (default: a free one)")
    p.add_argument("--online", action="store_true", help="start with him in game")
    p.add_argument("--seed", type=int, default=None, help="the mind's random seed")
    p.add_argument("--reuse", action="store_true",
                   help="run on DIR's existing snapshot instead of copying the live data again")
    p.add_argument("--fresh-library", action="store_true",
                   help="copy library.db again (it is otherwise copied once)")
    p.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return p


def sandbox_main(argv=None, *, make_llm=None) -> int:
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock
    from leeroy_mind.config import default_data_dir
    args = _sandbox_parser().parse_args(argv)
    try:
        live = prepare.check_live(args.data_dir or default_data_dir())
        layout = prepare.check_out(args.out, live)
    except prepare.Refused as e:
        print(f"sandbox: {e}", file=sys.stderr)
        return 1
    if not 0 <= args.port <= 65535:
        print("sandbox: --port 0 to 65535", file=sys.stderr)
        return 1
    prepare.make_root(layout)
    try:
        lock = acquire_instance_lock(layout.data)
    except AlreadyRunning:
        print("sandbox: a sandbox runs in that directory", file=sys.stderr)
        return 1
    with lock:
        teardown = _logging(layout.data / "logs", args.log_level)
        try:
            if args.reuse:
                if prepare.snapshot_info(layout) is None:
                    raise prepare.Refused("no snapshot in that directory to reuse")
            else:
                print(f"sandbox: copying {live} (mind.db by online backup)", flush=True)
                prepare.take_snapshot(live, layout)
            if not (layout.data / prepare.LIBRARY).exists() or args.fresh_library:
                print("sandbox: copying library.db (once)", flush=True)
            prepare.copy_library(live, layout, fresh=args.fresh_library)
            return asyncio.run(serve(layout, port=args.port, make_llm=make_llm,
                                     online=args.online, seed=args.seed,
                                     say=lambda s: print(s, flush=True)))
        except prepare.Refused as e:
            print(f"sandbox: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 0
        finally:
            teardown()


# -- sandbox-talk ----------------------------------------------------------------------------------

class Client:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: dict | None = None, *, timeout: float = 60.0):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with self._opener.open(req, timeout=timeout) as reply:
                return json.loads(reply.read())
        except urllib.error.HTTPError as e:
            try:
                out = json.loads(e.read())
            except ValueError:
                out = {"ok": False, "error": f"HTTP {e.code}"}
            raise RuntimeError(out.get("error") or f"HTTP {e.code}") from None


def _guard(a: dict) -> str:
    g = a.get("guard") or {}
    codes = ",".join(g.get("codes") or ())
    verdict = g.get("verdict") or "-"
    return verdict + (f" [{codes}]" if codes else "") + (" rewritten" if g.get("rewritten") else "")


def _beat(a: dict) -> str:
    b = a.get("beat")
    if not b:
        return "-"
    if b.get("skipped"):
        return f"skipped ({b['skipped']})"
    if not b.get("asked"):
        return "-"
    return f"{b.get('want') or '?'}" + (", use" if b.get("use") else "")


def transcript(n: int, his: str, to_name: str, channel: str, res: dict, *,
               prompts: bool = False) -> list[str]:
    out = [f"[{n}] you -> {to_name or channel} ({channel}): {his}"]
    for a in res.get("answers", []):
        p = a.get("prompt") or {}
        size = (f"prompt {len(p['messages'])} msgs ~{p['tokens']} tok" if "messages" in p
                else "prompt: same generation" if p else "prompt: not captured")
        calls = (f"{a['attempts']} call(s), sent #{a['sent_attempt']}"
                 if a.get("attempts") is not None else "calls ?")
        out.append(f"    {a['name']} +{a.get('latency_s')}s | {calls} | {size} | guard "
                   f"{_guard(a)} | beat {_beat(a)}")
        out.extend(f"        {line}" for line in str(a["text"]).splitlines() or [""])
        if prompts and "messages" in p:
            for m in p["messages"]:
                out.append(f"        | {m.get('role')}:")
                out.extend(f"        |   {line}" for line in str(m.get("content", "")).splitlines())
    for o in res.get("others", []):
        out.append(f"    ({o['channel']}) {o['name']} +{o.get('latency_s')}s: {o['text']}")
    for d in res.get("dropped", []):
        codes = ",".join(d.get("codes") or ())
        out.append(f"    dropped: {d.get('name') or d['guid']} ({d.get('kind')}) {d['reason']}"
                   + (f" [{codes}]" if codes else ""))
    for r in res.get("reactions", []):
        out.append(f"    reaction: {r['guid']} {r['emoji']}")
    if not res.get("answers"):
        out.append("    (no answer)")
    if not res.get("settled"):
        out.append(f"    (still going after {res.get('waited_s')} s)")
    return out


def _talk_parser():
    p = argparse.ArgumentParser(
        prog="leeroy_mind sandbox-talk",
        description="A scripted conversation with a sandbox mind: one line of his per line of "
                    "the script (blank lines and lines starting with # are skipped).")
    p.add_argument("--url", required=True, help="the sandbox's URL, as `sandbox` printed it")
    p.add_argument("--guid", type=int, default=None, help="the member (dm and whisper)")
    p.add_argument("--channel", default="dm", choices=CHANNELS)
    p.add_argument("--script", type=Path, required=True)
    p.add_argument("--json", type=Path, default=None, help="save every result here")
    p.add_argument("--wait", type=float, default=120.0, help="seconds per line (1-600)")
    p.add_argument("--reset", action="store_true", help="reset the sandbox first")
    p.add_argument("--prompts", action="store_true", help="print each answer's prompt")
    return p


def sandbox_talk_main(argv=None, *, out=None) -> int:
    args = _talk_parser().parse_args(argv)
    write = out or (lambda s: print(s, flush=True))
    if out is None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.channel in ("dm", "whisper") and args.guid is None:
        print("sandbox-talk: --guid names the member for dm and whisper", file=sys.stderr)
        return 2
    try:
        lines = [line for line in args.script.read_text(encoding="utf-8-sig").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
    except OSError as e:
        print(f"sandbox-talk: cannot read the script: {e.strerror or e}", file=sys.stderr)
        return 2
    client = Client(args.url)
    turns = []
    try:
        if args.reset:
            client.call("POST", "/reset", {}, timeout=300)
        if args.channel != "dm":
            client.call("POST", "/login", {})
        name = ""
        if args.guid is not None:
            members = client.call("GET", "/members")["members"]
            name = next((m["name"] for m in members if m["guid"] == args.guid), "")
        for n, text in enumerate(lines, 1):
            body = {"channel": args.channel, "text": text, "wait": args.wait}
            if args.guid is not None:
                body["to"] = args.guid
            res = client.call("POST", "/say", body, timeout=args.wait + 120)
            turns.append({"text": text, "result": res})
            for line in transcript(n, text, name, args.channel, res, prompts=args.prompts):
                write(line)
    except (RuntimeError, OSError) as e:
        print(f"sandbox-talk: {e}", file=sys.stderr)
        return 1
    finally:
        if args.json is not None:
            args.json.write_bytes(json.dumps(
                {"url": args.url, "channel": args.channel, "guid": args.guid, "turns": turns},
                ensure_ascii=False, indent=1).encode("utf-8"))
    return 0
