import argparse
import logging
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path

from .clock import SystemClock
from .config import default_data_dir, write_default_config

DATA_SUBDIRS = ("personas", "prompts", "logs", "snapshots")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DOCTOR_PROMPT = "Say hello to the guild in a few words."
DOCTOR_CHAT_S = 180.0  # the live model may have to load first
BRIDGE_PROBE_S = 2.0
log = logging.getLogger("leeroy_mind")


def _root(args) -> Path:
    """The data dir, absolute: a read-only sqlite URI needs one (relative --data-dir)."""
    return (Path(args.data_dir).resolve() if getattr(args, "data_dir", None)
            else default_data_dir())


def _data_dir_help() -> str:
    where = str(default_data_dir()).replace("%", "%%")
    return ("the mind's data dir, with mind.toml and mind.db (default: $LEEROY_MIND_DATA, "
            f"else the mind-data folder beside this code, here {where})")


def _add_data_dir(parser, *, after_action: bool = False) -> None:
    """--data-dir on a command, and again on each of its actions (after_action), so it can go
    before or after the action; an action's own default never hides the command's value."""
    parser.add_argument("--data-dir", default=argparse.SUPPRESS if after_action else None,
                        help=_data_dir_help())


def _data_dir_problem(root: Path) -> tuple[str | None, int]:
    """Why `run` and `doctor` will not use this data dir (None: they will), and its persona
    count. A wrong dir (a worktree's own mind-data, a typo) would become a new, empty mind
    that drains the bridge's backlog in silence: only `init` and the roster make a mind. The
    files are opened, not stat'ed, so the tests' guard sees every look at the live one."""
    try:
        (root / "mind.toml").read_bytes()
    except FileNotFoundError:
        return f"no mind.toml in {root}", 0
    except OSError as e:
        return f"cannot read {root / 'mind.toml'}: {e}", 0
    path = root / "mind.db"
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return f"no mind.db in {root}", 0
    try:
        n = conn.execute("SELECT count(*) FROM persona_core").fetchone()[0]
    except sqlite3.Error as e:
        return f"{path} cannot be read: {e}", 0
    finally:
        conn.close()
    if not n:
        return f"no personas in {path}: seed and generate the cast first", 0
    return None, n


def _refuse(cmd: str, problem: str) -> None:
    print(f"{cmd}: {problem}; pass --data-dir with the mind's data dir (or set "
          "LEEROY_MIND_DATA)", file=sys.stderr)


def _default_llm(cfg):
    from .llm.client import OllamaClient
    return OllamaClient(cfg.ollama.url)


def _snapshot_file(db: Path, dest: Path, label: str) -> Path:
    """A snapshot of mind.db as it is on disk: no migration first (it may be the backup the
    migration needs)."""
    from .storage.db import snapshot
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return snapshot(conn, dest, label)
    finally:
        conn.close()


async def serve(root: Path, *, make_llm=None, ollama_conf: Path | None = None,
                ready=None) -> int:
    """The `run` command inside asyncio.run: a "start" snapshot, the app, its startup checks,
    then MindApp.run() until Ctrl+C or stop(). `ready(app)` is told about the app (tests)."""
    from .app import MindApp, startup_checks
    from .config import ConfigError, load_config
    from .storage.db import EmbeddingMismatch
    if (root / "mind.db").exists():
        _snapshot_file(root / "mind.db", root / "snapshots", "start")
    try:
        cfg = load_config(root / "mind.toml")
    except ConfigError as e:
        log.error("config_invalid: %s", e)
        return 1
    app = MindApp(cfg, root, llm=(make_llm or _default_llm)(cfg))
    if ready is not None:
        ready(app)
    try:
        try:
            await startup_checks(app, ollama_conf=ollama_conf)
        except EmbeddingMismatch as e:
            log.error("embedding_mismatch: %s", e)
            return 1
        await app.run()
        return 0
    finally:
        await app.stop()  # a Ctrl+C before run() still closes the db (a no-op after run())


def cmd_run(args) -> int:
    import asyncio
    from .app import AlreadyRunning, acquire_instance_lock
    from .logs import setup_logging
    root = _root(args)
    problem, personas = _data_dir_problem(root)
    if problem:
        _refuse("run", problem)
        return 1
    try:
        lock = acquire_instance_lock(root)
    except AlreadyRunning:
        print("mind service is running")
        return 1
    with lock:
        print(f"mind: data dir {root}, {personas} personas, log {root / 'logs' / 'mind.log'}; "
              "Ctrl+C stops it", flush=True)
        teardown = setup_logging(root / "logs", level=args.log_level)
        try:
            return asyncio.run(serve(root, make_llm=args.make_llm, ollama_conf=args.ollama_conf))
        except KeyboardInterrupt:  # Ctrl+C: run() has already shut the app down
            return 0
        except Exception:
            log.exception("mind_failed")
            return 1
        finally:
            teardown()


async def doctor(root: Path, *, make_llm=None, ollama_conf: Path | None = None
                 ) -> list[tuple[bool | None, str]]:
    """One (ok, text) per check; ok None is a note, which never fails the doctor (no
    library.db). Reads mind.db and library.db read-only and changes nothing; the bridge check
    opens a TCP connection and closes it without reading, and is skipped while the mind
    service holds the bridge (pre-flight ruling 21)."""
    import socket
    import sqlite_vec
    from .app import check_ollama_chat, has_model, instance_lock_held
    from .config import ConfigError, load_config
    from .llm.client import LLMError
    from .llm.presets import LIVE
    from .storage.db import VEC_DIM, latest_version
    out: list[tuple[bool | None, str]] = []
    v = sys.version_info
    out.append((v >= (3, 14), f"python {v.major}.{v.minor}.{v.micro} (3.14 or later)"))
    with closing(sqlite3.connect(":memory:")) as c:
        try:
            c.enable_load_extension(True)
            sqlite_vec.load(c)
            out.append((True, f"sqlite-vec {c.execute('select vec_version()').fetchone()[0]}"))
        except Exception as e:
            out.append((False, f"sqlite-vec does not load: {e}"))
        try:
            c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            out.append((True, f"FTS5 (sqlite {sqlite3.sqlite_version})"))
        except sqlite3.Error as e:
            out.append((False, f"FTS5 is missing: {e}"))
    try:
        cfg = load_config(root / "mind.toml")
        out.append((True, f"{root / 'mind.toml'} valid"))
    except ConfigError as e:
        cfg = None
        out.append((False, f"mind.toml: {e}"))
    out += _doctor_db(root, latest_version(), cfg)
    out.append(_doctor_library(root, cfg))
    out += _doctor_overlays(root)
    if cfg is None:
        out += [(False, f"{what}: not checked, mind.toml is invalid")
                for what in ("ollama", "models", "live chat", "embedding", "mod-ollama-chat",
                             "bridge")]
        return out
    out += _doctor_voice_service(root)
    llm = (make_llm or _default_llm)(cfg)
    try:
        names = await llm.list_models()
        out.append((True, f"ollama reachable at {cfg.ollama.url} ({len(names)} models)"))
    except LLMError as e:
        names = None
        out.append((False, f"ollama not reachable at {cfg.ollama.url}: {e}"))
    if names is None:
        out += [(False, f"{what}: not checked, Ollama is not reachable")
                for what in ("models", "live chat", "embedding")]
    else:
        m = cfg.models
        missing = [x for x in (m.live, m.embedding, m.reflection) if not has_model(names, x)]
        fallback = ("pulled" if has_model(names, m.live_fallback) else "not pulled")
        out.append((not missing, (f"models missing: {', '.join(missing)}" if missing else
                                  "models live, embedding and reflection pulled")
                    + f"; live_fallback {fallback}"))
        try:
            reply = await llm.chat(m.live, [{"role": "user", "content": DOCTOR_PROMPT}],
                                   options=dict(LIVE.options), think=False, timeout=DOCTOR_CHAT_S)
            text = reply.text.strip()  # ascii(): a cp1252 pipe cannot print every reply
            out.append((bool(text), f"live chat with think:false: {ascii(text[:40])}" if text else
                        "live chat with think:false came back empty: the model thought anyway"))
        except LLMError as e:
            out.append((False, f"live chat on {m.live} failed: {e}"))
        try:
            vec = (await llm.embed(m.embedding, ["doctor"], dimensions=VEC_DIM))[0]
            out.append((len(vec) == VEC_DIM, f"embedding {m.embedding}: {len(vec)} dimensions"))
        except LLMError as e:
            out.append((False, f"embedding on {m.embedding} failed: {e}"))
    warnings = check_ollama_chat(ollama_conf, cfg)
    out.append((not warnings, "; ".join(warnings) if warnings else
                f"mod-ollama-chat uses the live model with num_ctx {LIVE.options['num_ctx']}"))
    where = f"{cfg.bridge.host}:{cfg.bridge.port}"
    if instance_lock_held(root):
        out.append((True, f"bridge {where}: skipped, the mind service holds the connection"))
    else:
        try:
            socket.create_connection((cfg.bridge.host, cfg.bridge.port),
                                     timeout=BRIDGE_PROBE_S).close()
            out.append((True, f"bridge {where} reachable"))
        except OSError as e:
            out.append((False, f"bridge {where} not reachable: {e.__class__.__name__}"))
    return out


def _doctor_db(root: Path, latest: int, cfg=None) -> list[tuple[bool | None, str]]:
    """The schema version, then the personas and their mix, then the women's notes toward
    the human against their attitude (personas/notes.py), read-only."""
    from .personas.generate import mix_report
    from .storage.store import Store
    path = root / "mind.db"
    if not path.exists():
        return [(False, f"no mind.db at {path}"), (False, "personas: no mind.db")]
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        have = conn.execute("PRAGMA user_version").fetchone()[0]
        lines = [(have == latest, f"mind.db schema v{have}" + (
            "" if have == latest else f", this code is v{latest}: run `db migrate`"))]
        if have > latest or have < 1:
            return lines + [(False, "personas: not read, the schema is not this code's")]
        store = Store(conn, clock=SystemClock())
        try:
            human = _human_guid(store, None)
        except SystemExit:
            human = None
        personas = _cast_personas(store, human)
        lines.append((bool(personas), f"personas {len(personas)}: "
                      + _mix_line(mix_report(personas)).replace("\n", "; ")))
        lines.append(_doctor_voice_specs(root, personas))
        if human is not None:
            lines += _doctor_notes(store, personas, human, cfg)
        return lines
    except sqlite3.Error as e:
        return [(False, f"mind.db cannot be read: {e}"), (False, "personas: not read")]
    finally:
        conn.close()


def _doctor_voice_specs(root: Path, personas) -> tuple[bool | None, str]:
    """Count the cast's readable spec files; an incomplete casting is informational."""
    from .voice.brief import load_specs
    guids = {p.guid for p in personas}
    try:
        count = len(guids.intersection(load_specs(root)))
    except OSError as e:
        return None, f"voice specs: not read ({type(e).__name__})"
    return (True if guids and count == len(guids) else None,
            f"voice specs: {count} of {len(guids)} cast members")


def _doctor_voice_service(root: Path) -> list[tuple[bool | None, str]]:
    """Probe without a protocol greeting, autostart or model loading. Voice is optional.

    Use the voice service's configured endpoint when its data directory has voice.toml;
    report a mismatch with the mind's endpoint rather than silently probing a default port.
    """
    import socket
    import tomllib
    from .config import ConfigError, _HOSTS
    from .voice.attach import PROBE_S, voice_data_dir
    from .voice.config import load_voice_config

    try:
        cfg = load_voice_config(root / "mind.toml")
    except (ConfigError, TypeError) as e:
        return [(None, f"voice service: not checked ({type(e).__name__})")]
    if not cfg.enabled:
        return [(None, "voice service: disabled")]
    host, port = cfg.host, cfg.port
    path = voice_data_dir() / "voice.toml"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        pass  # The mind's endpoint still lets doctor report a separately managed service.
    except OSError as e:
        return [(None, f"voice service: configuration not read ({type(e).__name__})")]
    else:
        try:
            table = tomllib.loads(raw.decode("utf-8-sig")).get("mind", {})
            if not isinstance(table, dict):
                raise ValueError("mind table")
            host, port = table.get("host", "127.0.0.1"), table.get("port", 7879)
            if not isinstance(host, str) or host not in _HOSTS:
                raise ValueError("host")
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("port")
        except (UnicodeDecodeError, ValueError) as e:
            return [(None, f"voice service: invalid configuration ({type(e).__name__})")]
    lines = []
    if (host, port) != (cfg.host, cfg.port):
        lines.append((None, f"voice endpoint mismatch: mind uses {cfg.host}:{cfg.port}; "
                           f"service uses {host}:{port}"))
    try:
        socket.create_connection((host, port), timeout=PROBE_S).close()
    except OSError:
        lines.append((None, f"voice service: not running on {host}:{port}"))
    else:
        lines.append((True, f"voice service: listening on {host}:{port}"))
    return lines


def _doctor_notes(store, personas, human_guid: int, cfg) -> list[tuple[bool | None, str]]:
    """A lust or drawn woman's note toward him that denies it fails, a fond one's is a note
    (fix wave G2). His names come from mind.toml when it is valid, else the roster's."""
    from .human import HumanIdentity, human_identity
    from .personas.notes import cold_notes, doctor_lines
    from .personas.stances import is_woman
    if cfg is not None:
        human = human_identity(cfg, store, human_guid)
    else:
        m = store.cast().get(human_guid)
        human = HumanIdentity(human_guid, m.name if m else "", "", "", True)
    women = sum(is_woman(p) and p.romance.stance != "none" for p in personas)
    return [(ok, _ascii(text)) for ok, text in
            doctor_lines(cold_notes(store, personas, human), human.called, women)]


def _doctor_overlays(root: Path) -> list[tuple[bool, str]]:
    """One note per .txt file in mind-data/prompts (prompts lens, minor 6): an overlay's size in
    estimated tokens against the cap its prompt cuts it to (talk.romance, personas.generate),
    saying so when it is over (its end is cut) or blank (not used), when it is not UTF-8 (read
    as UTF-16, or characters that could not be read), and a file no prompt reads; one that
    cannot be read fails. Names and sizes only, never the text; no files, no lines."""
    from .personas.generate import OVERLAY_TOKEN_CAP
    from .prompts import read_overlay
    from .talk.romance import OVERLAY_CAPS
    from .text import estimate_tokens
    caps = dict(OVERLAY_CAPS)
    caps["persona_rules"] = OVERLAY_TOKEN_CAP
    folder = root / "prompts"
    out = []
    for path in sorted(folder.glob("*.txt")) if folder.is_dir() else ():
        cap = caps.get(path.stem)
        if cap is None:
            known = ", ".join(f"{name}.txt" for name in sorted(caps))
            out.append((True, f"prompt overlay {path.name}: no prompt reads it ({known})"))
            continue
        try:
            found = read_overlay(path.stem, root)
        except OSError as e:  # its prompt would fail the same way
            out.append((False, f"prompt overlay {path.stem}: cannot be read ({e})"))
            continue
        tokens = estimate_tokens(found.text) if found is not None else 0
        size = ("blank: not used" if not tokens else
                f"{tokens} tokens, over its cap of {cap}: the end is cut" if tokens > cap else
                f"{tokens} tokens (cap {cap})")
        how = []
        if found is not None and found.encoding != "utf-8":
            how.append(f"read as {found.encoding.upper()}")
        if found is not None and found.replaced:
            how.append(f"{found.replaced} characters could not be read (save it as UTF-8)")
        out.append((True, f"prompt overlay {path.stem}: {size}"
                    + (f"; not UTF-8: {', '.join(how)}" if how else "")))
    return out


def _doctor_library(root: Path, cfg) -> tuple[bool | None, str]:
    """library.db read-only: its chunks, how many are embedded and with which model. None (a
    note, never a FAIL) when there is none: talk runs without facts (fix wave part E)."""
    from .library.db import latest_version as library_version
    path = root / "library.db"
    if not path.exists():
        return None, f"library: no library.db at {path}; talk runs without facts"
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return False, f"library.db cannot be read: {e}"
    try:
        have = conn.execute("PRAGMA user_version").fetchone()[0]
        if have != library_version():
            return False, (f"library.db is schema v{have}, this code reads v{library_version()}"
                           ": migrate it with the library CLI")
        n, embedded = conn.execute("SELECT count(*), count(embedding) FROM chunks").fetchone()
        row = conn.execute("SELECT value FROM meta WHERE key = 'embed_model'").fetchone()
    except sqlite3.Error as e:
        return False, f"library.db cannot be read: {e}"
    finally:
        conn.close()
    model = row[0] if row else None
    text = f"library: {n} chunks, embedded {embedded}, model {model or 'none'}"
    if cfg is not None and model is not None and model != cfg.models.embedding:
        text += f" (the mind embeds with {cfg.models.embedding}: text search only)"
    return True, text


DOCTOR_MARK = {True: "ok  ", False: "FAIL", None: "note"}


def cmd_doctor(args) -> int:
    import asyncio
    root = _root(args)
    problem, _ = _data_dir_problem(root)
    if problem:
        print(f"FAIL data dir {root}: {problem}")
        _refuse("doctor", problem)
        return 1
    lines = asyncio.run(doctor(root, make_llm=args.make_llm, ollama_conf=args.ollama_conf))
    for ok, text in lines:
        print(f"{DOCTOR_MARK[ok]} {text}")
    return 0 if all(ok is not False for ok, _ in lines) else 1


def cmd_db(args) -> int:
    from .app import AlreadyRunning, acquire_instance_lock
    from .storage.db import checkpoint, open_db
    root = _root(args)
    path = root / "mind.db"
    if args.action == "snapshot":
        if not path.exists():
            print(f"no mind.db at {path}", file=sys.stderr)
            return 1
        print(f"snapshot {_snapshot_file(path, root / 'snapshots', args.label)}")
        return 0
    try:  # opened, not stat'ed (the tests' guard): migrate never makes a new mind.db
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True).close()
    except sqlite3.Error:
        _refuse("db migrate", f"no mind.db in {root}")
        return 1
    try:
        lock = acquire_instance_lock(root)
    except AlreadyRunning:
        print("mind service is running")
        return 1
    with lock:
        if path.exists():
            print(f"snapshot {_snapshot_file(path, root / 'snapshots', 'pre-migrate')}")
        conn = open_db(path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            checkpoint(conn)
        finally:
            conn.close()
    print(f"mind.db is at schema v{version}")
    return 0


class _NoLLM:
    """The model client of a command that never needs one (keyed memory writes never embed)."""

    async def chat(self, *a, **k):
        from .llm.client import LLMError
        raise LLMError("this command does not use the model")

    async def embed(self, *a, **k):
        from .llm.client import LLMError
        raise LLMError("this command does not use the model")

    async def list_models(self):
        return set()


def cmd_memories(args) -> int:
    """`memories seed FILE`: the owner's own memories of the guild's history, as data
    (memory/owner_seed.py). The dry run reads mind.db read-only; the real run takes the
    service's lock and a snapshot first. Nothing prints a memory's text."""
    from .config import ConfigError
    from .memory import owner_seed as S
    root = _root(args)
    problem, _ = _data_dir_problem(root)
    if problem:
        _refuse("memories seed", problem)
        return 1
    try:
        fiction_now = S.parse_fiction_now(args.fiction_now)
        entries = S.read_seed_file(Path(args.file).read_text(encoding="utf-8"))
        cfg = _load_cfg(root)
    except (OSError, ValueError, ConfigError) as e:
        print(f"memories seed: {e}", file=sys.stderr)
        return 1
    if args.dry_run:
        from .storage.store import Store
        conn = sqlite3.connect(f"{(root / 'mind.db').resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return _seed_memories(args, Store(conn, clock=SystemClock()), cfg, entries,
                                  fiction_now)
        finally:
            conn.close()
    from .app import AlreadyRunning, acquire_instance_lock
    from .storage.db import open_db
    from .storage.store import Store
    try:
        lock = acquire_instance_lock(root)
    except AlreadyRunning:
        print("mind service is running")
        return 1
    with lock:
        print(f"snapshot {_snapshot_file(root / 'mind.db', root / 'snapshots', 'pre-memories')}")
        store = Store(open_db(root / "mind.db"), clock=SystemClock())
        try:
            return _seed_memories(args, store, cfg, entries, fiction_now)
        finally:
            store.close()


def _seed_memories(args, store, cfg, entries, fiction_now) -> int:
    import asyncio
    from datetime import date
    from .human import human_identity
    from .llm.scheduler import LLMScheduler
    from .memory import owner_seed as S
    from .memory.writer import Embedder, MemoryWriter
    human = human_identity(cfg, store, _human_guid(store, None))
    plan = S.plan_owner_memories(entries, store=store, human=human, fiction_now=fiction_now,
                                 now=store.clock.now())
    print(f"owner memories in {args.file}: {len(entries)} entries; the fiction's "
          f"{fiction_now.isoformat()} is {date.fromtimestamp(plan.anchor).isoformat()}")
    for line in S.summary(plan, store.cast()):
        print(line)
    for r in plan.redated:  # entries passed the checks: guid and moment are safe to print
        stored = store.keyed_memory(r.guid, r.key)
        if stored is None or stored.t_event != r.t_event:  # a re-run's quiet once it is done
            print(f"redate entry {r.index} (guid {r.guid}, moment {r.moment}): its text says "
                  f"{r.said}, so {r.new} (was {r.old})")
    for r in plan.rejects:
        guid = r.guid if isinstance(r.guid, int) else "?"
        moment = r.moment if isinstance(r.moment, str) and S._MOMENT.fullmatch(r.moment) else "?"
        print(f"reject entry {r.index} (guid {guid}, moment {moment}): {r.reason}")
    if args.dry_run:
        live = [c for c in plan.candidates if not S.is_retired(store, c)]
        same = sum(S.is_unchanged(store, c, human) for c in live)
        known = sum(store.keyed_memory(c.owner, c.key) is not None for c in live)
        print(f"would write {len(live) - same}: new {len(live) - known}, "
              f"updated {known - same}, unchanged {same}, "
              f"retired {len(plan.candidates) - len(live)}; rejected {len(plan.rejects)}; "
              "nothing was written")
        return 1 if plan.rejects else 0
    writer = MemoryWriter(store, Embedder(LLMScheduler(_NoLLM(), clock=store.clock),
                                          cfg.models.embedding), store.clock,
                          human=lambda: human)
    res = asyncio.run(S.write_owner_memories(plan, writer, store, human))
    print(f"wrote {res.new + res.updated}: new {res.new}, updated {res.updated}, unchanged "
          f"{res.unchanged}, retired {res.retired}; rejected {len(plan.rejects)}"
          + (f"; failed {res.failed}" if res.failed else ""))
    if res.new + res.updated:
        print("the running mind gives them their vectors, 32 every 10 s")
    return 1 if plan.rejects or res.failed else 0


def cmd_init(args) -> int:
    root = Path(args.data_dir) if args.data_dir else default_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    for name in DATA_SUBDIRS:
        (root / name).mkdir(exist_ok=True)
    toml = root / "mind.toml"
    if toml.exists():
        print(f"kept {toml}")
    else:
        write_default_config(toml)
        print(f"created {toml}")
    return 0


@contextmanager
def _open_store(args):
    """(root, store) for the personas and roster commands, holding mind.lock throughout:
    they write mind.db (and opening it migrates it), the running mind is its only writer, and
    its persona cache would not see the change. AlreadyRunning while the service holds it."""
    from .app import acquire_instance_lock
    from .storage.db import open_db
    from .storage.store import Store
    root = _root(args)
    with acquire_instance_lock(root):
        store = Store(open_db(root / "mind.db"), clock=SystemClock())
        try:
            yield root, store
        finally:
            store.close()


def _printable(text: str) -> str:
    """What the console can print: a character its encoding cannot show (a cp1252 console)
    becomes an escape instead of a UnicodeEncodeError."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(enc, "backslashreplace").decode(enc)


def _screened(fn, human) -> dict:
    """`human=` for a persona reader that screens records with it (part C's import_personas
    does); nothing for one that does not take it."""
    import inspect
    return {"human": human} if "human" in inspect.signature(fn).parameters else {}


def cmd_personas(args) -> int:
    from .app import AlreadyRunning
    try:
        with _open_store(args) as (root, store):
            return _personas_action(args, root, store)
    except AlreadyRunning:
        print("mind service is running")
        return 1


def _personas_action(args, root: Path, store) -> int:
    from .config import ConfigError
    from .human import human_identity
    from .personas.io import export_personas, import_personas
    from .text import estimate_tokens
    if args.action == "export":
        if args.guid and store.get_persona_json(args.guid) is None:
            print(f"no persona for guid {args.guid}", file=sys.stderr)
            return 1
        paths = export_personas(store, root / "personas", [args.guid] if args.guid else None)
        print(f"exported {len(paths)} to {root / 'personas'}")
        return 0
    if args.action == "seed":
        return cmd_seed(args, root, store)
    if args.action == "generate":
        return cmd_generate(args, root, store)
    if args.action == "mix":
        return cmd_mix(store)
    if args.action == "rebalance-stances":
        return cmd_rebalance(args, root, store)
    if args.action == "import":
        try:
            guid = _human_guid(store, None)
        except SystemExit:  # no roster yet: the names in mind.toml still screen the files
            guid = 0
        try:
            human = human_identity(_load_cfg(root), store, guid)
        except ConfigError as e:
            print(f"mind.toml: {e}", file=sys.stderr)
            return 1
        r = import_personas(store, root / "personas", ts=store.clock.now(),
                            dry_run=args.dry_run, **_screened(import_personas, human))
        verb = "would import" if args.dry_run else "imported"
        print(f"{verb} {len(r.imported)}: {r.imported}")
        print(f"unchanged {len(r.unchanged)}")
        for name, errs in r.errors.items():
            for e in errs:
                print(_printable(f"error {name}: {e}"))
        if r.errors:
            print("nothing was written")
        return 1 if r.errors else 0
    card = store.get_card(args.guid)
    if card is None:
        print(f"no persona for guid {args.guid}", file=sys.stderr)
        return 1
    print(_printable(card))
    print(f"[{estimate_tokens(card)} tokens]")
    return 0


def _load_cfg(root: Path):
    from .config import load_config
    return load_config(root / "mind.toml")


def _human_guid(store, explicit: int | None) -> int:
    """The explicit value, else the stored one, else the guild master (rank 0)."""
    cast = store.cast()
    stored = store.get_meta("human_guid")
    if explicit:
        guid = explicit
    elif stored and stored.isdigit() and int(stored):
        guid = int(stored)
    else:
        masters = sorted(m.guid for m in cast.values() if m.rank == 0)
        if not masters:
            raise SystemExit("cannot tell which member is the human: pass --human-guid")
        guid = masters[0]
    if guid not in cast:
        raise SystemExit(f"human guid {guid} is not in the stored roster; run `roster` first "
                         "or pass a valid --human-guid")
    return guid


def cmd_voices(args) -> int:
    """`voices brief`: what the casting reads of the cast (Task 19). Read-only (N1): the database
    is opened mode=ro as doctor opens it, never through open_db, so nothing migrates, nothing is
    written and no mind.lock is taken. It is safe on a snapshot or on the live file."""
    from .storage.store import Store
    from .voice.brief import brief_entries, write_entries
    root = _root(args)
    path = Path(args.db).resolve() if args.db else root / "mind.db"
    if not path.is_file():
        print(f"voices brief: no mind.db at {path}", file=sys.stderr)
        return 1
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"voices brief: cannot open {path}: {e}", file=sys.stderr)
        return 1
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            print(f"voices brief: {path} has no schema", file=sys.stderr)
            return 1
        entries = brief_entries(Store(conn, clock=SystemClock()))
    except sqlite3.Error as e:
        print(f"voices brief: cannot read {path}: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    written = write_entries(entries, Path(args.out) if args.out else root / "voices")
    women = [e for e in entries if e["gender"] == "female"]
    british = sum(e["accent"] == "en-gb" for e in women)
    share = f"{british / len(women):.0%}" if women else "n/a"
    print(f"brief: {len(entries)} members -> {written}")
    print(f"British women: {british} of {len(women)} ({share})")
    return 0


def cmd_roster(args) -> int:
    import asyncio
    from .app import AlreadyRunning
    from .personas import roster as R
    try:
        with _open_store(args) as (root, store):
            try:
                if args.file:
                    members = R.members_from_json(Path(args.file).read_text(encoding="utf-8"))
                elif args.from_bridge:
                    cfg = _load_cfg(root)
                    members = asyncio.run(R.fetch_cast_from_bridge(cfg.bridge.host,
                                                                   cfg.bridge.port))
                else:
                    members = R.query_roster(R.read_db_info(Path(args.conf)), args.guild,
                                             Path(args.mysql))
            except (R.RosterError, OSError) as e:
                print(f"roster: {e}", file=sys.stderr)
                return 1
            store.upsert_cast(members, store.clock.now())
            print(f"stored {len(members)} guild members")
            return 0
    except AlreadyRunning:
        print("mind service is running")
        return 1


def cmd_seed(args, root, store) -> int:
    import asyncio
    from .llm.client import OllamaClient
    from .personas.seed import seed_cast
    cfg = _load_cfg(root)
    human = _human_guid(store, args.human_guid)
    me = store.cast().get(human)
    print(f"human: {me.name if me else '?'} (guid {human})")
    llm = None if args.dry_run else OllamaClient(cfg.ollama.url)
    report = asyncio.run(seed_cast(
        store, llm, cfg, args.brainstorm, human_guid=human,
        dry_run=args.dry_run, force=args.force, progress=print))
    for row in report.table:
        print(row)
    for u in report.unparsed:
        print(f"UNPARSED {u}")
    print(f"left out, not cast ({len(report.not_cast)}): " + ", ".join(report.not_cast))
    if report.unknown_relations:
        print(f"relationships naming nobody on the roster ({len(report.unknown_relations)}): "
              + "; ".join(report.unknown_relations))
    print(f"assigned {report.assigned}, unmatched {len(report.unmatched)}: {report.unmatched}, "
          f"unparsed {len(report.unparsed)}")
    if args.dry_run:
        for guid, why in report.failed.items():
            print(f"failed {guid}: {why}")
    if not args.dry_run:
        print(f"created {len(report.created)}, skipped {len(report.skipped)}, "
              f"failed {len(report.failed)}")
        for guid, why in report.failed.items():
            print(f"failed {guid}: {why}")
        for guid, n in report.cut_lines.items():
            print(f"cut {n} sample lines for {guid}")
    return 1 if report.failed or report.unparsed else 0


def _mix_line(r) -> str:
    out = (f"total {r.total}  female_share {r.female_share:.2f}  "
           f"introvert_share {r.introvert_share:.2f}")
    if r.roles:
        out += "\n" + "  ".join(f"{k} {r.roles[k]} (female {r.female_by_role[k]:.2f})"
                                for k in ("tank", "healer", "damage"))
    return out


def _cast_personas(store, human_guid: int | None) -> list:
    """Stored personas of cast members only (level 80+, not the human), as plan_slots counts."""
    from .personas.io import load_persona
    from .wow import CAST_MIN_LEVEL
    cast = {g for g, m in store.cast().items() if m.level >= CAST_MIN_LEVEL and g != human_guid}
    return [p for g in sorted(store.persona_versions()) if g in cast
            and (p := load_persona(store, g))]


def cmd_mix(store) -> int:
    from .personas.generate import mix_report
    try:
        human = _human_guid(store, None)
    except SystemExit:
        human = None
    print(_mix_line(mix_report(_cast_personas(store, human))))
    return 0


def cmd_rebalance(args, root, store) -> int:
    import random
    from collections import Counter
    from .human import human_identity
    from .personas.io import _race_class, save_persona
    from .personas.stances import (FILL_ORDER, apply_reseeds, is_woman, none_fit_reason,
                                   parse_overrides, plan_rebalance, plan_reseeds)
    cfg = _load_cfg(root)
    human_guid = _human_guid(store, None)
    human = human_identity(cfg, store, human_guid)
    personas = _cast_personas(store, human_guid)
    try:
        fixed = parse_overrides(args.set, personas)
    except ValueError as e:
        print(f"rebalance-stances: {e}", file=sys.stderr)
        return 1
    plan = plan_rebalance(personas, random.Random(args.seed), human=human, fixed=fixed)
    by_guid = {p.guid: p for p in personas}
    women = [p for p in personas if is_woman(p)]
    before = Counter(p.romance.stance for p in women)
    after = before.copy()
    for _, old, new in plan.changes:
        after[old] -= 1
        after[new] += 1
    print(f"women {len(women)}: " + ", ".join(f"{s} {before[s]} -> {after[s]}"
                                              for s in FILL_ORDER))

    def who(g: int) -> str:
        return f"{by_guid[g].character_name} ({g})"

    def set_tag(g: int) -> str:
        return " (set)" if g in plan.fixed else ""

    print("lust picks:")
    for g in plan.lust:
        p = by_guid[g]
        print(f"  lust: {who(g)} from {p.romance.stance}, style {p.romance.style or '-'}, "
              f"extraversion {p.big_five.extraversion.score}, "
              f"{'seed' if p.core else 'generated'}{set_tag(g)}")
    print("kept out of lust (grandmother type):")
    for p in women:
        if p.guid not in plan.fixed and (why := none_fit_reason(p)):
            print(f"  {who(p.guid)}: {why}")
    print("changes:")
    changed = {g: (old, new) for g, old, new in plan.changes}
    for g in sorted(set(changed) | set(plan.fixed)):
        old, new = changed.get(g, (by_guid[g].romance.stance,) * 2)
        print(f"{g:>7}  {by_guid[g].character_name:<12}  {old} -> {new}{set_tag(g)}")
    for g, lines in plan.dropped.items():
        for text in lines:
            print(f"{who(g)} drops: {text}")
    reseeds = plan_reseeds(store, plan.changes, human_guid)
    for r in reseeds:
        print(f"re-seed {who(r.guid)} -> {human.called}: {r.old} -> {r.new}")
    for stance, n in plan.shortfall.items():
        why = ("no other woman fits it" if stance == "lust"
               else "everyone who could move there is set or needed elsewhere")
        print(f"{stance} is {n} short of its target: {why}")
    if args.dry_run:
        print(f"would change {len(plan.changes)} stances and re-seed {len(reseeds)} rows; "
              "nothing was written (run `personas export` first to keep a copy)")
        return 0
    cast = store.cast()
    updated = plan.updated(personas)
    with store.transaction():
        for p in updated:
            race, cls = _race_class(store, cast, p.guid)
            save_persona(store, p, source="owner", ts=store.clock.now(), race=race,
                         class_name=cls)
        written, skipped = apply_reseeds(store, reseeds, human_guid)  # re-checked here
    for r in skipped:
        print(f"skipped re-seed {who(r.guid)}: the row changed since the plan")
    print(f"changed {len(plan.changes)} stances, saved {len(updated)} personas, "
          f"re-seeded {len(written)} rows")
    return 0


# -- data fixes over the stored cast: relink-human, fix-typing ---------------------------------

def _ascii(s: str) -> str:
    """For the console, which may be cp1252: anything outside ASCII as an escape."""
    return s.encode("ascii", "backslashreplace").decode("ascii")


def _fix_store(args, root: Path, label: str):
    """(store, lock) for a data fix, or an exit code. A dry run opens mind.db read-only, so
    nothing is migrated or written; a real run holds the instance lock (the mind service must
    not run) and snapshots mind.db before opening it for writing."""
    from .app import AlreadyRunning, acquire_instance_lock
    from .storage.db import open_db
    from .storage.store import Store
    db = root / "mind.db"
    if not db.exists():
        print(f"no mind.db at {db}", file=sys.stderr)
        return 1
    if args.dry_run:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return Store(conn, clock=SystemClock()), None
    try:
        lock = acquire_instance_lock(root)
    except AlreadyRunning:
        print("mind service is running")
        return 1
    try:
        print(f"snapshot {_snapshot_file(db, root / 'snapshots', label)}")
        return Store(open_db(db), clock=SystemClock()), lock
    except BaseException:
        lock.release()
        raise


def _close_fix_store(store, lock) -> None:
    try:
        if lock is None:
            store.conn.close()  # read-only: nothing to checkpoint
        else:
            store.close()
    finally:
        if lock is not None:
            lock.release()


def _fix_personas(store, human_guid: int) -> tuple[list, list[str]]:
    """The cast's stored personas (as _cast_personas counts them), and a line for each one that
    cannot be read, so one bad row does not stop the fix for the others."""
    from .personas.io import load_persona
    from .wow import CAST_MIN_LEVEL
    cast = {g for g, m in store.cast().items() if m.level >= CAST_MIN_LEVEL and g != human_guid}
    personas, bad = [], []
    for g in sorted(store.persona_versions()):
        if g not in cast:
            continue
        try:
            p = load_persona(store, g)
        except ValueError as e:  # PersonaError and JSON errors alike
            bad.append(f"cannot read persona {g}: {_ascii(str(e))[:200]}")
            continue
        if p is not None:
            personas.append(p)
    return personas, bad


def cmd_relink_human(args) -> int:
    """`personas relink-human`: every unlived row toward the human takes what its persona
    declares toward him (controller ruling "final I2"; personas/relink.py)."""
    from .config import ConfigError
    from .human import human_identity
    from .personas.relink import apply_relink, plan_relink
    from .personas.seed import KIND_ORDER
    from .talk.prompt import is_close
    root = _root(args)
    if not (root / "mind.db").exists():
        print(f"no mind.db at {root / 'mind.db'}", file=sys.stderr)
        return 1
    if not (root / "mind.toml").exists():
        print(f"no mind.toml at {root}: the human's names come from it", file=sys.stderr)
        return 1
    try:
        cfg = _load_cfg(root)
    except ConfigError as e:
        print(f"mind.toml: {e}", file=sys.stderr)
        return 1
    opened = _fix_store(args, root, "pre-relink-human")
    if isinstance(opened, int):
        return opened
    store, lock = opened
    try:
        human_guid = _human_guid(store, args.human_guid)
        human = human_identity(cfg, store, human_guid)
        print(f"human: {_ascii(human.character_name)} (guid {human_guid}), "
              f"called {_ascii(human.called)}")
        personas, bad = _fix_personas(store, human_guid)
        for line in bad:
            print(line)
        plan = (plan_relink if args.dry_run else apply_relink)(store, personas, human,
                                                               now=store.clock.now())
        print(f"{'guid':>7}  {'name':<12}  {'kind':<8}  {'close':<5}  action")
        for r in plan:
            close = "yes" if is_close(r.row) else "no"
            why = f" ({r.why})" if r.why else ""
            print(f"{r.guid:>7}  {_ascii(r.name):<12}  {r.kind:<8}  {close:<5}  {r.action}{why}")
        writes = [r for r in plan if r.action == "write"]
        kinds = ", ".join(f"{k} {sum(r.kind == k for r in writes)}" for k in KIND_ORDER)
        print(f"{'would write' if args.dry_run else 'wrote'} {len(writes)} rows ({kinds}), "
              f"unchanged {sum(r.action == 'unchanged' for r in plan)}, "
              f"skipped {sum(r.action == 'skip' for r in plan)}")
        print(f"close to him afterwards: {sum(is_close(r.row) for r in plan)} of {len(plan)}")
        if args.dry_run:
            print("nothing was written")
        return 1 if bad else 0
    finally:
        _close_fix_store(store, lock)


def cmd_fix_typing(args) -> int:
    """`personas fix-typing`: typing maps the right way round (full form -> chat form), plain
    text emoticons, and old seed names left in free text replaced (personas/typing_fix.py).
    Each changed persona is saved as a new version; one still invalid is reported, not saved."""
    from collections import Counter
    from .config import ConfigError
    from .human import human_identity
    from .personas.io import _race_class, save_persona
    from .personas.typing_fix import plan_fix_typing, seed_names
    root = _root(args)
    if not (root / "mind.db").exists():
        print(f"no mind.db at {root / 'mind.db'}", file=sys.stderr)
        return 1
    logs = ([Path(p) for p in args.seed_log] if args.seed_log
            else sorted((root / "logs").glob("seed-*.log")))
    texts = []
    for path in logs:
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            print(f"seed log {path}: {e.__class__.__name__}", file=sys.stderr)
            return 1
    try:
        cfg = _load_cfg(root)
    except ConfigError as e:
        print(f"mind.toml: {e}", file=sys.stderr)
        return 1
    opened = _fix_store(args, root, "pre-fix-typing")
    if isinstance(opened, int):
        return opened
    store, lock = opened
    try:
        human_guid = _human_guid(store, None)
        personas, bad = _fix_personas(store, human_guid)
        for line in bad:
            print(line)
        cast = store.cast()
        names, notes = seed_names(texts, cast, human=human_identity(cfg, store, human_guid),
                                  personas=personas)
        listed = ", ".join(f"{_ascii(o)} -> {_ascii(n)}" for o, n in sorted(names.items()))
        print(f"seed names from {len(logs)} log{'' if len(logs) == 1 else 's'}: "
              f"{len(names)} to replace" + (f": {listed}" if listed else "")
              + ("" if logs else f" (no seed-*.log in {root / 'logs'}; see --seed-log)"))
        for note in notes:
            print(note)
        plan = plan_fix_typing(personas, names)
        for f in plan:
            print(f"{f.guid:>7}  {_ascii(f.name)}")
            for c in f.changes:
                print(f"         {c.text}")
            for e in f.errors:
                print(f"         cannot save: {_ascii(e)}")
        ok = [f for f in plan if not f.errors]
        if not args.dry_run:
            with store.transaction():
                for f in ok:
                    race, cls = _race_class(store, cast, f.guid)
                    save_persona(store, f.persona, source="owner", ts=store.clock.now(),
                                 race=race, class_name=cls)
        n = Counter(c.kind for f in ok for c in f.changes)
        print(f"{'would change' if args.dry_run else 'changed'} {len(ok)} personas: "
              f"{n['abbreviation turned round']} abbreviations turned round, "
              f"{n['abbreviation dropped']} dropped, {n['abbreviation cleaned']} cleaned; "
              f"{n['emoticon dropped']} emoticons dropped, {n['emoticon cleaned']} cleaned; "
              f"{n['seed name']} seed names replaced")
        if len(ok) < len(plan):
            print(f"left as they are, still invalid: {len(plan) - len(ok)}")
        if args.dry_run:
            print("nothing was written")
        return 1 if bad or len(ok) < len(plan) else 0
    finally:
        _close_fix_store(store, lock)


def cmd_generate(args, root, store) -> int:
    import asyncio
    import random
    from .llm.client import OllamaClient
    from .personas.generate import generate_cast
    cfg = _load_cfg(root)
    human = _human_guid(store, args.human_guid)
    llm = None if args.dry_run else OllamaClient(cfg.ollama.url)
    report = asyncio.run(generate_cast(
        store, llm, cfg, human_guid=human, data_dir=root, limit=args.limit,
        dry_run=args.dry_run, rng=random.Random(args.seed), progress=print))
    if args.dry_run:
        names = {m.guid: m.name for m in store.cast().values()}
        for sl in report.slots:
            print(f"{names.get(sl.guid, sl.guid)}: {sl.player_gender} {sl.age_band[0]}-"
                  f"{sl.age_band[1]} {sl.region} {'introvert' if sl.introvert else 'outgoing'} "
                  f"{sl.combat_role} {sl.guild_role} {'drinker' if sl.drinker else 'sober'} "
                  f"{sl.romance_stance} {sl.jealousy_mode or '-'} {sl.thinks_as}")
        have = _cast_personas(store, human)
        total = len(have) + len(report.slots)
        if total:
            women = sum(p.identity.player_gender == "female" for p in have) + sum(
                sl.player_gender == "female" for sl in report.slots)
            intro = sum(p.big_five.extraversion.score <= 4 for p in have) + sum(
                sl.introvert for sl in report.slots)
            print(f"projected: total {total}  female_share {women / total:.2f}  "
                  f"introvert_share {intro / total:.2f}")
            from .personas.generate import role_of
            pairs = [(role_of(p), p.identity.player_gender == "female") for p in have] + [
                (sl.combat_role, sl.player_gender == "female") for sl in report.slots]
            print("projected roles: " + "  ".join(
                f"{r} {sum(x == r for x, _ in pairs)} (female "
                f"{sum(f for x, f in pairs if x == r) / max(1, sum(x == r for x, _ in pairs)):.2f})"
                for r in ("tank", "healer", "damage")))
        print(f"planned {report.planned}")
        return 0
    print(f"planned {report.planned}, created {len(report.created)}, "
          f"failed {len(report.failed)}")
    for guid, why in report.failed.items():
        print(f"failed {guid}: {why}")
    if report.collisions:
        print(f"name collisions retried ({len(report.collisions)}): "
              + ", ".join(report.collisions))
    for guid, targets in report.dropped_targets.items():
        print(f"dropped relationship targets for {guid}: " + ", ".join(targets))
    return 1 if report.failed else 0


def main(argv: list[str] | None = None, *, make_llm=None) -> int:
    """`make_llm(cfg)` replaces the Ollama client for `run` and `doctor` (tests)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("nightly", "eval"):
        from .nightly.cli import eval_main, nightly_main
        command = nightly_main if argv[0] == "nightly" else eval_main
        return command(argv[1:], make_llm=make_llm)
    parser = argparse.ArgumentParser(prog="leeroy_mind")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("nightly", help="nightly status, trials, ledger and owner controls")
    sub.add_parser("eval", help="weekly evaluation on a copied database by default")
    p_init = sub.add_parser("init", help="create the data dir and a default mind.toml")
    _add_data_dir(p_init)
    p_init.set_defaults(func=cmd_init)
    conf_help = "mod_ollama_chat.conf to compare with the live model (default: the server's)"
    p_run = sub.add_parser("run", help="run the mind service until Ctrl+C (it refuses a data "
                                       "dir without mind.toml or personas)")
    _add_data_dir(p_run)
    p_run.add_argument("--log-level", default="INFO", choices=LOG_LEVELS)
    p_run.add_argument("--ollama-conf", default=None, type=Path, help=conf_help)
    p_run.set_defaults(func=cmd_run)
    p_doc = sub.add_parser("doctor", help="check Python, mind.db, library.db, Ollama, the "
                                          "models and the bridge; one ok, note or FAIL line "
                                          "each")
    _add_data_dir(p_doc)
    p_doc.add_argument("--ollama-conf", default=None, type=Path, help=conf_help)
    p_doc.set_defaults(func=cmd_doctor)
    p_db = sub.add_parser("db", help="snapshot or migrate mind.db")
    _add_data_dir(p_db)
    dsub = p_db.add_subparsers(dest="action", required=True)
    p_snap = dsub.add_parser("snapshot", help="copy mind.db into mind-data/snapshots")
    p_snap.add_argument("--label", default="manual")
    _add_data_dir(p_snap, after_action=True)
    p_mig = dsub.add_parser("migrate", help="snapshot, then bring mind.db to this code's schema")
    _add_data_dir(p_mig, after_action=True)
    p_db.set_defaults(func=cmd_db)
    p_mem = sub.add_parser("memories", help="seed the owner's own memories of the guild")
    _add_data_dir(p_mem)
    msub = p_mem.add_subparsers(dest="action", required=True)
    p_ms = msub.add_parser("seed", help="write the owner's memories from a JSON file: source "
                                        "owner, keys owner:<moment>:<guid>, so a re-run is safe")
    p_ms.add_argument("file", type=Path, help="a JSON list of {guid, text, moment, kind, "
                                              "importance?, actors?, date?, character?}")
    p_ms.add_argument("--dry-run", action="store_true",
                      help="check the file and print what would change; write nothing")
    p_ms.add_argument("--fiction-now", default="2009-12-31", metavar="YYYY-MM-DD",
                      help="the in-fiction date that is today: a dated memory is set that long "
                           "before the day the history is first seeded (default 2009-12-31)")
    _add_data_dir(p_ms, after_action=True)
    p_mem.set_defaults(func=cmd_memories)
    p_pers = sub.add_parser("personas", help="export, import or show persona cards")
    _add_data_dir(p_pers)
    psub = p_pers.add_subparsers(dest="action", required=True)
    p_exp = psub.add_parser("export", help="write mind-data/personas/<guid>.json")
    p_exp.add_argument("--guid", type=int, default=None)
    p_imp = psub.add_parser("import", help="read mind-data/personas/*.json back in")
    p_imp.add_argument("--dry-run", action="store_true")
    p_card = psub.add_parser("card", help="print a persona's core card and its token count")
    p_card.add_argument("guid", type=int)
    p_seed = psub.add_parser("seed", help="convert the owner's sketches into the seed personas")
    p_seed.add_argument("--brainstorm", required=True, help="path to the sketches file")
    p_seed.add_argument("--dry-run", action="store_true",
                        help="print the character assignment only; no model calls, no writes")
    p_seed.add_argument("--force", action="store_true", help="redo characters that have a persona")
    p_seed.add_argument("--human-guid", type=int, default=None,
                        help="the owner's character (default: stored, else the guild master)")
    p_gen = psub.add_parser("generate", help="write the rest of the cast from the mix targets")
    p_gen.add_argument("--limit", type=int, default=None)
    p_gen.add_argument("--dry-run", action="store_true",
                       help="print the slot plan and projected mix; no model calls, no writes")
    p_gen.add_argument("--seed", type=int, default=1)
    p_gen.add_argument("--human-guid", type=int, default=None,
                       help="the owner's character (default: stored, else the guild master)")
    psub.add_parser("mix", help="report the female and introvert shares of the stored personas")
    p_reb = psub.add_parser("rebalance-stances",
                            help="move the stored women's romance stances to the 20/30/40/10 "
                                 "lust/drawn/fond/none mix with the fewest changes")
    p_reb.add_argument("--dry-run", action="store_true",
                       help="print the planned changes; write nothing")
    p_reb.add_argument("--seed", type=int, default=1)
    p_reb.add_argument("--set", action="append", default=[], metavar="GUID=STANCE",
                       help="the owner's choice for one woman, applied first and never moved "
                            "(repeatable)")
    p_rel = psub.add_parser("relink-human",
                            help="give each unlived relationship row toward the human what its "
                                 "persona declares toward him (friend: close, friction: low "
                                 "affinity, with the note), keeping the stance's attraction")
    p_rel.add_argument("--dry-run", action="store_true",
                       help="print the table; mind.db is opened read-only and nothing is written")
    p_rel.add_argument("--human-guid", type=int, default=None,
                       help="the owner's character (default: stored, else the guild master)")
    p_rel.add_argument("--data-dir", default=argparse.SUPPRESS,
                       help="the mind's data folder (as personas --data-dir)")
    p_rel.set_defaults(func=cmd_relink_human)
    p_fix = psub.add_parser("fix-typing",
                            help="turn the stored personas' typing maps the right way round "
                                 "(full form -> chat form), drop emoji and replace old seed "
                                 "names left in their text; changed personas get a new version")
    p_fix.add_argument("--dry-run", action="store_true",
                       help="print every change; mind.db is opened read-only and nothing is "
                            "written")
    p_fix.add_argument("--seed-log", action="append", default=[], metavar="PATH",
                       help="a `personas seed` log with the sketch -> character table "
                            "(repeatable; default: every logs/seed-*.log in the data dir)")
    p_fix.add_argument("--data-dir", default=argparse.SUPPRESS,
                       help="the mind's data folder (as personas --data-dir)")
    p_fix.set_defaults(func=cmd_fix_typing)
    p_pers.set_defaults(func=cmd_personas)
    from .personas.roster import DEFAULT_CONF, DEFAULT_MYSQL, ROSTER_FILE_HELP
    p_voices = sub.add_parser("voices", help="the guild's voices")
    _add_data_dir(p_voices)
    vsub = p_voices.add_subparsers(dest="action", required=True)
    p_brief = vsub.add_parser("brief", help="write voices/brief.json for the casting, reading "
                                            "mind.db read-only (never migrates, takes no lock)")
    _add_data_dir(p_brief, after_action=True)
    p_brief.add_argument("--db", default=None, help="read this mind.db (a snapshot) instead of "
                                                    "the data dir's")
    p_brief.add_argument("--out", default=None, help="where brief.json goes (default: "
                                                     "<data dir>/voices)")
    p_voices.set_defaults(func=cmd_voices)
    p_ros = sub.add_parser("roster", help="store the guild roster in mind.db")
    _add_data_dir(p_ros)
    src = p_ros.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-mysql", action="store_true", help="read-only SELECT via mysql.exe")
    src.add_argument("--file", help="roster.json: " + ROSTER_FILE_HELP)
    src.add_argument("--from-bridge", action="store_true",
                     help="take the cast from the bridge greeting (uses its only mind slot)")
    p_ros.add_argument("--conf", default=str(DEFAULT_CONF))
    p_ros.add_argument("--mysql", default=str(DEFAULT_MYSQL))
    p_ros.add_argument("--guild", type=int, default=21)
    p_ros.set_defaults(func=cmd_roster)
    args = parser.parse_args(argv)
    args.make_llm = make_llm
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
