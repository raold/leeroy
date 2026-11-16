"""LifeService: her day for the talk engine (TalkEngine.life).

`configure(cfg)` puts `[life]` in force: it pins the process-wide values (life/config.py) that
Discord presence and the replies on her own time read, and writes the availability to meta
life:availability when it changed, so the console shows what the mind uses. `presence` is where
a member is now and how closely she is watching a medium; None at availability 0 (callers keep
today's behaviour) or for a member without a persona."""
from __future__ import annotations

from leeroy_mind.discord.presence import local_time

from . import config as life_config
from .presence import LifePresence, presence_at


class LifeService:
    def __init__(self, *, store, clock, personas, world, cfg):
        self.store, self.clock, self.personas, self.world = store, clock, personas, world
        self._availability = 0
        self._state = False
        self.configure(cfg)

    @property
    def availability(self) -> int:
        return self._availability

    @property
    def state(self) -> bool:
        return self._state

    def configure(self, cfg) -> None:
        """`[life]` from a (re)loaded config, with the world dials over it."""
        life = getattr(cfg, "life", None)
        life_config.pin(getattr(life, "availability", 0), getattr(life, "state", False))
        self._availability, self._state = life_config.availability(), life_config.state_on()
        if life_config.parse(self.store.get_meta(life_config.META_KEY)) != self._availability:
            self.store.set_meta(life_config.META_KEY, str(self._availability))

    def presence(self, guid: int, *, medium: str,
                 in_his_group: bool = False) -> LifePresence | None:
        """Where she is now and her attention on `medium` ("game" or "discord")."""
        if self._availability <= 0:
            return None
        p = self.personas.get(guid)
        if p is None:
            return None
        her_local = local_time(self.clock.local(), p.identity.region)
        return presence_at(p, her_local, medium=medium, in_his_group=in_his_group)
