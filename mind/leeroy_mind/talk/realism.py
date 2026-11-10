"""Optional bounded typing cadence. Pure plans; no model, probe or stored chat reads."""
from collections import OrderedDict
from dataclasses import dataclass
import random
import re

from leeroy_mind.discord.channels import is_discord, is_private
from leeroy_mind.fingerprint import fingerprint
from leeroy_mind.protocol import Boss, Hello, Logout, Zone
from leeroy_mind.talk.raid import MAX_FIGHT_S, RaidPhase
from leeroy_mind.talk.style import Styled
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.wow import DUNGEONS, RAID_INSTANCES

CALLS = frozenset({TurnKind.CALLOUT, TurnKind.URGENT})
BLOCK_BURSTS = frozenset({RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF})
CORRECTION_RATE = .02
BURST_RATE = .25
BURST_GAP = 90.0
MAX_MEMBERS = 1024
_CLAUSE = re.compile(r"(?<=[.!?;])\s+|,\s+(?=(?:but|then|and|so)\b)")
_WORD = re.compile(r"(?<![\w@])([a-z]{4,})(?!\w)")
# Reviewed nonwords only, each a single adjacent-key substitution. No dynamic
# dictionary guesses, names, slurs, transposed punctuation or item-link changes.
SAFE_TYPOS = {"flask": "flqsk", "water": "wster", "arrows": "arrpws",
              "ready": "reqdy", "tonight": "tonoght", "thanks": "thqnks"}
ADJACENT = {"a": frozenset("qs"), "o": frozenset("ip"), "i": frozenset("uo")}


@dataclass
class MemberPace:
    credit: float = 0
    burst_at: float = float("-inf")
    zone_at: float = float("-inf")


@dataclass(frozen=True)
class Plan:
    lines: tuple[Styled, ...]
    delays: tuple[float, ...]
    baseline: float
    extra: float
    correction: str = ""
    correction_delay: float = 0
    burst: bool = False


def words_per_minute(persona, profile=None):
    """Authored cadence prior, not measured keystroke speed or demographic inference."""
    base = 12 * persona.typing.chars_per_sec
    base += random.Random(f"typing-v1:{persona.guid}").uniform(-6, 6)
    base -= (persona.identity.age - 35) * .12
    if profile is not None and profile.available and not profile.seeded:
        base += max(-5, min(5, (profile.p50 - 12) * .3))
        base += 2 * profile.lowercase - 4 * min(1, profile.typo * 20)
    return max(25, min(100, base))


def duration(text, wpm, rng, *, first, busy=False):
    value = len(text) * 12 / wpm + rng.uniform(.4, 1.8)
    if first and len(text.split()) >= 18:
        value += rng.uniform(1.5, 4.5)
    if first and busy:
        value += rng.uniform(2, 4)
    return max(.5, value)


def split_line(line):
    typed, plain = _CLAUSE.split(line.line), _CLAUSE.split(line.plain)
    if len(typed) != len(plain) or len(typed) < 2:
        return [line]
    # Preserve every word and its styled counterpart; only natural clause breaks.
    if len(typed) > 3:
        typed, plain = [*typed[:2], " ".join(typed[2:])], [*plain[:2], " ".join(plain[2:])]
    if any(not a.strip() or not b.strip() for a, b in zip(typed, plain)):
        return [line]
    return [Styled(a.strip(), b.strip()) for a, b in zip(typed, plain)]


def typo(line, protected, rng):
    if any(mark in line.line for mark in ("[", "]", "{", "}", "|", "@", "http")):
        return None
    protected = {part.casefold() for s in protected for part in s.split()}
    choices = [m for m in _WORD.finditer(line.line) if m[0] in SAFE_TYPOS
               and m[0] not in protected and SAFE_TYPOS[m[0]] not in protected]
    if not choices:
        return None
    match = rng.choice(choices)
    text = line.line[:match.start()] + SAFE_TYPOS[match[0]] + line.line[match.end():]
    return Styled(text, line.plain), "*" + match[0]


class Realism:
    def __init__(self):
        self.members = OrderedDict()
        self.fights = OrderedDict()

    def member(self, guid):
        if guid not in self.members:
            self.members[guid] = MemberPace()
        self.members.move_to_end(guid)
        while len(self.members) > MAX_MEMBERS:
            self.members.popitem(last=False)
        return self.members[guid]

    def record_line(self, guid):
        state = self.member(guid)
        state.credit = min(1, state.credit + CORRECTION_RATE)

    def observe(self, event, now):
        if isinstance(event, Hello):
            self.fights.clear()
            for state in self.members.values():
                state.zone_at = float("-inf")
        elif isinstance(event, Logout):
            self.member(event.guid).zone_at = float("-inf")
        elif isinstance(event, Zone):
            at = event.ts / 1000 if event.ts > 0 else now
            if 0 <= now - at < 20:
                self.member(event.guid).zone_at = now
        elif isinstance(event, Boss):
            key = (event.map, event.instance_id, event.boss_id)
            if event.state == "engaged":
                self.fights[key] = (event.ts / 1000 if event.ts > 0 else now, frozenset(event.members))
                while len(self.fights) > 128:
                    self.fights.popitem(last=False)
            elif event.state in {"killed", "wiped"}:
                self.fights.pop(key, None)

    def combat(self, engine, turn):
        if is_discord(turn.channel):
            return False
        now = engine.clock.now()
        member = engine.world.members.get(turn.speaker)
        return any(turn.speaker in members and member and member.map == key[0]
                   and 0 <= now - at < MAX_FIGHT_S for key, (at, members) in self.fights.items())

    def busy(self, engine, turn):
        if is_discord(turn.channel):
            return False
        member = engine.world.members.get(turn.speaker)
        return bool(member and (member.map in DUNGEONS or member.map in RAID_INSTANCES)
                    or self.combat(engine, turn)
                    or 0 <= engine.clock.now() - self.member(turn.speaker).zone_at < 20)

    def safe(self, engine, turn):
        return is_discord(turn.channel) or (not self.combat(engine, turn)
            and engine.raid.phase(engine.clock.now()) not in BLOCK_BURSTS)

    def eligible(self, engine, turn, meta):
        return (engine.cfg.talk.realism and turn.kind not in CALLS and turn.template is None
                and meta.before_transport is None and not meta.elsewhere and meta.why != "hotmic")

    def plan(self, engine, turn, meta, persona, lines, baseline):
        if not self.eligible(engine, turn, meta):
            return None
        rng, now = engine.rng, engine.clock.now()
        state = self.member(turn.speaker)
        cap = 12.0 if is_private(turn.channel) else 30.0
        busy = turn.channel == "whisper" and turn.to_guid == engine.human.guid and self.busy(engine, turn)
        safe = self.safe(engine, turn)
        out, burst = list(lines), False
        if (busy or not safe) and len(out) > 1:
            out = out[:1]  # keep the complete first styled line; no invented busy excuse
        elif safe and len(out) == 1 and len(out[0].plain.split()) >= 18 \
                and now-state.burst_at >= BURST_GAP and rng.random() < BURST_RATE:
            out = split_line(out[0])
            burst = len(out) > 1
            if burst:
                state.burst_at = now
        profile = fingerprint(engine.store.conn, turn.speaker)
        wpm = words_per_minute(persona, profile)
        desired = [duration(s.line, wpm, rng, first=i == 0, busy=busy) +
                   (rng.uniform(.8, 2) if i else 0) for i, s in enumerate(out)]
        correction, correction_delay = "", 0.0
        if safe and not busy and len(out) < 3 and state.credit >= 1 and rng.random() < CORRECTION_RATE:
            chosen = typo(out[-1], engine._protect(turn.channel), rng)
            if chosen is not None:
                out[-1], correction = chosen
                correction_delay = rng.uniform(5, 10)
                # Reserve before any await; concurrent plans cannot spend it twice.
                state.credit -= 1
        baseline_total = sum(baseline)
        # The whole turn, including gaps and a possible correction, shares one cap.
        ceiling = baseline_total + cap - correction_delay
        total = sum(desired)
        if total > ceiling:
            desired = [d * ceiling / total for d in desired]
        extra = max(0, sum(desired) + correction_delay - baseline_total)
        return Plan(tuple(out), tuple(desired), baseline_total, extra, correction, correction_delay, burst)
