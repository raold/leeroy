"""Heat in the running mind: the glue between `resolve` (pure) and what it reads.

`[heat] enabled` is read once at the mind's start (attach). With it off there is no HeatLive:
`current()` is None, every reader takes the path it took before the dial, and stored heat rows
do nothing. With it on and no world value set, the world heat follows `[romance] dial` (9 is 9),
so nothing moves until the owner moves the dial.

The module-level functions are pure over what they are given, so the console (another process,
reading mind.db read-only) builds its page from the very same code."""
import logging
from collections import deque

from leeroy_mind.discord.channels import is_private
from leeroy_mind.logs import log as log_event

from .config import HeatConfig, load_heat_config
from .resolve import ATTITUDES, Member, Resolved, resolve
from .session import SessionBook, parse
from .table import DIAL_TO_HEAT, P4, ROWS

log = logging.getLogger("leeroy_mind.heat")

LED_WINDOW_S = 3 * 3600.0  # his heat lines count toward her lead for this long
ACK = {  # the one instruction line on her next private turn to him; built-in, non-explicit
    "clean": ("{name} just asked you to keep things clean for now. Say you will, once, in your "
              "own words, and keep to it."),
    "open": ("{name} just told you not to hold back tonight. Acknowledge it once, in your own "
             "words."),
}
_LIVE = None  # the running mind's HeatLive (attach), for readers with no app in reach


def current():
    """The running mind's HeatLive, or None with `[heat]` off (or no mind in this process)."""
    return _LIVE


def enabled() -> bool:
    return _LIVE is not None


def photo_tier(p) -> int:
    """Her photo tier now; P4 (the lists as they are) with heat off."""
    return P4 if _LIVE is None else _LIVE.photo_tier(p)


def sends(p, scene, look=None) -> bool:
    """Whether a stored photo may be sent or offered at her photo tier now: its scene (or a
    varied set's look) is within the tier. Always True with heat off. A photo above the tier is
    only passed over: nothing is expired, hidden, deleted or rendered again, and raising the
    heat makes it eligible again."""
    if _LIVE is None:
        return True
    from . import photos
    tier = _LIVE.photo_tier(p)
    return (photos.look_tier(look) if look is not None else photos.scene_tier(scene)) <= tier


def offers_of(p) -> float:
    """Heat's multiplier on the chance a turn to him offers a `him` photo; 1.0 with heat off."""
    return 1.0 if _LIVE is None else _LIVE.offers(p)


def starts_of():
    """For the whisper clocks: a function of a member's persona giving heat's multiplier on the
    romantic share of her starts, or None with heat off (the clocks as they were)."""
    return None if _LIVE is None else _LIVE.starts


def never_starts(p) -> bool:
    """Heat says she never starts it (heat 0): her own starts are casual. False with heat off."""
    return _LIVE is not None and _LIVE.never_starts(p)


# -- pure, shared with the console ----------------------------------------------------------------

def member_of(p, tuning=None, cast_member=None) -> Member:
    """What resolve reads of `p` (an effective or stored persona). The lust photo style and why
    not are decided here, by selfies.style (stance, 25 or older, the looks' age guard), before
    heat is consulted."""
    from leeroy_mind.selfies.style import SUGGESTIVE_MIN_AGE, looks_age_coded, suggestive
    female = p.identity.player_gender == "female"
    stance = p.romance.stance
    own = p.romance.dial if female and stance in ATTITUDES else None
    offset, groups = tuning.heat_of(p, cast_member) if tuning is not None else (None, 0)
    lust = suggestive(p)
    aged = (female and stance == "lust" and not lust
            and (p.identity.age < SUGGESTIVE_MIN_AGE or looks_age_coded(p)))
    return Member(female=female, stance=stance, own_dial=own,
                  offset=offset if female else None, groups=groups if female else 0,
                  lust_style=lust, photo_reason="age_guard" if aged else "plain_style")


def world_of(tuning, romance_dial: int) -> tuple[int, int | None]:
    """(the world heat, the romance dial it was derived from or None when heat is set)."""
    if tuning is not None and tuning.heat_world is not None:
        return tuning.heat_world, None
    dial = max(1, min(10, int(romance_dial)))
    return DIAL_TO_HEAT[dial], dial


def derived_dial(tuning, enabled: bool) -> int | None:
    """The legacy romance dial that stands in for the world's while heat is set and on (the old
    dial becomes a derived value); None otherwise."""
    if not enabled or tuning is None or tuning.heat_world is None:
        return None
    return ROWS[max(0, min(10, tuning.heat_world))].dial


def resolve_for(p, tuning, romance_dial: int, config: HeatConfig, *, channel: str = "whisper",
                session: str | None = None, late: bool = False, his_heat: int = 99,
                cast_member=None) -> Resolved:
    world, legacy = world_of(tuning, romance_dial)
    return resolve(world, member_of(p, tuning, cast_member), private=is_private(channel),
                   session=session, public_cap=config.public_cap, late=late, his_heat=his_heat,
                   legacy=legacy)


# -- the running mind ------------------------------------------------------------------------------

class HeatLive:
    def __init__(self, app, config: HeatConfig):
        self.app, self.config = app, config
        self.sessions = SessionBook(app.store, app.clock)
        self._led: dict[int, deque] = {}
        self._last: dict[int, str] = {}

    # the values in force ---------------------------------------------------------------------

    @property
    def tuning(self):
        return self.app.personas.tuning

    def _file_dial(self) -> int:
        """[romance] dial with the world's own override, never the one derived from heat."""
        t = self.tuning
        file_cfg = getattr(self.app, "file_cfg", None) or self.app.cfg
        return t.world.get("romance", file_cfg.romance.dial)

    def _cast_member(self, guid: int):
        t = self.tuning
        if not t.heat_groups or not t.needs_cast:
            return None
        return self.app.store.cast().get(guid)

    def resolve(self, p, *, channel: str = "whisper", late: bool = False, tuning=None,
                lead: bool = True, now: float | None = None, dial: int | None = None) -> Resolved:
        """`p`'s record on `channel` now. `tuning`: a candidate tuning (a preview's), else the
        one in force. `lead` False: as if his lines had already led (a preview, the page).
        `dial`: a candidate romance dial (its preview), read only while no world heat is set."""
        now = self.app.clock.now() if now is None else now
        return resolve_for(
            p, tuning if tuning is not None else self.tuning,
            self._file_dial() if dial is None else dial, self.config,
            channel=channel, session=self.sessions.mode(p.guid, now), late=late,
            his_heat=self.led(p.guid, now) if lead else 99,
            cast_member=self._cast_member(p.guid))

    def photo_tier(self, p) -> int:
        if p is None:
            return P4
        return self.resolve(p, lead=False).photo

    def starts(self, p) -> float:
        """Her multiplier on the romantic share of the whisper and opener clocks."""
        return self.resolve(p, lead=False).starts

    def never_starts(self, p) -> bool:
        """Heat says she never starts it (heat 0): her own starts are casual."""
        if p is None:
            return False
        r = self.resolve(p, lead=False)
        return r.applies and r.starts <= 0

    def offers(self, p) -> float:
        """Her multiplier on the chance a turn to him offers a `him` photo."""
        return 1.0 if p is None else self.resolve(p, lead=False).offers

    # his lead ----------------------------------------------------------------------------------

    def heard(self, guid: int, words: str, now: float) -> None:
        """A line of his to `guid` in private: when it turns things up (romance.his_heat) it
        counts once toward her lead."""
        from leeroy_mind.talk.romance import his_heat
        words = " ".join((words or "").split())
        if not words or self._last.get(guid) == words:
            return
        self._last[guid] = words
        if his_heat(words):
            self._led.setdefault(guid, deque(maxlen=8)).append(now)

    def led(self, guid: int, now: float) -> int:
        q = self._led.get(guid)
        if not q:
            return 0
        while q and now - q[0] >= LED_WINDOW_S:
            q.popleft()
        return len(q)

    # the session override ----------------------------------------------------------------------

    def his_line(self, guid: int, text: str) -> str | None:
        """A whisper or one-to-one DM of his to `guid`: start, replace or end her session
        override when it says one of the fixed phrases. The code ("clean", "open", "clear"), or
        None. Said while he is in game it ends at his logout, else by the Discord rule. The log
        has the guid and the code, never his words."""
        medium = "game" if self._in_game() else "discord"
        self.sessions.touch(guid)
        code = parse(text)
        if code is None:
            return None
        p = self.app.personas.get(guid)
        if p is None or not member_of(p).female or p.romance.stance not in ATTITUDES:
            return None  # nothing of hers to override: never her stance
        if code == "clear":
            if not self.sessions.clear(guid):
                return None
        else:
            self.sessions.set(guid, code, medium=medium, ack=True)
        log_event("heat_session", guid=guid, code=code, medium=medium)
        return code

    def set_all(self, code: str, *, guid: int | None = None) -> dict:
        """The console's button: "clean" or "open" for everyone (or one member), or "clear"."""
        if code == "clear":
            n = self.sessions.clear(guid, everything=guid is None)
        else:
            medium = "game" if self._in_game() else "discord"
            self.sessions.set(guid, code, medium=medium, ack=guid is not None)
            n = 1
        log_event("heat_session", guid=guid or 0, code=code, medium="console")
        return {"ok": True, "ended" if code == "clear" else "set": n,
                "sessions": self.sessions.view()}

    def _in_game(self) -> bool:
        engine = getattr(self.app, "engine", None)
        return getattr(engine, "_session_id", None) is not None

    def ack_line(self, guid: int, name: str) -> str:
        """Once per override: the line that has her acknowledge it in her own words ("" when
        there is none to acknowledge). An open evening reads the owner's heat/session_open.txt
        after it when he wrote one."""
        mode = self.sessions.take_ack(guid)
        if mode is None:
            return ""
        line = ACK[mode].format(name=name)
        if mode == "open":
            from . import overlays
            extra = overlays.one_line("session_open", self.app.data_dir)
            if extra:
                line = f"{line} {extra}"
        return line

    def on_logout(self) -> None:
        n = self.sessions.end_game()
        if n:
            log_event("heat_session_ended", reason="logout", n=n)

    def on_hello(self, online: bool) -> None:
        """The first hello of a mind start with him offline clears every override; a later one
        with him offline ends those said in game (he left while the link was down)."""
        if online:
            self._greeted = True
            return
        first, self._greeted = not getattr(self, "_greeted", False), True
        n = self.sessions.clear(everything=True) if first else self.sessions.end_game()
        if n:
            log_event("heat_session_ended", reason="start" if first else "logout", n=n)


def attach(app):
    """Read `[heat]` and, when it is on, make the app's HeatLive the process's own. With it off
    (or invalid) nothing is attached."""
    global _LIVE
    config = load_heat_config(app.data_dir / "mind.toml")
    if not config.enabled:
        _LIVE = None
        log_event("heat_off")
        return None
    _LIVE = HeatLive(app, config)
    log_event("heat_on", public_cap=config.public_cap)
    return _LIVE


def detach(live=None) -> None:
    """The app is stopping: readers go back to the path without the dial."""
    global _LIVE
    if live is None or _LIVE is live:
        _LIVE = None
