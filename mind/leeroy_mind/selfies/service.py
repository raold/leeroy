"""The running mind's side of the `selfie` op (endpoint.ControlEndpoint.selfie): the one writer of
profile photo picks while the mind runs, and the repair of the photo files at its start.

Actions, with args as endpoint.selfie_args returns them:
- status: the counts, and the newest changes each saying whether an undo of it would be taken
  now (two can be at once, as the tuning's);
- pick {guid, set, index}: that candidate becomes the member's photo; the same photo again
  writes nothing;
- undo {pick}: that change reversed, only while the photo is still what it left (store.undo);
- more {guid}: his "4 more", a set queued for tonight's render and every waiting set of his
  passed; needs rendering on and the member's looks and photo kind.

Only cast members have photos: on the stored roster at the cast's level, not the owner, with a
stored persona. Each change commits first, on the loop; then that member's files are made to
agree with mind.db in a thread (files.sync_chosen, whose rename is retried while the console's
face endpoint holds chosen.jpg), so the loop never waits on a held file. A file that still cannot
be written leaves the change in force and the answer says so; the member's next change, the same
pick again, or the mind's next start (sync_all) writes it. One call at a time: the endpoint's
lock serializes them.

Logs carry the action and its outcome, and for a file that could not be written the member's
guid and the error's type: never a name or a refusal's text."""
import asyncio
import logging
import re
import shutil
import time
from pathlib import Path

from leeroy_mind.logs import log
from leeroy_mind.wow import CAST_MIN_LEVEL

from . import files
from .config import SelfieCfg
from .prompt import renderable
from .store import (SelfieError, counts, current, member_of, photos, pick, picks, queue_more,
                    undo)

ACTIONS = ("status", "pick", "undo", "more")
SOURCE = "console"
_GUID_DIR = re.compile(r"[1-9][0-9]{0,9}")
_GUID_MAX = (1 << 32) - 1
_NOT_WRITTEN = (", but the photo file could not be updated yet; the mind tries again at its next "
                "start.")


class SelfieService:
    def __init__(self, app, cfg: SelfieCfg):
        self.app = app
        self.cfg = cfg

    async def handle(self, args: dict) -> dict:
        action = args["action"]
        if action not in ACTIONS:
            return {"ok": False, "error": f"a photo action is one of {', '.join(ACTIONS)}"}
        if action == "status":
            return self.status()
        try:
            if action == "pick":
                reply = await self._pick(args["guid"], args["set"], args["index"])
            elif action == "undo":
                reply = await self._undo(args["pick"])
            else:
                reply = self._more(args["guid"])
        except SelfieError as e:
            log("selfie_change", action=action, outcome="refused")
            return {"ok": False, "error": str(e)}
        same = action == "pick" and reply["pick"] is None
        log("selfie_change", action=action, outcome="unchanged" if same else "ok")
        return reply

    def status(self) -> dict:
        store = self.app.store
        with store.transaction():  # one snapshot, ended at once, so it never pins the WAL
            n = counts(store.conn)
            recent = picks(store.conn)
        return {"ok": True, "enabled": self.cfg.enabled, **n, "recent": recent}

    # -- the changes -----------------------------------------------------------------------------

    def _cast_member(self, guid: int) -> None:
        app = self.app
        m = app.store.cast().get(guid)
        if guid == app.human_guid or m is None or m.level < CAST_MIN_LEVEL:
            raise SelfieError("no such member in the cast")
        if guid not in app.store.persona_versions():
            raise SelfieError("only cast members with a stored persona have photos")

    async def _pick(self, guid: int, set_no: int, idx: int) -> dict:
        self._cast_member(guid)
        change = pick(self.app.store, guid, set_no, idx, source=SOURCE, now=self.app.clock.now())
        said = ("That is already their photo." if change is None
                else f"Photo set (change #{change}).")
        return {"ok": True, "pick": change, "guid": guid,
                "message": await self._synced(guid, said)}

    async def _undo(self, change: int) -> dict:
        app = self.app
        made = undo(app.store, change, source=SOURCE, now=app.clock.now())
        guid = member_of(app.store, made)
        return {"ok": True, "pick": made, "guid": guid,
                "message": await self._synced(guid, f"Undone (change #{made}).")}

    def _more(self, guid: int) -> dict:
        self._cast_member(guid)
        if not self.cfg.enabled:
            raise SelfieError("rendering is off in mind.toml ([selfies] enabled = false)")
        why = renderable(self.app.personas.base(guid))
        if why is not None:
            raise SelfieError(f"{why}: the night renders photos once the looks and photo kind "
                              "are in")
        queued = queue_more(self.app.store, guid, now=self.app.clock.now())
        return {"ok": True, "set": queued.set_no, "guid": guid,
                "message": "Four more will be rendered tonight."}

    async def _synced(self, guid: int, said: str) -> str:
        """`said`, once the member's files agree with what was just committed (off the loop:
        the rename's retries sleep); with a word that they do not when they cannot be written."""
        photo = current(self.app.store, guid)
        try:
            await asyncio.to_thread(files.sync_chosen, self.app.data_dir, guid, photo)
        except OSError as e:
            log("selfie_sync_failed", level=logging.WARNING, guid=guid, error=type(e).__name__)
            return said[:-1] + _NOT_WRITTEN
        return said

    # -- the mind's start ------------------------------------------------------------------------

    def sync_all(self) -> int:
        """At the mind's start, before any night can render: each member with a photo in force
        or a chosen.jpg gets files that agree with mind.db (a crash between a change's commit and
        its files), after what a crashed render or rename left is removed: sets/<n>.part folders
        and *.tmp files. A member that cannot be repaired is logged and never stops the rest.
        How many members' photo files it changed."""
        began = time.monotonic()
        data = self.app.data_dir
        on_disk = _member_guids(data)
        swept = sum(self._sweep(guid) for guid in on_disk)
        held = photos(self.app.store)
        todo = sorted(held.keys() | {g for g in on_disk if files.chosen(data, g).exists()})
        changed = failed = 0
        for guid in todo:
            try:
                if files.sync_chosen(data, guid, held.get(guid)):
                    changed += 1
            except OSError as e:  # FileNotFoundError: the photo is gone; PermissionError: held
                failed += 1
                log("selfie_sync_failed", level=logging.WARNING, guid=guid,
                    error=type(e).__name__)
        log("selfies_synced", members=len(todo), changed=changed, failed=failed, swept=swept,
            seconds=round(time.monotonic() - began, 2))
        return changed

    def _sweep(self, guid: int) -> int:
        member = files.member_dir(self.app.data_dir, guid)
        n = 0
        try:
            for part in sorted((member / "sets").glob("*.part")):
                if part.is_dir():
                    shutil.rmtree(part)
                    n += 1
            for tmp in sorted(member.rglob("*.tmp")):
                if tmp.is_file():
                    tmp.unlink()
                    n += 1
        except OSError as e:
            log("selfie_sweep_failed", level=logging.WARNING, guid=guid, error=type(e).__name__)
        return n


def _member_guids(data_dir) -> list[int]:
    """The guids with a folder under selfies/; anything else there is left alone."""
    root = Path(data_dir) / "selfies"
    try:
        names = [p.name for p in root.iterdir() if p.is_dir()]
    except FileNotFoundError:
        return []
    except OSError as e:
        log("selfie_sweep_failed", level=logging.WARNING, error=type(e).__name__)
        return []
    return sorted(int(n) for n in names if _GUID_DIR.fullmatch(n) and int(n) <= _GUID_MAX)
