"""`eval bank`: a frozen, versioned bank of the guild's own talk with a locked test half
(README.md, "Frozen talk bank"), so a model or prompt change is judged on turns it was not tuned
on.

- **build** reads mind.db read-only and freezes the bake-off's stored inputs: the captured
  requests (`bakeoff_turn`) whose every line is in the Turing corpus, chosen exactly as `bakeoff
  run` chooses them (bakeoff/plan.py), kept where the turn answers his line (the line just
  before it in its conversation is his), the unit the lab's pairwise judge compares. A case keeps
  the generation's first request (before any retry hint), the live model's reply to it (the
  reference), his line and the conversation before it. Whole conversations (turing.corpus: a
  channel in one of his sessions, a Discord channel or group DM; a member's whispers and DMs with
  him together) go to `dev` (70%) or `test` (30%) by a salted hash; the salt is made once per
  build and stored in the manifest.
  Files: `<data>/eval/bank/<version>/manifest.json`, `dev.jsonl`, `test.jsonl`; the same version
  again is refused unless `--force`, which replaces it and keeps its `test-runs.log`.
- **stats** prints counts per split and kind and whether the files still match their digests.
- **run** asks `--model` every case of `--split` again (bakeoff/generate.py: the case's request,
  options and a seed per case and sample; a turn that had a beat live asks its own first) and
  compares each answer with the reference through the lab judge's pairwise interface
  (`judge.prefer`, asked both ways round), printing the win rate with a 95% percentile
  bootstrap CI over conversations. `--split test` needs `--confirm-final` and appends a line
  (id, model, time) to `test-runs.log` before a test case is read.

Nothing here prints or logs a line, a prompt, a name or a reply: versions, kinds, model names,
counts, codes and numbers only."""
import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import statistics
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from leeroy_mind.app import AlreadyRunning
from leeroy_mind.config import load_config
from leeroy_mind.nightly.cli import (_close_client, _data_arg, _exclusive, _identity, _make_client,
                                     _positive, _read_store, _root)
from leeroy_mind.text import strip_wow_markup

from .talk.cli import private_logs

FORMAT = 1
SPLITS = ("dev", "test")
DEV_SHARE = 0.70
SOURCE = "first"        # the generation's first request, as `bakeoff run --from first`
DEFAULT_DAYS = 14       # what the capture keeps (bakeoff.capture.RETENTION_S)
BOOTSTRAP_ROUNDS = 2000
CI_LEVEL = 0.95
MAX_SAMPLES = 5
MANIFEST = "manifest.json"
TEST_LOG = "test-runs.log"
_VERSION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38}[A-Za-z0-9_-])?")


class Refused(Exception):
    """A fixed reason, printed as it is."""


def version_name(text: str) -> str:
    if not _VERSION.fullmatch(text):
        raise argparse.ArgumentTypeError("a version is 1-40 letters, digits, '.', '_' or '-' "
                                         "(not ending in '.')")
    return text


def _samples(text: str) -> int:
    value = _positive(text)
    if value > MAX_SAMPLES:
        raise argparse.ArgumentTypeError(f"samples must be 1-{MAX_SAMPLES}")
    return value


def add_parser(actions) -> None:
    bank = actions.add_parser("bank", help="a frozen bank of stored talk with a locked test half")
    _data_arg(bank, child=True)
    subs = bank.add_subparsers(dest="bank_action", required=True)
    build = subs.add_parser("build", help="freeze the stored turns into a new bank version")
    _data_arg(build, child=True)
    build.add_argument("--since", type=_positive, default=DEFAULT_DAYS, metavar="DAYS")
    build.add_argument("--version", type=version_name, help="default: today's date, YYYYMMDD")
    build.add_argument("--force", action="store_true",
                       help="replace an existing version (its test-run log is kept)")
    stats = subs.add_parser("stats", help="counts per split and kind, no text")
    _data_arg(stats, child=True)
    stats.add_argument("--version", type=version_name, help="default: the newest bank")
    run = subs.add_parser("run", help="ask a model the bank's cases again and judge them pairwise "
                          "against the live replies (the mind stopped, WoW closed)")
    _data_arg(run, child=True)
    run.add_argument("--model", required=True, help="the Ollama model to ask")
    run.add_argument("--split", required=True, choices=SPLITS)
    run.add_argument("--confirm-final", action="store_true",
                     help="required for --split test; the run is recorded in test-runs.log")
    run.add_argument("--version", type=version_name, help="default: the newest bank")
    run.add_argument("--samples", type=_samples, default=1,
                     help=f"answers per case, each with its own seed (1-{MAX_SAMPLES})")


def main(args, clock, *, make_llm=None, make_admin=None, wow_open=None) -> int:
    action = args.bank_action
    try:
        with private_logs():
            root = _root(args)
            if action == "build":
                return _build(root, clock, args)
            if action == "stats":
                return _stats(root, args)
            return _run(root, clock, args, make_llm, make_admin, wow_open)
    except AlreadyRunning:
        print("bank run: the mind is running; stop it first (the run loads models on the GPU "
              "and would unload its live model)", file=sys.stderr)
        return 1
    except Refused as e:
        print(f"bank {action}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"bank {action} interrupted", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"bank {action} failed: code={type(e).__name__}", file=sys.stderr)
        return 1


# -- the split and the statistics -----------------------------------------------------------------

def new_salt() -> str:
    return secrets.token_hex(16)


def split_of(salt: str, conversation: str) -> str:
    """`dev` or `test` for a whole conversation: the salted hash's first 8 bytes as a fraction,
    below DEV_SHARE is dev."""
    digest = hashlib.sha256(f"{salt}\x00{conversation}".encode("utf-8")).digest()
    return "dev" if int.from_bytes(digest[:8], "big") / 2 ** 64 < DEV_SHARE else "test"


def win_rate(groups: dict) -> float | None:
    """The mean share over every decided comparison ({conversation: [share, ...]})."""
    shares = [s for values in groups.values() for s in values]
    return sum(shares) / len(shares) if shares else None


def bootstrap_ci(groups: dict, *, rounds: int = BOOTSTRAP_ROUNDS, seed=0,
                 level: float = CI_LEVEL) -> tuple[float, float] | None:
    """The percentile bootstrap interval of win_rate: each round draws as many conversations as
    there are, with replacement, and pools their comparisons (a conversation's turns are
    correlated, so they are drawn together, never alone). None without a decided comparison."""
    clusters = [(sum(v), len(v)) for v in groups.values() if v]
    if not clusters:
        return None
    rng = random.Random(seed)
    n = len(clusters)
    rates = []
    for _ in range(rounds):
        total = count = 0
        for _ in range(n):
            s, k = clusters[rng.randrange(n)]
            total += s
            count += k
        rates.append(total / count)
    rates.sort()
    tail = int((1 - level) / 2 * rounds)
    return rates[tail], rates[rounds - 1 - tail]


# -- where a bank lives ---------------------------------------------------------------------------

def _inside(parent: Path, name: str) -> Path:
    child = parent / name
    if child.is_symlink() or child.is_junction() or child.resolve().parent != parent.resolve():
        raise Refused("the bank's folders must be plain folders inside the data directory")
    return child


def _banks(root: Path) -> Path:
    return _inside(_inside(root, "eval"), "bank")


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _manifest(path: Path) -> dict:
    data = _read(path / MANIFEST)
    try:
        m = json.loads(data) if data is not None else None
    except ValueError:
        m = None
    if (not isinstance(m, dict) or m.get("format") != FORMAT or not isinstance(m.get("salt"), str)
            or not isinstance(m.get("files"), dict) or not isinstance(m.get("counts"), dict)
            or any(f"{s}.jsonl" not in m["files"] or s not in m["counts"] for s in SPLITS)
            or type(m.get("built")) not in (int, float)):
        raise Refused("the bank's manifest is missing or not one this code reads")
    if m.get("version") != path.name:
        raise Refused("the bank's folder name is not its version")
    return m


def open_bank(root: Path, version: str | None = None) -> tuple[Path, dict]:
    """The version's folder and manifest; without a version, the newest bank built."""
    banks = _banks(root)
    if version is not None:
        path = _inside(banks, version)
        if not path.is_dir():
            raise Refused(f"no bank version {version}")
        return path, _manifest(path)
    found = []
    for path in (sorted(banks.iterdir()) if banks.is_dir() else ()):
        if path.name.startswith(".") or not path.is_dir() or path.is_symlink() or path.is_junction():
            continue
        try:
            m = _manifest(path)
        except Refused:
            continue
        found.append((m["built"], path.name, path, m))
    if not found:
        raise Refused("no bank yet: `eval bank build` makes one")
    _, _, path, m = max(found, key=lambda x: (x[0], x[1]))
    return path, m


def load_cases(path: Path, manifest: dict, split: str) -> list[dict]:
    """A split's cases, refused when its file is not the one the build wrote."""
    name = f"{split}.jsonl"
    data = _read(path / name)
    if data is None or hashlib.sha256(data).hexdigest() != manifest["files"][name]:
        raise Refused(f"the {split} split has changed since it was built; build a new version")
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def logged_runs(path: Path) -> list[dict]:
    """The logged test runs (a rebuild's line is not one)."""
    data = _read(path / TEST_LOG)
    out = []
    for line in (data.decode("utf-8").splitlines() if data else ()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and "model" in entry:
            out.append(entry)
    return out


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_log(path: Path, entry: dict) -> None:
    with _inside(path, TEST_LOG).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


# -- build ----------------------------------------------------------------------------------------

def conversation_of(turn) -> str:
    """The split's unit for a planned turn: its corpus conversation, except that a member's
    whispers and her Discord DMs with him are one private conversation (AGENTS.md, Privacy), so
    they never fall in different splits."""
    if turn.family == "private":
        return f"private:{turn.captured.guid}"
    return turn.lines[0].conv


def freeze(store, cfg, *, since: float, until: float) -> list[dict]:
    """The cases of the window, oldest turn first (the module docstring)."""
    from leeroy_mind.bakeoff import plan as planning
    from leeroy_mind.personas.io import PersonaCache
    from leeroy_mind.talk.romance import flirt_chance
    from leeroy_mind.turing.corpus import read_lines
    ident = _identity(cfg, store)
    lines = read_lines(store.conn, human=ident.guid, start=since, end=until)
    captured = planning.read_captured(store.conn, since=since, until=until)
    built = planning.build(captured, lines, human=ident.guid, turns=max(1, len(captured)),
                           windows=0, kinds=set(planning.KINDS), seed="bank", since=since,
                           until=until)
    personas = PersonaCache(store)
    out = []
    for t in sorted(built.turns.values(), key=lambda t: t.id):
        if not t.answers:
            continue
        c, first = t.captured, t.captured.first
        p = personas.get(c.guid)
        out.append({
            "id": f"turn:{c.id}", "source": "turn", "turn": c.id, "kind": t.kind,
            "family": t.family, "conversation": conversation_of(t), "ts": c.ts, "guid": c.guid,
            "channel": c.channel, "turn_kind": c.turn_kind, "beat": c.beat, "want": c.want,
            "shape": c.shape,
            "request": {"options": first.options, "messages": first.messages},
            "reference": {"model": first.model, "text": first.reply,
                          "done_reason": first.done_reason},
            "his": t.context[-1].text,
            "context": [["him" if x.human else "her", x.text] for x in t.context[:-1]],
            # what a beat needs (bakeoff/cli.py _who), so a run reads nothing but the bank
            "who": {"human": ident.called, "their": ident.forms[2],
                    "can_flirt": p is not None and flirt_chance(p, 1.0) > 0,
                    "speaker": p.character_name if p is not None else ""}})
    return out


def _counts(rows: list[dict]) -> dict:
    return {"cases": len(rows), "conversations": len({r["conversation"] for r in rows}),
            "kinds": dict(sorted(Counter(r["kind"] for r in rows).items())),
            "models": dict(sorted(Counter(r["reference"]["model"] for r in rows).items()))}


def _jsonl(rows: list[dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode("utf-8")


def _build(root: Path, clock, args) -> int:
    from leeroy_mind.bakeoff.generate import BEAT_PREDICT
    cfg = load_config(root / "mind.toml")
    version = args.version or datetime.fromtimestamp(clock.now()).strftime("%Y%m%d")
    banks = _banks(root)
    target = _inside(banks, version)
    if target.exists() and not args.force:
        raise Refused(f"version {version} exists; --force replaces it (its test-run log is kept)")
    until = clock.now()
    since = until - args.since * 86400
    with _read_store(root / "mind.db", clock) as store:
        rows = freeze(store, cfg, since=since, until=until)
    if not rows:
        raise Refused("nothing to freeze: no captured turn in the window answers his line "
                      "(turn on [bakeoff] capture and let the guild talk first)")
    salt = new_salt()
    split = {s: [] for s in SPLITS}
    for row in rows:
        split[split_of(salt, row["conversation"])].append(row)
    files = {f"{s}.jsonl": _jsonl(split[s]) for s in SPLITS}
    manifest = {"format": FORMAT, "version": version, "built": until, "since": since,
                "until": until, "days": args.since, "salt": salt, "dev_share": DEV_SHARE,
                "source": SOURCE,
                "beat_predict": getattr(cfg.talk, "think_budget", None) or BEAT_PREDICT,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                "counts": {s: _counts(split[s]) for s in SPLITS}}
    _publish(banks, target, files, manifest, clock)
    d, t = manifest["counts"]["dev"], manifest["counts"]["test"]
    print(f"bank {version}: froze {len(rows)} cases in {d['conversations'] + t['conversations']} "
          f"conversations (dev {d['cases']} cases in {d['conversations']}, test {t['cases']} in "
          f"{t['conversations']}) under eval/bank/{version}")
    return 0


def _publish(banks: Path, target: Path, files: dict, manifest: dict, clock) -> None:
    """Write the version beside its place and move it in whole; a version it replaces gives it
    its test-run log, with a line saying it was rebuilt."""
    banks.parent.mkdir(exist_ok=True)
    banks.mkdir(exist_ok=True)
    _banks(banks.parent.parent)  # still plain folders once made
    tag = uuid.uuid4().hex[:8]
    work = _inside(banks, f".{target.name}.{tag}.partial")
    work.mkdir()
    try:
        for name, data in files.items():
            (work / name).write_bytes(data)
        (work / MANIFEST).write_bytes(
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        if not target.exists():
            work.rename(target)
            return
        log = _read(target / TEST_LOG)
        if log:
            (work / TEST_LOG).write_bytes(log)
            _append_log(work, {"event": "rebuilt", "id": uuid.uuid4().hex[:12],
                               "time": _utc(clock.now())})
        retired = _inside(banks, f".{target.name}.{tag}.old")
        target.rename(retired)
        try:
            work.rename(target)
        except BaseException:
            retired.rename(target)
            raise
        shutil.rmtree(retired, ignore_errors=True)
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


# -- stats ----------------------------------------------------------------------------------------

def _stats(root: Path, args) -> int:
    path, m = open_bank(root, args.version)
    built = datetime.fromtimestamp(m["built"]).strftime("%Y-%m-%d %H:%M")
    print(f"bank {m['version']} (format {m['format']}): built {built}, {m.get('days')} days of "
          f"stored talk, dev share {m.get('dev_share', DEV_SHARE):.2f}, {m.get('source', SOURCE)} "
          "requests")
    for s in SPLITS:
        n = m["counts"][s]
        kinds = ", ".join(f"{k} {v}" for k, v in n.get("kinds", {}).items()) or "none"
        print(f"{s}: {n['cases']} cases, {n['conversations']} conversations; kinds: {kinds}")
    print("reference models: " + "; ".join(
        f"{s}: " + (", ".join(f"{k} {v}" for k, v in m["counts"][s].get("models", {}).items())
                    or "none") for s in SPLITS))
    changed = [s for s in SPLITS
               if (data := _read(path / f"{s}.jsonl")) is None
               or hashlib.sha256(data).hexdigest() != m["files"][f"{s}.jsonl"]]
    print("files: intact" if not changed else f"files: changed since the build ({', '.join(changed)})")
    print(f"test runs: {len(logged_runs(path))}")
    return 0


# -- run ------------------------------------------------------------------------------------------

def _run(root: Path, clock, args, make_llm, make_admin, wow_open) -> int:
    from leeroy_mind.bakeoff.cli import wow_running
    from leeroy_mind.gpucontrol import KEY as GPU_PAUSED
    if args.split == "test" and not args.confirm_final:
        raise Refused("the test split is locked: add --confirm-final for the final run, which "
                      f"{TEST_LOG} records")
    path, manifest = open_bank(root, args.version)
    cfg = load_config(root / "mind.toml")
    state = (wow_open or wow_running)()
    if state is None:
        raise Refused("cannot tell whether WoW is open; the run takes the GPU, so it waits")
    if state:
        raise Refused("WoW is open: the run takes the GPU; close WoW first")
    with _exclusive(root, gpu=True):
        with _read_store(root / "mind.db", clock) as store:
            if store.get_meta(GPU_PAUSED) is not None:
                raise Refused("the GPU is paused from the console (Free GPU): resume it first")
        units, logged = asyncio.run(_go(path, manifest, cfg, clock, args, make_llm, make_admin))
    _report(units, manifest, args, logged)
    return 0


async def _go(path, manifest, cfg, clock, args, make_llm, make_admin):
    from leeroy_mind.bakeoff.cli import _has, _make_room, _unload
    llm = _make_client(cfg, make_llm)
    if make_admin is not None:
        admin = make_admin(cfg)
    else:
        from leeroy_mind.nightly.gpu import HttpOllamaAdmin
        admin = HttpOllamaAdmin(cfg.ollama.url)
    used: list[str] = []
    try:
        try:
            pulled = await llm.list_models()
        except Exception as e:
            raise Refused(f"cannot ask Ollama which models are pulled ({type(e).__name__})") from None
        for name, what in ((args.model, "the model"),
                           (cfg.models.reflection, "the reflection model (the judge)")):
            if not _has(pulled, name):
                raise Refused(f"{what} {name} is not pulled; a run never pulls one")
        logged = None
        if args.split == "test":  # recorded before a single test case is read
            logged = uuid.uuid4().hex[:12]
            _append_log(path, {"id": logged, "model": args.model, "time": _utc(clock.now())})
        cases = load_cases(path, manifest, args.split)
        await _make_room(admin, cfg.models.embedding)
        used.append(args.model)
        replies = await _answers(llm, cases, args.model, manifest, args.samples)
        await _unload(admin, args.model)
        used.append(cfg.models.reflection)
        units = await _judged(llm, cfg, cases, replies, manifest, args.samples)
        await _unload(admin, cfg.models.reflection)
        return units, logged
    finally:
        for model in dict.fromkeys(used):
            await _unload(admin, model)
        await _close_client(llm)


def _turn(case: dict):
    """The case as bakeoff/generate.py reads a planned turn: its first request (without a stored
    seed: each case and sample gets its own) and the shape that cuts a long Discord reply."""
    from leeroy_mind.bakeoff.plan import Captured, Request
    ref = case["reference"]
    options = {k: v for k, v in case["request"]["options"].items() if k != "seed"}
    first = Request(ref["model"], False, options, case["request"]["messages"], ref["text"],
                    ref["done_reason"])
    captured = Captured(case["turn"], case["ts"], (), case["guid"], case["channel"],
                        case["turn_kind"], 1, 0, first, first, case["shape"], case["beat"],
                        case["want"])
    return SimpleNamespace(id=case["turn"], captured=captured)


def _seed(manifest: dict, sample: int) -> str:
    return f"bank|{manifest['version']}|{sample}"


async def _answers(llm, cases, model, manifest, samples) -> dict:
    """{(case id, sample): bakeoff/generate.py's result} with think false, as live sends it."""
    from leeroy_mind.bakeoff import generate
    from leeroy_mind.bakeoff.config import Candidate
    plain, beat = Candidate("bank", model), Candidate("bank", model, beat=True)
    out = {}
    for case in cases:
        turn = _turn(case)
        who = dict(case["who"], think_budget=manifest["beat_predict"])
        for sample in range(samples):
            out[(case["id"], sample)] = await generate.ask(
                llm, beat if case["beat"] else plain, turn, _seed(manifest, sample), SOURCE,
                lambda _turn, who=who: who)
    return out


def given(text: str | None) -> str | None:
    """A reply as the judge reads it, or None when none was given (empty, or a silence)."""
    from leeroy_mind.talk.filters import is_silence
    if text is None or not text.strip() or is_silence(text):
        return None
    return text.strip()


def _plain(text: str) -> str:
    return " ".join(strip_wow_markup(text).split())


async def compare(reasoner, his: str, candidate: str | None, reference: str | None, context=(),
                  *, seed) -> float | None:
    """The candidate's share against the reference (lab/judge.py prefer: asked both ways round;
    None when neither call answered). A reply not given loses to one given, as the lab judge
    scores a reply she never gave lowest; neither given is a tie, without a call."""
    from leeroy_mind.lab import judge
    if candidate is None or reference is None:
        return 0.5 if candidate is None and reference is None else float(reference is None)
    return await judge.prefer(reasoner, (his, candidate, context), (his, reference, context),
                              seed=seed)


async def _judged(llm, cfg, cases, replies, manifest, samples) -> list[dict]:
    from leeroy_mind.bakeoff.generate import trimmed
    from leeroy_mind.lab import judge
    from leeroy_mind.llm.client import LLMError
    from leeroy_mind.nightly.llm import Reasoner
    reasoner = Reasoner(llm, cfg.models.reflection, parallel=1)
    units = []
    for case in cases:
        ref = case["reference"]
        reference = given(trimmed(ref["text"], words=case["shape"].get("words"),
                                  done_reason=ref["done_reason"]))
        his = _plain(case["his"])
        context = tuple((who, _plain(text)) for who, text in case["context"])
        for sample in range(samples):
            r = replies[(case["id"], sample)]
            candidate = given(r["text"]) if r["status"] == "ok" else None
            status = r["status"] if r["status"] == "error" or candidate is not None else "silent"
            judge_error = False
            try:
                share = await compare(reasoner, his, candidate, reference, context,
                                      seed=judge.seed_of(f"bank|{manifest['version']}", case["id"], sample))
            except LLMError:
                share, judge_error = None, True
            units.append({"conversation": case["conversation"], "kind": case["kind"],
                          "status": status, "share": share, "judge_error": judge_error,
                          "words": _words(candidate), "reference_words": _words(reference)})
    return units


def _words(text: str | None) -> int | None:
    return None if text is None else len(text.split())


def _median(values) -> str:
    values = [v for v in values if v is not None]
    return f"{statistics.median(values):g}" if values else "none"



def _line(units: list[dict], seed) -> str:
    groups = defaultdict(list)
    for u in units:
        if u["share"] is not None:
            groups[u["conversation"]].append(u["share"])
    rate, ci = win_rate(groups), bootstrap_ci(groups, seed=seed)
    decided = sum(len(v) for v in groups.values())
    if rate is None:
        return f"win_rate=none decided=0 conversations={len({u['conversation'] for u in units})}"
    return (f"win_rate={rate:.4f} ci95_low={ci[0]:.4f} ci95_high={ci[1]:.4f} decided={decided} "
            f"conversations={len(groups)}")


def _report(units: list[dict], manifest: dict, args, logged: str | None) -> None:
    from leeroy_mind.lab import judge
    status = Counter(u["status"] for u in units)
    cases = len(units) // args.samples
    base = f"bank|{manifest['version']}"
    print(f"bank run: version={manifest['version']} split={args.split} model={args.model} "
          f"cases={cases} conversations={len({u['conversation'] for u in units})} "
          f"samples={args.samples}")
    # the judge's same-line preference leans to the shorter reply (lab/README.md): lengths beside it
    print(f"answers: ok={status['ok']} silent={status['silent']} errors={status['error']} "
          f"median_words={_median(u['words'] for u in units)} "
          f"reference_median_words={_median(u['reference_words'] for u in units)}")
    undecided = sum(u["share"] is None for u in units)
    print(_line(units, judge.seed_of(base, "ci", args.split, args.model)) + f" undecided={undecided} "
          f"judge_errors={sum(u['judge_error'] for u in units)}")
    for kind in sorted({u["kind"] for u in units}):
        mine = [u for u in units if u["kind"] == kind]
        print(f"kind={kind} " + _line(mine, judge.seed_of(base, "ci", args.split, args.model, kind)))
    if logged is not None:
        print(f"test_run={logged} (recorded in {TEST_LOG})")
