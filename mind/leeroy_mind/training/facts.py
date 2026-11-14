"""Conservative combat-log additions for the lead's live debrief and kill call.

Current telemetry cannot establish a killer or actual damage taken. CheckResult
causes therefore remain unknown: cached labels, requested damage and arbitrary
future provenance strings cannot activate causal or damage-percentage wording.
Only independently observed elapsed time and deaths support kill records.
"""

import math
from collections.abc import Callable, Sequence
from types import SimpleNamespace

from leeroy_mind.talk.reactions import FACTS_LOGGED, FACTS_RULE, WIPE_KNOWN, WIPE_LOGGED
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.wow import boss_name, is_raid_fight

from . import store


MAX_AGE_S = 120
# Boss and training terminal hooks timestamp the same transition separately.
# Allow subsecond callback/clock skew, never an arbitrary recent previous pull.
EVENT_SLOP_MS = 1000
NO_CAUSE = "Combat log: nothing stands out on this pull."


def _eligible(rec, outcome):
    h = rec.header
    return (rec.complete and h.outcome == outcome and is_raid_fight(h.map, h.boss_id)
            and h.end_ms > h.start_ms >= 0)


def wipe_facts(rec, result, roles, *, book, change="") -> str:
    """No exact cause/share is supported by the current measurement contract.

    Keep the result/roles/book interface for the checked-facts consumer, without
    treating even a supplied cause string as a new provenance contract. A change
    is a prospective plan from the caller, never an inferred reason for a death.
    """
    if not _eligible(rec, "wiped"):
        return ""
    # An incomplete analysis cannot establish that nothing stood out. Even a
    # complete check cannot turn unknown death causes into a negative finding.
    text = (NO_CAUSE if result is not None and result.complete and not result.failures
            and rec.header.deaths == 0 and not rec.deaths else "")
    change = change.strip().rstrip(".")
    if change:
        text = f"{text} Next pull we change one thing: {change}.".strip()
    return text


def kill_facts(rec, prior_kill_s: Sequence[float], boss: str) -> str:
    if not _eligible(rec, "killed"):
        return ""
    priors = [s for s in prior_kill_s if type(s) in (int, float) and math.isfinite(s) and s > 0]
    if len(priors) < 3:
        return ""
    duration = (rec.header.end_ms - rec.header.start_ms) / 1000
    seconds = int(round(duration))
    took = f"{seconds // 60}:{seconds % 60:02d}"
    if duration < min(priors):
        return f"Record: {took}, our fastest {boss} kill yet."
    if rec.header.deaths == 0 and not rec.deaths:
        return f"Nobody died, {took}."
    return ""


class TalkHook:
    """Read a persisted HIS pull only for its own current raid and boss event.

    Turns without explicit event identity keep their original guards. Reading at
    composition allows the header-last telemetry to arrive during the delay.
    This hook does not install itself or change members' reactions. ``live`` says
    whether his recorded pulls may speak: while the overhead gate still measures,
    his armed pulls are evidence only.
    """

    def __init__(self, ports, *, change_for: Callable[[str], str] = lambda context: "",
                 live: Callable[[], bool] = lambda: True):
        self.ports = ports
        self.change_for = change_for
        self.live = live

    def __call__(self, turn, meta, instruction):
        if turn.kind is TurnKind.CALLOUT and meta.why.startswith("wipe:"):
            outcome = "wiped"
        elif turn.kind is TurnKind.KILL and meta.why.startswith("kill:"):
            outcome = "killed"
        else:
            return instruction
        if not self.live():
            return instruction
        now_ms = int(self.ports.clock.now() * 1000)
        end_ms = meta.fight_end_ms
        if (end_ms <= 0 or not 0 <= now_ms - end_ms <= MAX_AGE_S * 1000
                or meta.fight_instance <= 0 or not is_raid_fight(meta.fight_map, meta.fight_boss)):
            return instruction
        raid = self.ports.raid_at(end_ms)
        current = self.ports.raid_at(now_ms)
        if (raid is None or current is None or raid.offline or current.offline
                or raid.raid_id != current.raid_id or raid.map_id != meta.fight_map):
            return instruction
        difficulty = (1 if raid.size == 25 else 0) + (2 if raid.heroic else 0)
        context = f"{meta.fight_map}:{meta.fight_boss}:{difficulty}"
        rows = self.ports.conn.execute(
            "SELECT * FROM tg_pull WHERE kind='his' AND raid_id=? AND context=? "
            "AND instance_id=? AND outcome=? AND end_ms BETWEEN ? AND ? "
            "ORDER BY end_ms DESC,id DESC LIMIT 2",
            (raid.raid_id, context, meta.fight_instance, outcome,
             max(now_ms - MAX_AGE_S * 1000, end_ms - EVENT_SLOP_MS),
             min(now_ms, end_ms + EVENT_SLOP_MS))).fetchall()
        if len(rows) != 1:  # Ambiguous transitions cannot identify this pull.
            return instruction
        row = rows[0]
        started = self.ports.raid_at(row["start_ms"])
        if (not row["complete"] or not row["scoreable"] or started is None
                or started.raid_id != raid.raid_id or row["size"] != raid.size
                or row["map"] != meta.fight_map or row["boss_id"] != meta.fight_boss
                or row["difficulty"] != difficulty):
            return instruction
        members = store.member_rows(self.ports.conn, row["id"])
        deaths = store.death_rows(self.ports.conn, row["id"])
        if (len(members) != row["members"] or len(deaths) != row["deaths"]
                or turn.speaker not in {m["guid"] for m in members}):
            return instruction
        # A persisted header view suffices for these independently observed facts;
        # do not rehydrate tracks or rerun checks during a live composition.
        rec = SimpleNamespace(header=SimpleNamespace(**dict(row)), complete=True, deaths=deaths)
        if outcome == "wiped":
            failures = self.ports.conn.execute(
                "SELECT kind FROM tg_failure WHERE pull=? AND count>0", (row["id"],)).fetchall()
            result = SimpleNamespace(
                complete="checks_complete" in row.keys() and bool(row["checks_complete"]),
                failures=failures)
            fact = wipe_facts(rec, result, {}, book=None, change=self.change_for(context))
        else:
            priors = self.ports.conn.execute(
                "SELECT (end_ms-start_ms)/1000.0 FROM tg_pull WHERE context=? AND kind='his' "
                "AND outcome='killed' AND complete=1 AND scoreable=1 "
                "AND end_ms<=? AND end_ms>start_ms AND id<>?",
                (context, row["start_ms"], row["id"])).fetchall()
            fact = kill_facts(rec, [p[0] for p in priors], boss_name(row["map"], row["boss_id"], ""))
        if not fact:
            return instruction
        if outcome == "wiped" and fact.startswith("Combat log:"):
            instruction = instruction.replace(WIPE_KNOWN, WIPE_LOGGED).replace(FACTS_RULE, FACTS_LOGGED)
        return f"{instruction} {fact}".strip()
