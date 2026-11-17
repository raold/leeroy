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
- vary {go[, guids]}: a varied set (varied.py) for every cast member of the lust style (or only
  those of guids), written as "4 more" writes it, plus each set's four looks; go false plans
  only, with how many waiting sets each would pass. Needs rendering and `[selfies] varied_sets`
  on to queue; once per member per local day.

Only cast members have photos: on the stored roster at the cast's level, not the owner, with a
stored persona. Each change commits first, on the loop; then that member's files are made to
agree with mind.db in a thread (files.sync_chosen, whose rename is retried while the console's
face endpoint holds chosen.jpg), so the loop never waits on a held file. A file that still cannot
be written leaves the change in force and the answer says so; the member's next change, the same
pick again, or the mind's next start (sync_all) writes it. A photo whose candidate and history
copy are both gone cannot be written at all: the answer says to pick another. One change at a
time: the endpoint's lock serializes them, held until the files are written.

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
from .kind import chosen_style
from .prompt import HELD, renderable
from .style import suggestive
from .store import (SelfieError, counts, current, member_of, photos, pick, picks, queue_more,
                    undo)

ACTIONS = ("status", "pick", "undo", "more", "vary")
SOURCE = "console"
_GUID_DIR = re.compile(r"[1-9][0-9]{0,9}")
_GUID_MAX = (1 << 32) - 1
_GONE = ", but the photo's file is gone; pick another photo."
_NOT_WRITTEN = (", but the photo file could not be updated yet; picking that photo again retries "
                "now.")
_NOT_REMOVED = (", but the old photo file could not be removed yet; the mind removes it at its "
                "next start.")


def queue_looks_set(store, cfg: SelfieCfg, p, guid: int, *, human: int, now: float,
                    day: str, data_dir) -> tuple[int, bool, str]:
    """The set that renders her new figure after a weight or bust edit: (its number, varied,
    state), state "new" (queued now), "queued" (one was queued already: it renders from the
    persona as it is then) or "rendering" (the queued one has started: its <set>.part staging
    folder exists, so it renders her previous looks; render._stale queues a fresh set once it
    is ready). A new set is queued as his "4 more" queues one (store.queue_more: the sets
    waiting for his choice passed, kept in her gallery), a varied set for a woman of the lust
    style while `varied_sets` is on (varied.queue, without its once-a-day rule). The set is
    marked (store.LOOKS_EDIT_SET_KEY) to hold her cover's face lightly, so her new figure shows,
    in every state: a rendering set that fails or is cancelled renders again with the mark;
    once he picks one as her photo, later sets carry that fuller cover as usual."""
    from .store import LOOKS_EDIT_SET_KEY
    with store.transaction():
        row = store.conn.execute("SELECT set_no FROM selfie_set WHERE guid=? AND "
                                 "status='queued'", (guid,)).fetchone()
    if row is not None:
        # marked even while it renders: a render that started chose its faces already, but one
        # that fails or is cancelled (his login cancels the booth) renders again from the new
        # looks and must take the light face then (controller ruling, 2026-11-15)
        store.set_meta(LOOKS_EDIT_SET_KEY.format(guid=guid, set_no=row[0]), "1")
        rendering = files.staging_dir(data_dir, guid, row[0]).exists()
        return row[0], False, "rendering" if rendering else "queued"
    set_no, varied_set = None, False
    if (getattr(cfg, "varied_sets", False) and suggestive(p)
            and renderable(p, chosen_style(store, p) or "plain") is None):
        from . import varied
        report = varied.queue(store, human=human, now=now, day=day, go=True, guids=[guid],
                              daily=False)
        if report["queued"]:
            set_no, varied_set = report["queued"][0]["set"], True
    if set_no is None:
        set_no = queue_more(store, guid, now=now).set_no
    store.set_meta(LOOKS_EDIT_SET_KEY.format(guid=guid, set_no=set_no), "1")
    return set_no, varied_set, "new"


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
            elif action == "vary":
                reply = self._vary(args["go"], args.get("guids"))
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
        change = pick(self.app.store, guid, set_no, idx, source=SOURCE, now=self.app.clock.now(),
                      lust_held=not suggestive(self.app.personas.base(guid)))
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
        p = self.app.personas.base(guid)
        why = renderable(p, chosen_style(self.app.store, p) if p is not None else "plain")
        if why is not None and why not in HELD:
            raise SelfieError(f"{why}: the night renders photos once the looks and photo kind "
                              "are in")
        queued = queue_more(self.app.store, guid, now=self.app.clock.now())
        said = ("Four more are queued; they wait until her photo kind is chosen again for her "
                "photo style." if why in HELD else "Four more will be rendered tonight.")
        return {"ok": True, "set": queued.set_no, "guid": guid, "message": said}

    def queue_for_looks(self, guid: int) -> tuple[int | None, str]:
        """A fresh profile set after his weight or bust edit (personas.looks_edit): queue_looks_set
        below. (the set's number or None, the words for the console)."""
        app = self.app
        if not self.cfg.enabled:
            return None, "Rendering is off in mind.toml ([selfies] enabled = false), so no new set."
        p = app.personas.base(guid)
        style = chosen_style(app.store, p) if p is not None else "plain"
        why = renderable(p, style)
        if why is not None and why not in HELD:
            return None, f"No new set yet ({why}): the night renders one once the looks and photo kind are in."
        with app.store.transaction():
            waiting = app.store.conn.execute(
                "SELECT count(*) FROM selfie_set WHERE guid=? AND status='ready' AND "
                "decided IS NULL", (guid,)).fetchone()[0]
        set_no, varied, state = queue_looks_set(
            app.store, self.cfg, p, guid, human=app.human_guid, now=app.clock.now(),
            day=app.clock.local().date().isoformat(), data_dir=app.data_dir)
        their = "her" if p.identity.player_gender == "female" else "his"
        keep = f"with {their} new looks; pick one as {their} photo to keep it."
        if state == "rendering":
            return set_no, (f"{their.capitalize()} set is rendering now from {their} previous "
                            f"looks; a set with {their} new looks follows it. Pick one as "
                            f"{their} photo to keep it.")
        from .kind import wardrobe_pending
        if why is None and not varied and state == "queued":
            from .varied import plan_of
            with app.store.transaction():
                varied = plan_of(app.store.conn, guid, set_no) is not None
        if why is None and not varied and wardrobe_pending(app.store, p):
            # the photo pass chooses the kind again for the wardrobe first (render._held)
            queued = "A set is already queued" if state == "queued" else "A new set is queued"
            return set_no, (f"{queued}; it renders once tonight's photo pass has chosen "
                            f"{their} photo kind again for {their} wardrobe. Pick one as "
                            f"{their} photo to keep it.")
        if state == "queued":
            return set_no, f"A set is already queued: it renders tonight {keep}"
        kind_words = "A new varied set" if varied else "A new set"
        if why in HELD:
            said = (f"{kind_words} is queued; it waits until {their} photo kind is chosen again "
                    f"for {their} photo style.")
        else:
            when = "tonight" + (" or at the photo booth's next pass" if self.cfg.booth else "")
            said = f"{kind_words} renders {when} {keep}"
        if waiting:
            said += f" The set waiting for your choice stays in {their} gallery."
        return set_no, said

    def _vary(self, go: bool, guids=None) -> dict:
        from . import varied
        if go and not self.cfg.enabled:
            raise SelfieError("rendering is off in mind.toml ([selfies] enabled = false)")
        if go and not self.cfg.varied_sets:
            raise SelfieError("varied sets are off in mind.toml ([selfies] varied_sets = false)")
        app = self.app
        report = varied.queue(app.store, human=app.human_guid, now=app.clock.now(),
                              day=app.clock.local().date().isoformat(), go=go, guids=guids)
        n = len(report["queued"])
        log("selfies_varied", go=go, queued=n, skipped=len(report["skipped"]))
        said = (f"{n} varied sets will be rendered at night." if go
                else f"{n} varied sets would be queued.")
        return {"ok": True, **varied.summary(report), "message": said}

    async def _synced(self, guid: int, said: str) -> str:
        """`said`, once the member's files agree with what was just committed (off the loop:
        the rename's retries sleep); when they cannot be written, with what happens next: a
        photo whose file is gone needs another pick, one that could not be written yet is
        written by picking it again, and a photo file left after an undo to none goes at the
        mind's next start."""
        photo = current(self.app.store, guid)
        try:
            await asyncio.to_thread(files.sync_chosen, self.app.data_dir, guid, photo)
        except OSError as e:
            log("selfie_sync_failed", level=logging.WARNING, guid=guid, error=type(e).__name__)
            if photo is None:
                return said[:-1] + _NOT_REMOVED
            return said[:-1] + (_GONE if isinstance(e, FileNotFoundError) else _NOT_WRITTEN)
        return said

    # -- the mind's start ------------------------------------------------------------------------

    def sync_all(self) -> int:
        """At the mind's start, before any night can render: each member with a photo in force
        or a chosen.jpg gets files that agree with mind.db (a crash between a change's commit and
        its files), after what a crashed render or rename left is removed: sets/<n>.part folders
        and *.tmp files. A member that cannot be repaired is logged and never stops the rest.
        How many members' photo files it changed. Called once, by attach_selfies in
        MindApp.__init__: it removes every .part folder, so never while a night may render."""
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
