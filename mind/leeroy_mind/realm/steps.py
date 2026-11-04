"""Realm data steps: a plan file, his hash-bound approval, and one apply in the running mind.

``realm plan <step>`` writes ``mind-data/realm/plans/<step>-<YYYYmmdd-HHMM>.json`` with a
SHA-256 over its canonical JSON. ``realm approve <file>`` writes only
``mind-data/realm/approved/<same name>`` (``{"sha256", "approved_at"}``), never ``mind.db``:
the running mind is its only writer. Every 60 s the mind's ``Steps`` part applies each approved
plan whose file still has the approved hash, through the step's registered
``apply(payload, progress)``, and records the result in ``realm_step``. A plan edited after its
approval is refused (``realm_step_hash_mismatch``) for good; a fresh plan needs a fresh approval.

``apply`` may be a coroutine function. It must be idempotent: each of its one-shot commands goes
through ``progress.send(idx, cmd)``, which prepares the DirectLedger ref
``step:<name>:<sha>:<idx>`` at most once, so a resumed apply (after a disable, a lost
connection or a restart) never sends an item twice. An item that never moved anything (lost
before the barrier, or refused by the bridge, native's realm switch or a busy owner) is sent again
with a fresh command id, the step staying open meanwhile. Raising ``StepPending`` leaves the step
open for the next tick; any other exception is counted in its result and the step is tried again.
"""

import dataclasses
import hashlib
import inspect
import json
import logging
import math
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from .direct import busy

log = logging.getLogger(__name__)

OWNER = "step"
TICK_S = 60
# Refusals before anything moved: the bridge's own before the wire, and native's realm switch.
_AGAIN = frozenset({"pretransport_refused", "realm_off", "realm_not_started"})
_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_STEM = re.compile(r"([a-z][a-z0-9_]{0,31})-([0-9]{8}-[0-9]{4})(?:-([1-9][0-9]?))?")
_SHA = re.compile(r"[0-9a-f]{64}")
_RESERVED = frozenset({"history"})  # approved/history-<seed>-<canon>.json is canon's, not a step's
_MAX_BYTES = 16 << 20
_REFUSED = "realm_step_hash_mismatch"


class StepError(ValueError):
    """A refused plan or approval. The message is fixed text, never a local path or value."""


class StepPending(Exception):
    """Raised by apply (or progress.send) when the step cannot finish yet: tried again later."""


def check_name(name: str) -> str:
    if type(name) is not str or _NAME.fullmatch(name) is None or name in _RESERVED:
        raise StepError("invalid step name")
    return name


def _strict(value):
    raise ValueError("non-finite number")


def _normalise(obj):
    """What a plan file reads back as: str keys, lists for tuples, finite numbers only."""
    return json.loads(json.dumps(obj, allow_nan=False), parse_constant=_strict)


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(doc: dict) -> str:
    """SHA-256 of the plan's canonical JSON, every key but "sha256" itself."""
    return hashlib.sha256(canonical({k: v for k, v in doc.items() if k != "sha256"})).hexdigest()


def plans_dir(data_dir) -> Path:
    return Path(data_dir) / "realm" / "plans"


def approved_dir(data_dir) -> Path:
    return Path(data_dir) / "realm" / "approved"


def _write_new(path: Path, text: str) -> None:
    """A complete file or none; refuses an existing one."""
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    try:
        if path.exists():
            raise FileExistsError(path.name)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path):
    try:
        if path.stat().st_size > _MAX_BYTES:
            raise StepError("file too large")
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_strict)
    except FileNotFoundError:
        raise StepError("file not found") from None
    except (OSError, UnicodeError, ValueError) as error:
        if type(error) is StepError:
            raise
        raise StepError("file unreadable") from None


def write_plan(data_dir, name: str, payload, *, local: datetime) -> Path:
    """The plan file for step `name`, stamped with `local` (minute resolution, never overwritten)."""
    check_name(name)
    try:
        doc = _normalise({"step": name, "written": local.isoformat(timespec="seconds"),
                          "payload": payload})
    except (TypeError, ValueError):
        raise StepError("plan payload is not finite JSON") from None
    doc["sha256"] = digest(doc)
    text = json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    directory = plans_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = local.strftime("%Y%m%d-%H%M")
    for n in range(1, 100):
        path = directory / (f"{name}-{stamp}.json" if n == 1 else f"{name}-{stamp}-{n}.json")
        try:
            _write_new(path, text)
        except FileExistsError:
            continue
        return path
    raise StepError("too many plans of this step this minute")


def read_plan(path: Path, *, name: str | None = None) -> dict:
    doc = _read_json(path)
    if (type(doc) is not dict or "payload" not in doc or type(doc.get("sha256")) is not str
            or _SHA.fullmatch(doc["sha256"]) is None or type(doc.get("step")) is not str
            or (name is not None and doc["step"] != name)):
        raise StepError("not a plan")
    check_name(doc["step"])
    return doc


def _plan_stem(path: Path):
    match = _STEM.fullmatch(path.stem)
    return match if match is not None and path.suffix == ".json" else None


def approve(data_dir, path, *, now: float) -> Path:
    """His OK on one plan file: an approval file bound to its hash. Never touches mind.db."""
    if type(now) not in (int, float) or not math.isfinite(now):
        raise StepError("invalid approval time")
    directory = plans_dir(data_dir).resolve()
    plan = Path(path).resolve()
    if plan.parent != directory:
        raise StepError("plan is not in the plans directory")
    match = _plan_stem(plan)
    if match is None:
        raise StepError("not a plan file")
    doc = read_plan(plan, name=match.group(1))
    sha = digest(doc)
    if sha != doc["sha256"]:
        raise StepError("plan changed after it was written; write a fresh plan")
    target = approved_dir(data_dir) / plan.name
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_new(target, json.dumps({"sha256": sha, "approved_at": float(now)}, sort_keys=True) + "\n")
    except FileExistsError:
        existing = _read_json(target)
        if type(existing) is not dict or existing.get("sha256") != sha:
            raise StepError("plan already approved with another hash") from None
    return target


def _approval(path: Path):
    doc = _read_json(path)
    if (type(doc) is not dict or type(doc.get("sha256")) is not str
            or _SHA.fullmatch(doc["sha256"]) is None or type(doc.get("approved_at")) not in (int, float)
            or not math.isfinite(doc["approved_at"])):
        raise StepError("not an approval")
    return doc["sha256"], float(doc["approved_at"])


def _again(state) -> bool:
    """An item the ledger says never moved anything, so it may be sent again: abandoned (it
    never reached the wire), refused before the bridge wrote it, or refused by native's realm
    switch or a busy owner. Any other refusal is the item's answer, and unknown is final."""
    if state is None:
        return False
    name, reason, _ = state
    return name == "abandoned" or (name == "refused" and (reason in _AGAIN or busy(reason)))


class Progress:
    """One plan's exactly-once sends: item idx is the DirectLedger ref step:<name>:<sha>:<idx>."""

    __slots__ = ("_steps", "name", "sha256")

    def __init__(self, steps, name, sha256):
        self._steps, self.name, self.sha256 = steps, name, sha256

    def ref(self, idx: int) -> str:
        if type(idx) is not int or idx < 0:
            raise ValueError("step item index must be a non-negative int")
        return f"step:{self.name}:{self.sha256}:{idx}"

    def state(self, idx: int) -> tuple[str, str | None, int | None] | None:
        """The ledger's (state, reason, value) of item idx; None before it was ever prepared."""
        return self._steps.ledger().state(OWNER, self.ref(idx))

    async def send(self, idx: int, cmd, *,
                   expects_job: bool = False) -> tuple[str, str | None, int | None] | None:
        """Prepare and send item idx once; the ledger's (state, reason, value) after it. An item
        done, refused for good or unknown is never sent again: its state is returned. One that
        never moved anything (_again) is sent again with a fresh command id, and the step
        stays open (StepPending) while native still gives such a refusal."""
        ref, ledger = self.ref(idx), self._steps.ledger()
        state = ledger.state(OWNER, ref)
        if state is not None and not _again(state):
            return state
        realm = self._steps.realm
        context = realm.action_context()
        if not self._steps.ready(context):
            raise StepPending("realm not ready")
        if state is not None:
            cmd = dataclasses.replace(cmd, id=uuid.uuid4().hex)  # A command id is one intent's.
        prepared = ledger.prepare(cmd, owner=OWNER, ref=ref, context=context, expects_job=expects_job)
        if prepared is None:
            return ledger.state(OWNER, ref)
        if prepared != cmd.id:
            cmd = dataclasses.replace(cmd, id=prepared)
        try:
            await realm.ports.send(cmd)
        except Exception as error:  # The ledger row records the outcome.
            log.debug("realm_step_send_failed step=%s idx=%d error=%s", self.name, idx,
                      type(error).__name__)
        finally:
            ledger.abandon_unsent(cmd)  # Only a row that never reached its barrier.
        state = ledger.state(OWNER, ref)
        if _again(state):
            raise StepPending("step item refused before anything moved")
        return state


class Steps:
    """The running mind's half of the data steps: a Realm part with a 60 s tick."""

    name = "steps"

    def __init__(self, realm):
        self.realm = realm
        self._applies = {}
        self._next = None
        self._noted = set()

    def register(self, name: str, apply) -> None:
        check_name(name)
        if not callable(apply):
            raise TypeError("apply must be callable")
        if name in self._applies:
            raise ValueError("step already registered")
        self._applies[name] = apply

    @property
    def data_dir(self) -> Path:
        return Path(self.realm.app.data_dir)

    def write_plan(self, name: str, payload) -> Path:
        return write_plan(self.data_dir, name, payload, local=self.realm.app.clock.local())

    def approve(self, path) -> Path:
        return approve(self.data_dir, path, now=self.realm.app.clock.now())

    @staticmethod
    def ready(context) -> bool:
        return context.enabled is True and context.connected is True and context.presence_known is True

    def ledger(self):
        """The DirectLedger part, resolved at call time; without one a step waits."""
        part = getattr(self.realm, "part", None)
        try:
            ledger = part("direct") if part is not None else None
        except LookupError:
            ledger = None
        if ledger is None:
            self._note("realm_step_no_direct_ledger", "direct")
            raise StepPending("no direct ledger")
        return ledger

    def _note(self, event, key, level=logging.WARNING):
        if (event, key) not in self._noted:
            self._noted.add((event, key))
            log.log(level, "%s step=%s", event, key)

    async def tick(self) -> None:
        clock = self.realm.app.clock
        now = clock.now()
        if self._next is not None and now < self._next:
            return
        if not self.ready(self.realm.action_context()):
            return
        self._next = now + TICK_S
        for entry in self._inbox():
            if not self.ready(self.realm.action_context()):
                return
            await self._consider(*entry)

    def _inbox(self) -> list[tuple[float, str, str, str]]:
        directory = approved_dir(self.data_dir)
        if not directory.is_dir():
            return []
        found = []
        for path in directory.glob("*.json"):
            match = _plan_stem(path)
            if match is not None:
                try:
                    sha, approved_at = _approval(path)
                except StepError:
                    self._note("realm_step_approval_invalid", path.stem)
                    continue
                found.append((approved_at, path.name, match.group(1), sha))
        return sorted(found)

    def _row(self, name, sha):
        row = self.realm.ports.conn.execute(
            "SELECT applied,result FROM realm_step WHERE name=? AND sha256=?", (name, sha)).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row[1])
        except ValueError:
            result = {}
        return row[0], result if type(result) is dict else {}

    async def _consider(self, approved_at, filename, name, sha) -> None:
        row = self._row(name, sha)
        if row is not None and (row[0] is not None or "refused" in row[1]):
            return
        conn = self.realm.ports.conn
        try:
            doc = read_plan(plans_dir(self.data_dir) / filename)
        except StepError:
            self._note("realm_step_plan_unreadable", name)
            return
        if doc["step"] != name or digest(doc) != sha:
            with conn:
                conn.execute(
                    "INSERT INTO realm_step(name,sha256,approved,result) VALUES (?,?,?,?) "
                    "ON CONFLICT(name,sha256) DO UPDATE SET result=excluded.result "
                    "WHERE realm_step.applied IS NULL",
                    (name, sha, approved_at, json.dumps({"refused": _REFUSED})))
            log.warning("%s step=%s", _REFUSED, name)
            return
        with conn:
            conn.execute("INSERT INTO realm_step(name,sha256,approved) VALUES (?,?,?) "
                         "ON CONFLICT(name,sha256) DO NOTHING", (name, sha, approved_at))
        apply = self._applies.get(name)
        if apply is None:
            self._note("realm_step_unregistered", name, logging.INFO)
            return
        try:
            result = apply(doc["payload"], Progress(self, name, sha))
            if inspect.isawaitable(result):
                result = await result
            text = canonical({} if result is None else result).decode("utf-8")
        except StepPending:
            return
        except Exception as error:
            attempts = (row[1].get("attempts", 0) if row is not None else 0)
            attempts = attempts + 1 if type(attempts) is int else 1
            with conn:
                conn.execute("UPDATE realm_step SET result=? WHERE name=? AND sha256=? AND applied IS NULL",
                             (json.dumps({"attempts": attempts, "error": type(error).__name__}), name, sha))
            log.warning("realm_step_failed step=%s attempts=%d error=%s", name, attempts,
                        type(error).__name__)
            return
        with conn:
            conn.execute("UPDATE realm_step SET applied=?,result=? WHERE name=? AND sha256=? "
                         "AND applied IS NULL", (self.realm.app.clock.now(), text, name, sha))
        log.info("realm_step_applied step=%s", name)


def attach(realm) -> Steps:
    """Register the steps part: no I/O and no task until the Realm's own tick."""
    steps = Steps(realm)
    realm.add_part(steps)
    return steps
