"""The running mind's side of the `tune` op (endpoint.ControlEndpoint.tune): the one writer of
the owner's dials while the mind runs.

A change is checked against the live cast (tuning.changes.check_set), written as one change in
mind.db, and in force at once: the persona cache takes the new values (talk reads them from the
next turn), and a world dial is merged over mind.toml and applied to the talk engine
(chattiness rescales the pending gap, jealousy the asides' rate). What the night reads moves
with the next night's run. A preview never writes anything (tuning.preview).

Logs carry the action and the outcome only: never a name, a value's persona or preview text."""
import logging
from dataclasses import replace

from leeroy_mind.logs import log as log_event
from leeroy_mind.personas.io import load_tuning

from . import changes as C
from . import groups as G
from .dials import MEMBER_DIALS, WORLD_DIALS, applies

log = logging.getLogger("leeroy_mind.tuning")


class TuningService:
    def __init__(self, app):
        self.app = app

    # -- the live picture ------------------------------------------------------------------------

    def picture(self) -> C.Picture:
        app = self.app
        personas = {g: p for g in app.store.persona_versions()
                    if (p := app.personas.base(g)) is not None}
        pic = C.picture(app.store, personas)
        return replace(pic, human=app.human_guid or pic.human)

    def reach(self, scope: str, target: str, dial: str, pic: C.Picture | None = None) -> int:
        """How many members a value reaches now: the cast members with a stored persona (the
        owner left out) in its scope for whom the dial means something."""
        pic = pic or self.picture()
        if scope == "member":
            return 1
        members = [(p, pic.cast.get(g)) for g, p in pic.personas.items()
                   if g != pic.human and g in pic.cast]
        if scope == "world":
            return len(members)
        group = self.app.personas.tuning.groups.get(target)
        if group is None:
            return 0
        return sum(1 for p, m in members if (group.builtin or G.matches(group.where, p, m))
                   and applies(dial, p))

    # -- the op ----------------------------------------------------------------------------------

    async def handle(self, args: dict) -> dict:
        action = args["action"]
        try:
            if action == "nudge":
                return await self._nudge(args)
            if action == "edit":
                return self._edit(args)
            if action == "status":
                return self.status()
            if action == "preview":
                from .preview import preview
                return await preview(self.app, args)
            return self._write(action, args)
        except (C.TuneError, G.GroupError, ValueError) as e:
            code = getattr(e, "code", "")
            log_event("tune_refused", action=action, **({"code": code} if code else {}))
            return {"ok": False, "error": str(e), **({"code": code} if code else {})}

    async def _nudge(self, args):
        from . import nudges as N
        verb, app = args["verb"], self.app
        if verb == "list":
            return {"ok": True, **N.view(app.store.conn, now=app.clock.now())}
        if verb == "preview":
            from .nudge_preview import preview
            return await preview(app, args)
        if verb == "set":
            pic = self.picture()
            r = N.set_nudge(app.store, args, pic, app.engine.human,
                            source="console", now=app.clock.now())
        else:
            r = N.retire(app.store, args["id"], source="console", now=app.clock.now())
        self.refresh()
        if verb == "set" and r.change and (signals := getattr(app, "signals", None)) is not None:
            signals.nudged(args, pic)
        log_event("tune_written", action="nudge_" + verb, change=r.change or 0, rows=r.rows)
        return {"ok": True, "change": r.change, "rows": r.rows,
                "version": app.personas.tuning.version,
                "message": f"Saved as change #{r.change}." if r.change else "Already retired."}

    def _dial(self) -> int:
        """[life] wow_vs_life, which a life event's on-mind line reads as the night does."""
        from leeroy_mind.nightly.settings import load_life
        try:
            return load_life(self.app.data_dir / "mind.toml")
        except Exception:
            return 5

    def _edit(self, args):
        """Godmode edits (tuning.edits): list at any time, retire while off too (a cleanup);
        set and preview only with [godmode] enabled. Logs carry the kind, the ids and the
        outcome, never the edit's text."""
        from leeroy_mind.godmode.config import app_enabled
        from . import edits as E
        verb, app = args["verb"], self.app
        now = app.clock.now()
        if verb == "list":
            return {"ok": True, **E.view(app.store.conn, now=now)}
        if verb in ("set", "preview") and not app_enabled(app):
            return {"ok": False, "code": "off", "error": "Godmode is off: set [godmode] enabled = "
                    "true in mind.toml and restart the mind."}
        if verb == "preview":
            out = E.preview(app.store, args, human=app.engine.human, now=now, pic=self.picture())
            log_event("tune_previewed", action="edit", kind=args["kind"])
            return out
        if verb == "set":
            r = E.set_edit(app.store, args, human=app.engine.human, source="console", now=now,
                           pic=self.picture(), dial=self._dial())
        else:
            pic = self.picture()
            r = E.retire(app.store, args["id"], source="console", now=now, human=pic.human,
                         dial=self._dial())
        self.refresh()
        log_event("tune_written", action="edit_" + verb, kind=r["kind"], change=r["change"] or 0,
                  edit=r["id"])
        return {"ok": True, "change": r["change"], "id": r["id"], "rows": r["rows"],
                "version": app.personas.tuning.version,
                "message": f"Saved as change #{r['change']}." if r["change"] else "Already retired."}

    def status(self) -> dict:
        t = self.app.personas.tuning
        return {"ok": True, "version": t.version, "pinned_members": len(t.pins),
                "world": dict(t.world), "dials": list(MEMBER_DIALS),
                "world_dials": list(WORLD_DIALS)}

    def _write(self, action: str, args: dict) -> dict:
        app, now, reach = self.app, self.app.clock.now(), 0
        pic = self.picture()
        signals = getattr(app, "signals", None)
        before = signals.tuning_values(args, pic) if signals is not None and action == "set" else {}
        if action == "set":
            s = C.Set(args["scope"], args["target"], args["dial"], args["value"])
            r = C.apply_sets(app.store, [s], pic, source="console", now=now,
                             label="clear" if s.value is None else "set",
                             previewed=args["previewed"])
            reach = self.reach(s.scope, s.target, s.dial, pic)
        elif action == "undo":
            r = C.undo(app.store, args["change"], pic, source="console", now=now,
                       dial=self._dial())
        elif action == "revert":
            r = C.revert_to(app.store, args["to"], pic, source="console", now=now,
                            dial=self._dial())
        elif action == "group":
            r = C.save_group(app.store, args["id"], args["name"], args["where"],
                             source="console", now=now)
        else:
            r = C.retire_group(app.store, args["id"], source="console", now=now)
        self.refresh()
        if before:
            signals.tuned(before, args)
        t = app.personas.tuning
        if action == "group" and r.change is not None:
            reach = self.reach("group", str(self._group_id(r.change)), "talkative")
        log_event("tune_written", action=action, change=r.change or 0, rows=r.rows)
        message = ("Nothing to change: it already stands that way." if r.change is None
                   else f"Saved as change #{r.change}.")
        return {"ok": True, "change": r.change, "rows": r.rows, "reach": reach,
                "version": t.version, "message": message}

    def _group_id(self, change: int) -> int:
        row = self.app.store.conn.execute(
            "SELECT target FROM tuning_row WHERE change=? AND dial='definition'",
            (change,)).fetchone()
        self.app.store.conn.commit()
        return int(row[0]) if row else 0

    def refresh(self) -> None:
        """The values in mind.db in force now: the persona cache, and the world dials over
        mind.toml for the talk engine when they moved."""
        app = self.app
        before = app.personas.tuning
        t = load_tuning(app.store, app.clock.now())
        app.personas.set_tuning(t)
        app.store.reload_edits()  # godmode feelings and moods (tuning.edits)
        if dict(t.world) != dict(before.world):
            from .world import merge
            app.cfg = merge(getattr(app, "file_cfg", app.cfg), t)
            app.engine.apply_config(app.cfg)
