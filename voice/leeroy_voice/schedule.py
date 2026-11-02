"""The playback scheduler (draft section 4.3): which member's line plays when, at what gain, and
what the mind is told. It is pure: no device, no thread, no timer, and not thread-safe: the service
calls it from its event loop only (a synthesis thread hands ready() over to the loop), with each
event, at the clock's time unless it passes `now`, ticks it every few tens of milliseconds, and
applies each Plan it returns. Every line ends exactly once, reported in `played`: dropped, or
started and then done or cut.

- Only lines he can hear: his room's, and voice whispers to him in any room (else dropped
  "not_his_channel"); none while his session is offline ("offline").
- One queue per member, so nobody overlaps themself. A member's lines go most urgent tier first,
  first come first served within a tier, and an `after` dependency beats tier order (R2): a line
  waiting on its `after` lets through only that line's chain (what must start before it can:
  the line, what that one waits on, and what is ahead of it in its member's order), so it never
  blocks the line it answers, whatever their tiers or members. The member's other lines keep
  their place behind it: a later line of its tier never overtakes it. A line not yet
  synthesized keeps its place too.
- At most max_voices members are audible at once. When a voice ends, a different speaker starts
  no sooner than GAP_S later (drawn each time), and nobody less urgent takes the slot meanwhile;
  the same speaker goes on at once.
- p0 (the raid lead's call) always gets a slot: with every slot busy, a silent voice (one
  waiting for its next segment) is cut first, else the least urgent (the latest started of
  those), with CUT_FADE_MS. p1 (anyone's urgent) takes a slot the same way, from a p3 or p4 voice
  only (settled item 10). Either one cuts its own member's line of such a tier, so an "adds"
  never dies behind its member's banter; neither waits for the gap.
- Ducks: every voice PTT_DUCK_DB while he holds the key; every p2-p4 voice URGENT_DUCK_DB while a
  p1 plays, as tier ducking never reaches a higher tier (R3: never a p0 or another p1); every
  voice in his channel WHISPER_DUCK_DB while a whisper to him plays, whatever its tier (the
  draft; never another whisper). A voice takes the deepest that applies, not the sum. A p0 voice
  plays P0_GAIN_DB above the rest.
- Push-to-talk: while the key is down, the model's p2-p4 lines wait with their expiry running;
  p0 and p1 lines, and every template line (speak.template, R1) at any tier, start at once,
  ducked under him (N2, settled item 13). After key-up the waiting lines start PTT_GRACE_S later.
  A member's held line steps aside, so a template line of theirs behind it still goes. A line in
  progress is ducked, never cut. set_ptt(True) comes only with a Press, after the 150 ms gate
  (I2), so a tap ducks nothing.
- Raid silence, mirrored as a second guard: in a phase of RAID_PASS only its tiers start, plus
  DEBRIEF_KINDS in the debrief; trash, kill and none pass everything (I1). The tiers already
  carry the lead's calls, so the rule needs no lead. A phase change drops the waiting lines it
  silences ("raid"); one already playing plays on (the mind's Cancel(below="p3") at a pull stops
  banter in progress).
- Expiry: a line that has not started by its expires_at is dropped "expired"; once started it
  plays to its end.
- `after` (I7): a line naming another waits until that one is done, letting its chain through
  meanwhile, and is dropped "after_dropped" when it was dropped or cut, or is an id never seen
  or ended more than AFTER_MEMORY_S ago.
- Segments: a line starts with its first segment once that one is synthesized; the later ones
  follow in order as appends. If its audio runs out before the next one is ready, the line keeps
  its slot and resumes, as a start, when it is, or is cut "engine_error" after SEGMENT_WAIT_S, so
  no line waits forever.
- set_human_room drops his old room's waiting lines and fades its playing ones over MOVE_FADE_MS
  ("moved"); whispers follow him. cancel drops or cuts ("cancelled", or the reason it is given,
  CUT_FADE_MS). Going offline drops what waits and fades what plays over MOVE_FADE_MS ("offline"),
  and ends the press.
- The service reports two things that come from outside: drop_waiting(reason) when the mind has
  gone (what waits is dropped, what plays plays on), and engine_failed(id) when a line's synthesis
  failed. A waiting line is dropped "engine_error"; a started one plays what it has and is cut
  "engine_error" when that runs out, with no SEGMENT_WAIT_S."""
import bisect
import itertools
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .wire import DROP_REASONS, PHASES, TIERS, Played

GAP_S = (0.15, 0.40)  # silence before a different speaker, drawn each time a voice ends
PTT_GRACE_S = 0.3
PTT_DUCK_DB = -12.0
URGENT_DUCK_DB = -10.0
WHISPER_DUCK_DB = -6.0
P0_GAIN_DB = 2.0
CUT_FADE_MS = 40
MOVE_FADE_MS = 150
AFTER_MEMORY_S = 120.0  # how long finished and dropped ids are remembered for `after` (I7)
SEGMENT_WAIT_S = 5.0  # how long a line whose audio ran out waits for its next segment
RAID_PASS: dict[str, frozenset[int]] = {
    "combat": frozenset({0, 1, 2}),
    "wipe_silence": frozenset({1}),
    "wipe_debrief": frozenset({0, 1, 2}),
}
DEBRIEF_KINDS = frozenset({"wipe_react"})  # the reactions anyone may voice in the debrief
WHISPER = "whisper"  # a voice whisper's channel


@dataclass(slots=True)
class Line:
    """One `speak` as the scheduler holds it. `tier` is the priority's number (p0 is 0);
    `whisper` marks a voice whisper to him, whose channel is "whisper"; `expires_at` is in the
    clock's seconds; `segments` are 48 kHz mono clips, sentence by sentence, None until
    synthesized (ready() fills them in); `template` is speak.template, a turn composed without
    the model."""
    id: str
    guid: int
    tier: int
    kind: str
    channel: str
    whisper: bool
    expires_at: float
    after: str  # "": none
    segments: list[np.ndarray | None]
    template: bool = False


@dataclass(slots=True)
class Plan:
    """What one call changed. The service applies it in this order: the stops (id, fade ms,
    reason), the starts (id, audio, gain dB), the appends (id, audio), then the ducks in
    `gains`; `played` goes to the mind.

    A start's gain is the line's tier offset (P0_GAIN_DB for p0) plus its duck; `gains` holds
    the new duck alone (dB, 0 = none) of each voice already sounding whose duck changed. So a
    voice's level is the service's own gain for it, plus its tier offset, plus its latest duck.
    A start also resumes a line whose audio ran out before its next segment was ready: the same
    id, no second "started". An append must play even when it reaches the mixer just after the
    voice's audio ran out (the mixer revives the voice and holds back its end), since
    finished() tells the scheduler that the mixer played everything handed to it."""
    starts: list[tuple[str, np.ndarray, float]] = field(default_factory=list)
    appends: list[tuple[str, np.ndarray]] = field(default_factory=list)
    stops: list[tuple[str, int, str]] = field(default_factory=list)
    gains: dict[str, float] = field(default_factory=dict)
    played: list[Played] = field(default_factory=list)


@dataclass(slots=True, eq=False)
class _Entry:
    line: Line
    seq: int  # arrival order; a member's queue goes by (tier, seq)
    waits_for: str = ""  # the line in `after`, until that one is done
    order: int = -1  # when it started, among starts; -1 while it waits
    handed: int = 0  # how many of its segments the mixer has had
    stalled_at: float | None = None  # when its audio ran out before its next segment was ready
    failed: bool = False  # its next segment will never come


def _takes(tier: int, other: int) -> bool:
    """Whether a line of `tier` takes the slot of a voice of tier `other`."""
    return (tier == 0 and other > 0) or (tier == 1 and other >= 3)


def _tier_gain(tier: int) -> float:
    return P0_GAIN_DB if tier == 0 else 0.0


class PlaybackScheduler:
    def __init__(self, clock, rng, *, max_voices: int = 2):
        """`clock` gives the time of a call made without `now`; `rng` draws the gaps."""
        if max_voices < 1:
            raise ValueError("max_voices must be 1 or more")
        self._clock = clock
        self._rng = rng
        self._max = max_voices
        self._online = False
        self._room = ""  # his room; "": off comms
        self._phase = "none"
        self._lead = 0  # kept as the mind reports it; the pass rule needs only the tiers
        self._ptt = False
        self._ptt_up: float | None = None  # when the key last came up
        self._end: tuple[float, int, float] | None = None  # (when, guid, gap) of the last end
        self._queues: dict[int, list[_Entry]] = {}  # each member's waiting lines, in order
        self._playing: dict[str, _Entry] = {}  # started lines, in start order
        self._active: dict[str, _Entry] = {}  # every line waiting or playing
        self._ended: dict[str, tuple[str, float]] = {}  # id -> (last state, when), for `after`
        self._ducks: dict[str, float] = {}  # the duck each sounding voice was last given
        self._seq = itertools.count()

    # the calls

    def submit(self, line: Line, now: float | None = None) -> Plan:
        self._validate(line)
        plan, now = self._open(now)
        reason = self._refusal(line, now)
        if reason:
            self._ended[line.id] = ("dropped", now)
            plan.played.append(Played(line.id, "dropped", reason))
        else:
            entry = _Entry(line, next(self._seq),
                           waits_for=line.after if line.after in self._active else "")
            self._active[line.id] = entry
            bisect.insort(self._queues.setdefault(line.guid, []), entry,
                          key=lambda e: (e.line.tier, e.seq))
        return self._settle(plan, now)

    def ready(self, line_id: str, index: int, audio: np.ndarray) -> None:
        """Segment `index` of a line is synthesized; it goes out with the next call's Plan. A
        line that has ended meanwhile is ignored."""
        entry = self._active.get(line_id)
        if entry is None:
            return
        segments = entry.line.segments
        if not 0 <= index < len(segments):
            raise ValueError(f"line {line_id} has no segment {index}")
        if segments[index] is not None:
            raise ValueError(f"segment {index} of line {line_id} is already ready")
        segments[index] = audio

    def finished(self, line_id: str, now: float | None = None) -> Plan:
        """The mixer played everything it was handed for this line. A voice it stopped, or one
        it never had, is ignored.

        Task 7's contract: the mixer reports a voice's end only once every segment the Plans
        handed it has played, appends included. A stale end, posted before an append reached
        the mixer, is taken as current: the line counts as done while that append still plays,
        and its member's next line starts over it, so the member overlaps themself. The
        scheduler has no guard for it."""
        plan, now = self._open(now)
        entry = self._playing.get(line_id)
        if entry is not None and entry.stalled_at is None:
            if entry.handed < len(entry.line.segments):
                entry.stalled_at = now  # it keeps its slot until its next segment is ready
            else:
                self._done(plan, entry, now)
        return self._settle(plan, now)

    def cancel(self, *, id: str = "", guid: int = 0, below: int = -1,
               now: float | None = None, reason: str = "cancelled") -> Plan:
        """Drop or cut one line, a member's lines, or tier `below` and every less urgent one."""
        if (id != "") + (guid != 0) + (below != -1) != 1:
            raise ValueError("cancel takes exactly one of id, guid and below")
        if below != -1 and not 0 <= below < len(TIERS):
            raise ValueError(f"no tier {below}")
        plan, now = self._open(now)

        def hit(line: Line) -> bool:
            return line.id == id if id else line.guid == guid if guid else line.tier >= below
        self._clear(plan, hit, reason, CUT_FADE_MS, now)
        return self._settle(plan, now)

    def drop_waiting(self, reason: str, now: float | None = None) -> Plan:
        """Drop every waiting line with `reason`; what plays plays on."""
        if reason not in DROP_REASONS:
            raise ValueError(f"no drop reason {reason!r}")
        plan, now = self._open(now)
        self._clear(plan, lambda line: True, reason, CUT_FADE_MS, now, playing=False)
        return self._settle(plan, now)

    def engine_failed(self, line_id: str, now: float | None = None) -> Plan:
        """A segment of this line will never come. A line that has ended is ignored."""
        plan, now = self._open(now)
        entry = self._active.get(line_id)
        if entry is not None:
            if entry.order < 0:
                self._drop(plan, entry, "engine_error", now)
            else:
                entry.failed = True
        return self._settle(plan, now)

    def set_ptt(self, down: bool, now: float | None = None) -> Plan:
        plan, now = self._open(now)
        if down != self._ptt:
            self._ptt = down
            if not down:
                self._ptt_up = now
        return self._settle(plan, now)

    def set_raid(self, phase: str, lead: int, now: float | None = None) -> Plan:
        if phase not in PHASES:
            raise ValueError(f"unknown raid phase {phase!r}")
        plan, now = self._open(now)
        self._phase, self._lead = phase, lead
        self._clear(plan, lambda line: not self._passes(line), "raid", CUT_FADE_MS, now,
                    playing=False)
        return self._settle(plan, now)

    def set_human_room(self, room: str, now: float | None = None) -> Plan:
        plan, now = self._open(now)
        if room != self._room:
            self._room = room
            self._clear(plan, lambda line: not line.whisper and line.channel != room, "moved",
                        MOVE_FADE_MS, now)
        return self._settle(plan, now)

    def set_session(self, online: bool, now: float | None = None) -> Plan:
        plan, now = self._open(now)
        if online != self._online:
            self._online = online
            if not online:
                self._ptt, self._ptt_up = False, None
                self._clear(plan, lambda line: True, "offline", MOVE_FADE_MS, now)
        return self._settle(plan, now)

    def tick(self, now: float | None = None) -> Plan:
        plan, now = self._open(now)
        return self._settle(plan, now)

    def knows(self, line_id: str) -> bool:
        """Whether the id is in use: a line waiting or playing, or one that ended within
        AFTER_MEMORY_S. submit() refuses such an id."""
        return line_id in self._active or line_id in self._ended

    def waiting(self) -> int:
        return sum(len(q) for q in self._queues.values())

    def playing(self) -> set[str]:
        """The started lines, a line waiting for its next segment included (it keeps its slot)."""
        return set(self._playing)

    # the rules

    def _validate(self, line: Line) -> None:
        if not line.id:
            raise ValueError("a line needs an id")
        if line.id in self._active or line.id in self._ended:
            raise ValueError(f"line id {line.id} is already in use")
        if not 0 <= line.tier < len(TIERS):
            raise ValueError(f"no tier {line.tier}")
        if not line.segments:
            raise ValueError("a line needs a segment")
        if line.whisper != (line.channel == WHISPER):
            raise ValueError("a whisper's channel is 'whisper', and only a whisper's")

    def _refusal(self, line: Line, now: float) -> str:
        if not self._online:
            return "offline"
        if not line.whisper and (not self._room or line.channel != self._room):
            return "not_his_channel"
        if not self._passes(line):
            return "raid"
        if now >= line.expires_at:
            return "expired"
        if line.after and line.after not in self._active and (
                self._ended.get(line.after, ("", 0.0))[0] != "done"):
            return "after_dropped"
        return ""

    def _passes(self, line: Line) -> bool:
        allowed = RAID_PASS.get(self._phase)
        if allowed is None or line.tier in allowed:
            return True
        return self._phase == "wipe_debrief" and line.kind in DEBRIEF_KINDS

    def _held(self, line: Line, now: float) -> bool:
        """A p2-p4 line from the model waits while he talks, and PTT_GRACE_S after; a template
        line never does (R1)."""
        if line.tier <= 1 or line.template:
            return False
        return self._ptt or (self._ptt_up is not None and now < self._ptt_up + PTT_GRACE_S)

    def _candidate(self, queue: list[_Entry], now: float) -> _Entry | None:
        """The member's line to try next: the first in their order that is free to go and that
        every waiting line ahead of it lets through. A line waiting on its `after` lets through
        only its `after` line's chain (R2), so the member's other lines keep their order behind
        it; a line held by the key lets through a template line (R1)."""
        waiting: list[_Entry] = []
        for entry in queue:
            passes = all(self._lets_through(w, entry, now) for w in waiting)
            free = not entry.waits_for and not self._held(entry.line, now)
            if passes and free:
                return entry
            if free:
                return None  # held up behind a waiting line, and so is every line behind it
            waiting.append(entry)
        return None

    def _lets_through(self, waiting: _Entry, entry: _Entry, now: float) -> bool:
        if waiting.waits_for:
            return entry.line.id in self._chain(waiting)
        return entry.line.template  # held by the key, which never holds a template line

    def _chain(self, entry: _Entry) -> set[str]:
        """The lines that must start before a waiting line can: its `after` line and, in turn,
        what that one waits on and what is ahead of it in its member's order."""
        chain: set[str] = set()
        todo = [entry.waits_for]
        while todo:
            other = self._active.get(todo.pop())
            if other is None or other.line.id in chain:
                continue
            chain.add(other.line.id)
            if other.waits_for:
                todo.append(other.waits_for)
            if other.order < 0:  # waiting in its member's queue, behind the lines ahead of it
                queue = self._queues[other.line.guid]
                todo.extend(e.line.id for e in queue[:queue.index(other)])
        return chain

    def _gap(self, line: Line, now: float) -> bool:
        """A p2-p4 line from another speaker waits for the gap after the last voice ended."""
        if line.tier <= 1 or self._end is None:
            return False
        when, guid, gap = self._end
        return guid != line.guid and now < when + gap

    def _duck(self, entry: _Entry, sounding: list[_Entry]) -> float:
        """The deepest duck on a voice. Tier ducking never reaches a higher tier (R3); a
        whisper's duck takes his whole channel, whatever plays there (the draft)."""
        duck = PTT_DUCK_DB if self._ptt else 0.0
        for other in sounding:
            if other is entry:
                continue
            if other.line.tier == 1 and entry.line.tier >= 2:
                duck = min(duck, URGENT_DUCK_DB)
            if other.line.whisper and not entry.line.whisper:
                duck = min(duck, WHISPER_DUCK_DB)
        return duck

    # the bookkeeping

    def _open(self, now: float | None) -> tuple[Plan, float]:
        if now is None:
            now = self._clock.now()
        while self._ended:  # oldest first: they are recorded in time order
            line_id, (_, when) = next(iter(self._ended.items()))
            if now - when <= AFTER_MEMORY_S:
                break
            del self._ended[line_id]
        plan = Plan()
        for entry in self._waiting_where(lambda line: now >= line.expires_at):
            if self._active.get(entry.line.id) is entry:
                self._drop(plan, entry, "expired", now)
        return plan, now

    def _settle(self, plan: Plan, now: float) -> Plan:
        for entry in self._playing.values():
            segments = entry.line.segments
            if entry.stalled_at is not None and segments[entry.handed] is not None:
                entry.stalled_at = None  # it resumes with its next segment
                plan.starts.append((entry.line.id, segments[entry.handed], 0.0))
                entry.handed += 1
            if entry.stalled_at is None:
                self._hand(plan, entry)
        for entry in [e for e in self._playing.values() if e.stalled_at is not None]:
            if entry.failed or now - entry.stalled_at >= SEGMENT_WAIT_S:
                self._cut(plan, entry, "engine_error", CUT_FADE_MS, now)
        self._pump(plan, now)
        self._finish(plan)
        return plan

    def _pump(self, plan: Plan, now: float) -> None:
        """Start what may start, most urgent first (offline, nothing waits)."""
        free = self._max - len(self._playing)
        heads = [c for q in self._queues.values() if (c := self._candidate(q, now)) is not None]
        heads.sort(key=lambda e: (e.line.tier, e.seq))
        for entry in heads:  # none waits on an `after`, so no cut below can drop one of them
            line = entry.line
            if line.segments[0] is None:
                continue  # not synthesized yet: its member's later lines wait behind it
            own = next((e for e in self._playing.values() if e.line.guid == line.guid), None)
            if own is not None:
                if _takes(line.tier, own.line.tier):
                    self._cut(plan, own, "", CUT_FADE_MS, now, gap=False)
                    self._start(plan, entry)
                continue
            if self._gap(line, now):
                free -= 1  # its slot is kept for it until the gap has passed
            elif free > 0:
                self._start(plan, entry)
                free -= 1
            elif (victim := self._victim(line.tier)) is not None:
                self._cut(plan, victim, "", CUT_FADE_MS, now, gap=False)
                self._start(plan, entry)

    def _victim(self, tier: int) -> _Entry | None:
        """The voice a line of `tier` takes the slot of: a silent one (waiting for its next
        segment) first, then the least urgent, then the latest started."""
        voices = [e for e in self._playing.values() if _takes(tier, e.line.tier)]
        return max(voices, key=lambda e: (e.stalled_at is not None, e.line.tier, e.order),
                   default=None)

    def _start(self, plan: Plan, entry: _Entry) -> None:
        line = entry.line
        self._unqueue(entry)
        entry.order = next(self._seq)
        self._playing[line.id] = entry
        plan.played.append(Played(line.id, "started"))
        plan.starts.append((line.id, line.segments[0], 0.0))  # its gain comes in _finish
        entry.handed = 1
        self._hand(plan, entry)

    def _hand(self, plan: Plan, entry: _Entry) -> None:
        """Append the line's next segments that are ready, in order."""
        segments = entry.line.segments
        while entry.handed < len(segments) and segments[entry.handed] is not None:
            plan.appends.append((entry.line.id, segments[entry.handed]))
            entry.handed += 1

    def _finish(self, plan: Plan) -> None:
        """Set the starts' gains and the ducks that changed."""
        sounding = [e for e in self._playing.values() if e.stalled_at is None]
        ducks = {e.line.id: self._duck(e, sounding) for e in sounding}
        begun = {line_id for line_id, _, _ in plan.starts}
        plan.starts = [(line_id, audio, _tier_gain(self._playing[line_id].line.tier)
                        + ducks[line_id]) for line_id, audio, _ in plan.starts]
        for line_id, duck in ducks.items():
            if line_id not in begun and self._ducks.get(line_id) != duck:
                plan.gains[line_id] = duck
        self._ducks = ducks

    def _clear(self, plan: Plan, hit: Callable[[Line], bool], reason: str, fade_ms: int,
               now: float, *, playing: bool = True) -> None:
        """Cut the playing lines `hit` picks (unless `playing` is False) and drop the waiting
        ones."""
        if playing:
            for entry in [e for e in self._playing.values() if hit(e.line)]:
                if self._active.get(entry.line.id) is entry:
                    self._cut(plan, entry, reason, fade_ms, now)
        for entry in self._waiting_where(hit):
            if self._active.get(entry.line.id) is entry:
                self._drop(plan, entry, reason, now)

    def _waiting_where(self, hit: Callable[[Line], bool]) -> list[_Entry]:
        return sorted((e for q in self._queues.values() for e in q if hit(e.line)),
                      key=lambda e: e.seq)

    def _done(self, plan: Plan, entry: _Entry, now: float) -> None:
        self._close(entry, "done", now)
        plan.played.append(Played(entry.line.id, "done"))
        self._mark_end(entry.line.guid, now)
        for other in self._active.values():
            if other.waits_for == entry.line.id:
                other.waits_for = ""

    def _cut(self, plan: Plan, entry: _Entry, reason: str, fade_ms: int, now: float, *,
             gap: bool = True) -> None:
        """Stop a started line. `gap`: its slot falls silent (no line is taking it over)."""
        line_id = entry.line.id
        resumed = any(s[0] == line_id for s in plan.starts)  # in this Plan: never sounded
        plan.starts = [s for s in plan.starts if s[0] != line_id]
        plan.appends = [a for a in plan.appends if a[0] != line_id]
        if entry.stalled_at is None and not resumed:
            plan.stops.append((line_id, fade_ms, reason))
        self._close(entry, "cut", now)
        plan.played.append(Played(line_id, "cut", reason))
        if gap:
            self._mark_end(entry.line.guid, now)
        self._drop_answers(plan, line_id, now)

    def _drop(self, plan: Plan, entry: _Entry, reason: str, now: float) -> None:
        self._close(entry, "dropped", now)
        plan.played.append(Played(entry.line.id, "dropped", reason))
        self._drop_answers(plan, entry.line.id, now)

    def _drop_answers(self, plan: Plan, line_id: str, now: float) -> None:
        for entry in sorted((e for e in self._active.values() if e.waits_for == line_id),
                            key=lambda e: e.seq):
            if self._active.get(entry.line.id) is entry:
                self._drop(plan, entry, "after_dropped", now)

    def _close(self, entry: _Entry, state: str, now: float) -> None:
        line_id = entry.line.id
        if self._playing.pop(line_id, None) is None:
            self._unqueue(entry)
        del self._active[line_id]
        self._ended[line_id] = (state, now)

    def _unqueue(self, entry: _Entry) -> None:
        queue = self._queues[entry.line.guid]
        queue.remove(entry)
        if not queue:
            del self._queues[entry.line.guid]

    def _mark_end(self, guid: int, now: float) -> None:
        self._end = (now, guid, self._rng.uniform(*GAP_S))
