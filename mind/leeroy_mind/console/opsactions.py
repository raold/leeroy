"""The console's confirmation and lifecycle coordinator for operator actions.

A short-lived, one-use confirmation is bound to the complete request. One reservation
covers the whole stop/release/start sequence. The CLI is the write authority and uses this
checkout's interpreter; stdout/stderr are discarded, never displayed or logged.
"""
import asyncio
import hmac
import json
import secrets
import sqlite3
import subprocess
import time
from contextlib import closing
from uuid import uuid4

from ..opscontrol import ACTIONS, arguments, release_record
from ..realm.action_store import ActionStore, release_reason
from .control import Refused
from .httpd import json_response
from .mindlink import LinkError


class OperationFailed(ValueError):
    pass


def run_release(app, args):
    """Run the documented CLI with live code, then read its committed result."""
    with closing(sqlite3.connect((app.s.data_dir / "mind.db").resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT 1 FROM guild_raid WHERE ended IS NULL LIMIT 1").fetchone():
            raise ValueError("A raid opened before the mind stopped; nothing was released.")
        row = conn.execute("SELECT value FROM meta WHERE key='guild:offline:owned'").fetchone()
        if row and json.loads(row[0]).get("phase") not in {"complete", "rejected"}:
            raise ValueError("An offline run is still owned; nothing was released.")
        release_record(conn, args["id"], time.time())
    cmd = [str(app.mind.python), "-m", "leeroy_mind", "realm", "release", args["id"],
           "--data-dir", str(app.s.data_dir), "--reason="+args["reason"], "--yes"]
    result = subprocess.run(cmd, cwd=app.s.root / "mind", stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with closing(sqlite3.connect((app.s.data_dir / "mind.db").resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        record = ActionStore(conn).get(args["id"])
        if result.returncode == 0 and record is not None and record.state == "completed" and record.result_reason == "abandoned":
            return "The Realm root was released."
        # Reuse the CLI's own fixed refusal words, without printing its native evidence.
        release_record(conn, args["id"], time.time())
    raise RuntimeError("release_not_confirmed")


class OpsActions:
    def __init__(self, app, *, release=run_release, recover_s=60.0, recover_poll_s=1.0):
        self.app, self.release = app, release
        self.recover_s, self.recover_poll_s = recover_s, recover_poll_s
        self.task, self.operation, self.preview = None, None, None

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def view(self):
        enabled = self.app.s.ops_actions and self.app.controls.enabled
        return {"enabled": bool(enabled), "reason": "" if enabled else
                "Ops actions are off in this console.", "operation": self.operation}

    def gate(self):
        a = self.app
        if not a.controls.enabled or not a.s.ops_actions:
            raise Refused(403, "Ops actions are off in this console.")
        if self.busy or a.controls.busy is not None:
            raise Refused(409, "Another action is still running.")
        if a.view.state != "running" or a.link is None:
            raise Refused(409, "The mind must be running with a verified control endpoint.")

    async def call(self, args):
        return await asyncio.to_thread(self.app.link.call, self.app._mind_pids(), "ops", args, timeout=5)

    async def post(self, request):
        from ..endpoint import BadRequest
        try:
            raw = request.json()
            if not isinstance(raw, dict) or raw.get("phase") not in ("preview", "confirm"):
                raise BadRequest("Preview the action, then confirm it.")
            phase = raw["phase"]
            extras = {"phase", "reason"} | ({"token", "typed_id"} if phase == "confirm" else set())
            args = arguments({k: v for k, v in raw.items() if k not in extras})
            if args["action"] not in ACTIONS:
                raise BadRequest("Choose an Ops action.")
            expected = set(args) | {"phase"}
            if args["action"] == "release":
                if not release_reason(raw.get("reason")):
                    raise BadRequest("A release needs a reason of 1 to 200 printable characters.")
                args["reason"] = raw["reason"]
                expected.add("reason")
            if phase == "confirm":
                expected.add("token")
                if args["action"] == "release":
                    expected.add("typed_id")
            if set(raw) != expected:
                raise BadRequest("Unexpected or missing confirmation fields.")
            self.gate()
            if args["action"] == "release":
                self.app.controls.check("mind", "stop")
            # Fresh mind state on BOTH preview and confirmation, never the cached Ops panel.
            inspected = await self.call({"action": "inspect", "kind": args["action"],
                                         **{k: v for k, v in args.items() if k not in {"action", "reason"}}})
            if inspected.get("ok") is not True:
                raise Refused(409, inspected.get("error", "The mind refused the action."))
            self.gate()  # another request could have started during the control round-trip
            if phase == "preview":
                token = secrets.token_urlsafe(24)
                self.preview = (token, time.monotonic()+180, args)
                return json_response(200, {"ok": True, "confirm": True, "token": token,
                    "details": inspected.get("details"), "message": "Confirm this one action. The current state will be checked again."})
            saved, self.preview = self.preview, None
            if (saved is None or time.monotonic() > saved[1] or saved[2] != args
                    or not isinstance(raw.get("token"), str)
                    or not hmac.compare_digest(raw["token"].encode("utf-8", "surrogatepass"), saved[0].encode())):
                raise Refused(409, "The confirmation expired or changed; preview it again.")
            if args["action"] == "release" and raw.get("typed_id") != args["id"]:
                raise Refused(409, "The typed root id did not match; nothing was released.")
            # Claim before creating the task: there is no gap for a lifecycle POST to enter.
            self.app.controls.reserve_ops()
            self.operation = {"id": uuid4().hex, "action": args["action"], "target": args.get("id"),
                              "state": "running", "message": "The action is starting."}
            self.task = asyncio.create_task(self._run(args), name="console-ops-action")
            return json_response(202, {"ok": True, "operation": dict(self.operation)})
        except BadRequest as e:
            return json_response(400, {"ok": False, "error": str(e)})
        except Refused as e:
            return json_response(e.status, {"ok": False, "error": str(e)})
        except LinkError:
            return json_response(409, {"ok": False, "error": "The mind could not verify this action; nothing started."})

    async def _release(self, args):
        a = self.app
        # Stop/restart run off the event loop, through the existing ownership checks.
        self.operation["message"] = "Stopping the mind cleanly before releasing one root."
        try:
            stopped = await asyncio.to_thread(a.mind.clean_stop)
            clean = stopped.code == 0 and stopped.state == "stopped"
        except Exception:
            clean = False
        if not clean:
            self.operation.update(release_outcome="not_started", restart_outcome="pending",
                                  message="The clean stop was not proved; checking the mind.")
            if await self._recover():
                self.operation["restart_outcome"] = "running"
                raise ValueError("The mind did not stop cleanly; nothing was released. The mind is running.")
            self.operation["restart_outcome"] = "failed"
            raise OperationFailed("The mind did not stop cleanly; nothing was released. "
                                  "The mind is not confirmed running; inspect it before retrying.")
        result, release_error = None, None
        self.operation.update(release_outcome="uncertain", restart_outcome="pending")
        try:
            result = await asyncio.to_thread(self.release, a, args)
            self.operation["release_outcome"] = "released"
        except Exception as e:
            release_error = e
            self.operation["release_outcome"] = "refused" if isinstance(e, ValueError) else "uncertain"
        finally:
            self.operation["message"] = "Starting the mind again."
            try:
                started = await asyncio.to_thread(a.mind.act, "start")
                restarted = started.code == 0 and started.state == "running"
            except Exception:
                restarted = False
            self.operation["restart_outcome"] = "running" if restarted else "failed"
            if not restarted:
                release_words = {"released": "The Realm root was released.",
                    "refused": "The Realm release was refused.", "uncertain": "The release outcome is uncertain."}
                raise OperationFailed(release_words[self.operation["release_outcome"]]+
                    " The mind did not restart; inspect it before retrying.")
        if release_error is not None:
            if isinstance(release_error, ValueError):
                raise ValueError(str(release_error)+" The mind is running again.")
            raise ValueError("The release could not be confirmed; the mind was restarted.")
        return result + " The mind is running again."

    async def _recover(self):
        """After a stop not proved clean the mind may still be ending its own shutdown: wait
        for it to end or settle, and start it again once it has ended, so a release that never
        began does not leave the guild without its mind. True when it is running at the end."""
        a = self.app
        deadline = time.monotonic() + self.recover_s
        while True:
            try:
                found, held, error = await asyncio.to_thread(a.mind.probe)
            except Exception:
                found, held, error = None, False, "unreadable"
            if error is None and found == [] and not held:
                try:
                    started = await asyncio.to_thread(a.mind.act, "start")
                    return started.code == 0 and started.state == "running"
                except Exception:
                    return False
            if time.monotonic() >= deadline:
                return error is None and bool(found) and held
            await asyncio.sleep(self.recover_poll_s)

    async def _run(self, args):
        from .app import log
        state, message = "failed", "The action could not be confirmed; inspect its state before retrying."
        try:
            if args["action"] == "release":
                message = await self._release(args)
                state = "done"
            else:
                reply = await self.call(args)
                if reply.get("ok") is not True:
                    raise ValueError(reply.get("error", "The mind refused the action."))
                remote = reply.get("operation") or {}
                remote_id = remote.get("id")
                while remote.get("state") == "running":
                    self.operation["message"] = remote.get("message", "The mind is still working.")
                    await asyncio.sleep(1)
                    reply = await self.call({"action": "status"})
                    remote = reply.get("operation") or {}
                    if remote.get("id") != remote_id:
                        raise ValueError("The mind restarted or the operation changed; inspect its state before retrying.")
                state, message = remote.get("state", "uncertain"), remote.get("message", message)
        except OperationFailed as e:
            state, message = "failed", str(e)
        except ValueError as e:
            state, message = "refused", str(e)
        except asyncio.CancelledError:
            state, message = "uncertain", "The console stopped monitoring; check the mind before retrying."
            raise
        except Exception:
            pass
        finally:
            self.operation.update(state=state, message=message)
            self.app.controls.busy = None
            log.info("ops_action action=%s id=%s outcome=%s", args["action"], self.operation["id"], state)
            await self.app._after_control()

    async def close(self):
        if self.busy:
            if self.operation["action"] == "release":
                from ..selfies.worker import to_the_end
                await to_the_end(self.task)  # never abandon restart after a clean stop
            else:
                self.task.cancel()  # remote work stays owned by the mind
                await asyncio.gather(self.task, return_exceptions=True)
