"""The native mechanic responder's feed ([raidplay] respond): each covered raid's actionable mechanics, sent to
mod-leeroy as one `raid_mechanics` command per map and difficulty (README, "The responder").

Actionable means a kind the responder moves bodies for, a place the cast start names (`area`), and roles stock
playerbots leaves: every role of a `none` mechanic, the `respond_roles` of a `partial` one, none of a `playerbots`
one. Nothing here reads mind.db or calls a model; logs carry ids and counts only.
"""
from __future__ import annotations

import asyncio
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.logs import log
from leeroy_mind.protocol import Hello, register_command

from .model import DEFAULT_CONE_DEGREES, DIFFICULTIES, DIFFICULTY_KEYS, PlaybookError, Playbooks, load

# The hello feature of a worldserver that takes raid_mechanics (mod-leeroy's kFeatures).
FEATURE = "mechanics"
# Native's bounds (LeeroyMechanicPolicy.h): rules a table, spells a rule, yards, seconds.
MAX_RULES = 64
MAX_SPELLS = 16
MAX_YARDS = 100.0
MAX_DURATION = 30.0
_CODE = re.compile(r"[A-Za-z0-9_:.,-]{1,64}")


@dataclass(frozen=True, slots=True)
class MechanicRule:
    """One mechanic at one difficulty, as native reads it (raid_mechanics' `mechanics` items)."""
    id: str
    kind: str
    area: str
    roles: tuple[str, ...]
    spells: tuple[int, ...]
    radius: float = 0.0
    range_: float = 0.0
    angle: float = 0.0
    duration: float = 0.0


@dataclass(frozen=True, slots=True)
class RaidMechanics:
    """raid_mechanics: one instance's whole table, replacing what native held for that map and difficulty."""
    id: str
    map: int
    difficulty: int
    mechanics: tuple[MechanicRule, ...]


def _yards(value) -> float:
    return float(min(max(value or 0, 0), MAX_YARDS))


def actionable(playbooks: Playbooks, map_id: int, difficulty: int) -> tuple[MechanicRule, ...]:
    """The mechanics native should respond to in one instance (training difficulty 0-3), in playbook order."""
    raid = playbooks.raids.get(map_id)
    key = DIFFICULTIES.get(difficulty) if type(difficulty) is int else None
    if raid is None or key not in raid.difficulties:
        return ()
    rules = []
    for boss in raid.bosses:
        for m in boss.mechanics:
            roles = m.responder_roles()
            spells = m.responder_spells(key)[:MAX_SPELLS] if m.applies(key) else ()
            if not roles or not spells:
                continue
            angle = m.number("angle", key) or (DEFAULT_CONE_DEGREES if m.area == "cone" else 0)
            # A spread holds until the next cast; anything else lingers as long as its spell (native).
            duration = m.number("duration", key) or (m.number("every", key) if m.kind == "spread" else 0) or 0
            rules.append(MechanicRule(m.id, m.kind, m.area, roles, spells, _yards(m.number("radius", key)),
                                      _yards(m.number("range", key)), float(min(angle, 360)),
                                      float(min(duration, MAX_DURATION))))
    return tuple(rules[:MAX_RULES])


def tables(playbooks: Playbooks) -> dict[tuple[int, int], tuple[MechanicRule, ...]]:
    """Every covered map and difficulty with at least one actionable mechanic."""
    out = {}
    for map_id, raid in playbooks.raids.items():
        for key in raid.difficulties:
            difficulty = DIFFICULTY_KEYS.index(key)
            if rules := actionable(playbooks, map_id, difficulty):
                out[(map_id, difficulty)] = rules
    return out


def settings(path: Path) -> tuple[bool, str]:
    """([raidplay] respond is on, why not). It needs [training] playbooks: the training ground scores the
    playbooks' checks that measure the responder. A missing table means off; an invalid one, off."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return False, "off"
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False, "invalid"
    table = data.get("raidplay", {})
    if not isinstance(table, dict) or set(table) - {"respond"} or type(table.get("respond", False)) is not bool:
        return False, "invalid"
    if not table.get("respond", False):
        return False, "off"
    from leeroy_mind.training.config import ConfigError, load_training_config
    try:
        playbooks = load_training_config(path).playbooks
    except (ConfigError, ValueError, TypeError):
        return False, "needs_playbooks"
    return (True, "") if playbooks else (False, "needs_playbooks")


def _code(value) -> str:
    text = str(value or "")[:64]
    return text if _CODE.fullmatch(text) else "?"


class MechanicFeed:
    """Sends every table at each hello from a worldserver that takes them, and an instance's table again when
    his group enters it (native keeps them for the connection that sent them)."""

    def __init__(self, app, playbooks: Playbooks):
        self.app = app
        self.tables = tables(playbooks)
        self.connected = False
        self.supported = False
        self.pending: list[tuple[int, int]] = []
        self.entered: set[tuple[int, int]] = set()  # (map, instance id) re-sent on this connection
        self.wake = asyncio.Event()

    def queue(self, key: tuple[int, int]) -> None:
        if key in self.tables and key not in self.pending:
            self.pending.append(key)
            self.wake.set()

    async def connection(self, connected: bool) -> None:
        self.connected = connected
        if not connected:
            self.supported = False
            self.pending.clear()
            self.entered.clear()

    async def event(self, ev) -> None:
        if isinstance(ev, Hello):
            self.supported = FEATURE in ev.features
            self.entered.clear()
            if not self.supported:
                log("raidplay_respond_unsupported")
                return
            for key in sorted(self.tables):
                self.queue(key)
            return
        if self.app.is_backlog(ev) or getattr(ev, "reason", None) != "entered":
            return
        key = (getattr(ev, "map", 0), getattr(ev, "difficulty", -1))
        where = (key[0], getattr(ev, "instance_id", 0))
        if self.supported and where[1] and key in self.tables and where not in self.entered:
            self.entered.add(where)
            self.queue(key)

    async def send(self, key: tuple[int, int]) -> bool:
        if not (self.connected and self.supported):
            return False
        rules = self.tables[key]
        try:
            ack = await self.app.bridge.send(RaidMechanics("", key[0], key[1], rules))
        except Exception as exc:  # the bridge dropped: the next hello sends everything again
            log("raidplay_respond_transport", map=key[0], difficulty=key[1], error=type(exc).__name__)
            return False
        if ack.ok:
            log("raidplay_respond_sent", map=key[0], difficulty=key[1], mechanics=len(rules))
        else:
            log("raidplay_respond_refused", map=key[0], difficulty=key[1], error=_code(ack.error))
        return ack.ok

    async def run(self) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.pending:
                await self.send(self.pending.pop(0))
                await asyncio.sleep(0)


def attach(app):
    """Off (the default), invalid or without [training] playbooks: nothing is attached and native gets no
    table, so its responder moves nobody."""
    on, reason = settings(app.data_dir / "mind.toml")
    if not on:
        if reason != "off":
            log("raidplay_respond_off", reason=reason)
        return None
    try:
        playbooks = load()
    except PlaybookError:
        log("raidplay_respond_off", reason="playbook_error")
        return None
    from leeroy_mind.guild.protocol import register as register_guild
    register_command("raid_mechanics", RaidMechanics)
    register_guild()
    feed = MechanicFeed(app, playbooks)
    app.register_event_handler("hello", feed.event)
    app.register_event_handler("guild_group", feed.event)
    app.register_connection_hook(feed.connection)
    app.register_background(feed.run)
    log("raidplay_respond_on", tables=len(feed.tables), mechanics=sum(len(r) for r in feed.tables.values()))
    return feed
