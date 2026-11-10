"""Opt-in operator work, owned by the running mind. No console-side database writes.

Requests return promptly; one task keeps its reservation until its real work ends. An ACK
to offline stop is pending, never completion. Public replies/logs contain fixed words and
ids, never a label, release reason, native payload or exception text.
"""
import asyncio
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from uuid import uuid4

from .logs import log
from .realm.action_store import ActionStore, abandon_refusal

ACTIONS = ("release", "offline_stop", "render", "snapshot")
ID = re.compile(r"[0-9a-f]{32}")
LABEL = re.compile(r"[A-Za-z0-9_-]{1,40}")


def _plain(path, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError("Snapshots require ordinary contained files and directories, without links or junctions.")
    return info.st_dev, info.st_ino


def take_snapshot(data, label, operation_id):
    """The run command's backup helper, in a new private staging directory.

    No request can supply a path. An unpredictable operation suffix prevents collision with
    an old partial file; publication never overwrites another snapshot. Manual snapshots
    are retained for the operator rather than pruning another label's history.
    """
    from .storage.db import snapshot
    _plain(data, True)
    source = data / "mind.db"
    _plain(source)
    dest = data / "snapshots"
    dest.mkdir(exist_ok=True)
    identity = _plain(dest, True)
    staging = dest / (".ops-" + operation_id)
    staging.mkdir(mode=0o700)  # exclusive; never adopt an existing directory
    made = None
    try:
        with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            made = snapshot(conn, staging, "console-"+label+"-"+operation_id)
        if _plain(dest, True) != identity:
            raise ValueError("The snapshot directory changed; publication was refused.")
        _plain(staging, True)
        _plain(made)
        # Hardlink is atomic/no-clobber on the local mind data volume. Unsupported volumes
        # fail closed; no SMB assumption or overwrite fallback is used here.
        os.link(made, dest / made.name, follow_symlinks=False)
    finally:
        if _plain(dest, True) == identity:
            if made is not None:
                made.unlink(missing_ok=True)
            staging.rmdir()


def arguments(value):
    from .endpoint import BadRequest
    if not isinstance(value, dict) or value.get("action") not in (*ACTIONS, "status", "inspect"):
        raise BadRequest("Choose a known Ops action.")
    action = value["action"]
    required = {"action"}
    if action == "inspect":
        if value.get("kind") not in ACTIONS:
            raise BadRequest("Choose a known Ops action.")
        required.add("kind")
        action = value["kind"]
    if action in {"release", "offline_stop"}:
        required.add("id")
        if not isinstance(value.get("id"), str) or not ID.fullmatch(value["id"]):
            raise BadRequest("Use the exact run or root id.")
    if action == "snapshot":
        required.add("label")
        if not isinstance(value.get("label"), str) or not LABEL.fullmatch(value["label"]):
            raise BadRequest("Snapshot label: 1 to 40 letters, digits, underscores or hyphens.")
    if set(value) != required:
        raise BadRequest("Unexpected or missing Ops fields.")
    return dict(value)


def release_record(conn, root_id, now):
    store = ActionStore(conn)
    row = store.get(root_id)
    if row is None:
        raise ValueError("No such Realm root; use the exact id.")
    refusal = abandon_refusal(row, [r for r in store.records() if r.parent_id == root_id])
    if refusal:
        raise ValueError(refusal)
    evidence = json.loads(row.evidence)
    native = (evidence.get("job") or {}).get("reason") or (evidence.get("ack") or {}).get("error")
    # Native reason codes only, never arbitrary evidence strings.
    native = native if isinstance(native, str) and re.fullmatch(r"[a-z_:]{1,80}", native) else "not_recorded"
    return {"id": root_id, "kind": row.kind, "age_s": max(0, now-row.created),
            "members": len(row.reserved_manifest), "native_reason": native}


class OpsControl:
    def __init__(self, endpoint):
        self.endpoint, self.app = endpoint, endpoint.app
        self.task = None
        self.operation = None

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def available(self, action):
        app, ep = self.app, self.endpoint
        if not ep.cfg.ops_actions:
            raise ValueError("Ops actions are off; enable control.ops_actions and restart the mind and console.")
        if app.stopping:
            raise ValueError("The mind is stopping.")
        if self.busy or any(getattr(ep, name).locked() for name in ("_raid", "_tune", "_selfie", "_say", "_board", "_preview")):
            raise ValueError("Another action is still running.")
        from .endpoint import world_status
        world = world_status(app)
        if not world or world.get("connected") is not True:
            raise ValueError("The owner's game state is unknown; wait for the bridge.")
        if world.get("human_online") is not False:
            raise ValueError("The owner is in game; wait until he is offline.")
        conn = app.store.conn
        if conn.execute("SELECT 1 FROM guild_raid WHERE ended IS NULL AND offline=0 LIMIT 1").fetchone():
            raise ValueError("A guild raid is open; end it before this action.")
        offline = getattr(getattr(app, "guild", None), "offline", None)
        if action != "offline_stop" and ((offline is not None and offline.active) or conn.execute(
                "SELECT 1 FROM guild_raid WHERE ended IS NULL AND offline=1 LIMIT 1").fetchone()):
            raise ValueError("An offline raid is open; wait for its terminal outcome.")

    def inspect(self, args):
        action = args.get("kind", args["action"])
        self.available(action)
        if action == "release":
            return release_record(self.app.store.conn, args["id"], self.app.clock.now())
        if action == "offline_stop":
            offline = getattr(getattr(self.app, "guild", None), "offline", None)
            if offline is None:
                raise ValueError("Offline raids are not running in this mind.")
            problem = offline.stop_problem(args["id"])
            if problem:
                raise ValueError(problem)
        if action == "render":
            booth = getattr(self.app, "selfie_booth", None)
            if booth is None:
                raise ValueError("The photo booth is off in this mind.")
            if problem := booth.problem():
                raise ValueError(problem)
        return None

    async def answer(self, args):
        action = args["action"]
        if action == "status":
            return {"ok": True, "operation": self.operation, "enabled": self.endpoint.cfg.ops_actions}
        try:
            details = self.inspect(args)
            if action == "inspect":
                return {"ok": True, "details": details}
            if action == "release":
                raise ValueError("Realm release must use the console's clean stop and restart procedure.")
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception:
            return {"ok": False, "error": "The required state could not be verified; nothing started."}
        self.operation = {"id": uuid4().hex, "action": action, "state": "running",
                          "message": "The mind is carrying out the action.", "target": args.get("id")}
        self.task = asyncio.create_task(self._run(args), name="ops-action")
        return {"ok": True, "operation": dict(self.operation)}

    async def _run(self, args):
        state, message = "failed", "The action failed; inspect the local status before retrying."
        try:
            if args["action"] == "snapshot":
                from .selfies.worker import to_the_end
                work = asyncio.create_task(asyncio.to_thread(take_snapshot,
                    self.app.data_dir, args["label"], self.operation["id"]))
                # A database backup cannot be abandoned when the endpoint shuts down.
                await to_the_end(work)
                state, message = "done", "The mind snapshot was saved."
            elif args["action"] == "render":
                booth = self.app.selfie_booth
                work = await booth.request()
                try:
                    outcome = await asyncio.shield(work)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling() or not work.cancelled():
                        raise
                    outcome = "preempted"
                state = "done" if outcome == "done" else "failed"
                message = {"done": "The photo booth pass finished.", "partial": "The photo booth pass finished with failed sets.",
                           "preempted": "The photo booth yielded to a live lease or night."}.get(outcome, "The photo booth pass failed.")
            else:
                offline = self.app.guild.offline
                await offline.request_stop(args["id"])
                if offline._owned.get("stop_unsupported"):
                    raise ValueError("This game server refused safe offline stops; the run remains owned.")
                self.operation["message"] = "Stop requested; waiting for the original run's terminal outcome and cleanup."
                while offline.active and offline._owned.get("id") == args["id"]:
                    await asyncio.sleep(.2)
                result = offline.operator_result(args["id"])
                if not result:
                    state, message = "uncertain", "The run changed before a terminal outcome could be verified."
                elif result.get("stopped") is True:
                    state, message = "done", "The offline raid stopped and its cleanup finished."
                else:
                    state, message = "failed", "The offline raid ended with another outcome; inspect its raid record."
        except ValueError as e:
            state, message = "refused", str(e)
        except asyncio.CancelledError:
            state, message = "uncertain", "The mind stopped before the action's outcome could be confirmed."
            raise
        except Exception:
            pass
        finally:
            self.operation.update(state=state, message=message)
            log("ops_action", action=args["action"], id=self.operation["id"], outcome=state)

    async def close(self):
        if self.busy:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
