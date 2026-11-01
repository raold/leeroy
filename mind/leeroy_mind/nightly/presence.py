"""The human's presence as the night scheduler reads it (P1c Task 4): online, offline or not
known yet, and since when. The bridge's hello says where he is now (a non-zero human_guid is
online), and his own login and logout say when that changes. A dropped bridge counts him offline
as of the drop: nothing can be played on a server the mind cannot reach, and a hello says
otherwise once it is back. Nothing is stored. The mind starts not knowing, and its start counts
as the last change, so a night can still run on a day the bridge never comes up."""
from typing import Literal

from leeroy_mind.clock import Clock
from leeroy_mind.protocol import Hello, Login, Logout

Change = Literal["login", "logout"]


class Presence:
    """`online` is True, False or None (not known yet); `changed` is when it last took a new
    value, the start at first. A word that repeats the state, such as the hello after a
    reconnect, leaves `changed` where it was."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.online: bool | None = None
        self.changed = clock.now()

    def _say(self, online: bool) -> Change:
        if self.online != online:
            self.online = online
            self.changed = self.clock.now()
        return "login" if online else "logout"

    def on_event(self, ev) -> Change | None:
        """What a bridge event says about him: a hello naming him (a non-zero human_guid) is a
        login and one naming nobody a logout; a Login or Logout counts only for him (human)."""
        if isinstance(ev, Hello):
            return self._say(bool(ev.human_guid))
        if isinstance(ev, (Login, Logout)) and ev.human:
            return self._say(isinstance(ev, Login))
        return None

    def on_connection(self, up: bool) -> Change | None:
        """A drop turns online into offline as of now ("logout"); anything else changes
        nothing, and a connect waits for the hello that follows it."""
        if not up and self.online:
            return self._say(False)
        return None

    def offline_seconds(self) -> float:
        """0 while he is online; offline or not known, the seconds since the last change (never
        below 0, should the clock be set back)."""
        if self.online:
            return 0.0
        return max(0.0, self.clock.now() - self.changed)
