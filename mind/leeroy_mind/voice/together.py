"""Shared voice-room time, through P1c's attraction API when it is installed.

No new gain curve lives here. Before P1c exists, stretches are only logged;
VoiceStore's normal seat history remains the record of where members were.
"""
import importlib
import logging
from dataclasses import dataclass

from leeroy_mind.logs import log
from leeroy_mind.personas.io import load_persona
from leeroy_mind.voice.rooms import RAID_ROOMS


@dataclass(frozen=True)
class Stretch:
    since: float
    room: str
    session: int
    human: int


class RoomTogether:
    def __init__(self, ext):
        self.ext = ext
        self.open: dict[int, Stretch] = {}

    def _eligible(self):
        ext, app = self.ext, self.ext.app
        if not ext.comms.available() or app.session_id is None:
            return {}
        room, human = ext.rooms.human_room(), app.human_guid
        if room is None or room in (*RAID_ROOMS, "afk") or app.world.human_afk:
            return {}
        out = {}
        for guid in ext.rooms.occupants(room) & ext.on_comms & app.world.online:
            if (guid == human or guid in app.world.afk
                    or (app.world.in_group(human) and app.world.in_group(guid))):
                continue
            p = app.personas.get(guid)
            if p is not None and p.identity.player_gender == "female" and p.romance.stance in {"fond", "drawn", "lust"}:
                out[guid] = (room, app.session_id, human)
        return out

    def tick(self, now):
        eligible = self._eligible()
        for guid, stretch in list(self.open.items()):
            if eligible.get(guid) != (stretch.room, stretch.session, stretch.human):
                self.close(guid, now)
        for guid, (room, session, human) in eligible.items():
            self.open.setdefault(guid, Stretch(now, room, session, human))

    def before_move(self, guid, now):
        if guid == self.ext.app.human_guid:
            self.close_all(now)
        else:
            self.close(guid, now)

    def close_all(self, now):
        for guid in list(self.open):
            self.close(guid, now)

    def close(self, guid, now):
        stretch = self.open.pop(guid, None)
        if stretch is None or now <= stretch.since:
            return
        seconds = now - stretch.since
        try:
            credited, reason = self._credit(guid, stretch, seconds, now)
        except Exception as exc:
            # Credit is optional; its rollback must not prevent AFK, logout or room moves.
            credited, reason = False, "credit_failed"
            log("room_together_credit_failed", level=logging.WARNING, guid=guid,
                error=type(exc).__name__)
        log("room_together", guid=guid, seconds=round(seconds, 3), session=stretch.session,
            credited=credited, reason=reason)

    def _credit(self, guid, stretch, seconds, now):
        credited, reason = False, "api_absent"
        try:
            api = importlib.import_module("leeroy_mind.nightly.attraction")
        except ModuleNotFoundError as exc:
            if exc.name not in {"leeroy_mind.nightly", "leeroy_mind.nightly.attraction"}:
                raise
            api = None
        required = ("reconcile", "gain", "apply_act", "science_for", "TOGETHER_MIN_S", "TOGETHER_BLOCK_S")
        if api is not None and all(hasattr(api, name) for name in required):
            reason = "too_short"
            if seconds >= api.TOGETHER_MIN_S:
                app, changed = self.ext.app, False
                with app.store.transaction():
                    state = api.reconcile(app.store, guid, now=now)
                    if state is not None:
                        persona = load_persona(app.store, guid)
                        if persona is not None:
                            science = api.science_for(app.store, guid, human=stretch.human,
                                                      now=now, local=app.clock.local())
                            amount, mult = api.gain("quest_together", persona, science=science)
                            result = api.apply_act(
                                app.store, human=stretch.human, guid=guid, act="quest_together",
                                amount=amount * max(1, int(seconds // api.TOGETHER_BLOCK_S)), mult=mult,
                                ref=f"session:{stretch.session}", ts=now, live=True, stance=state[0])
                            changed = bool(state[1]) or result is not None
                            credited, reason = True, "live"
                    if not credited:
                        reason = "ineligible_at_close"
                if changed:
                    app.personas.invalidate(guid)
        return credited, reason
