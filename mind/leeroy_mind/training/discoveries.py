"""Bounded normal social proposals from verified provisional dummy results.

The service owns the ticker and supplies current session/raid safety facts.
This module neither calls a model nor claims that a queued proposal was spoken.
Actual DPS and field performance remain unmeasured by the current lab counters.
"""

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math

from leeroy_mind.guild.roles import _SPECS
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.wow import CLASSES

from .lab import _DAMAGE_SPECS, halving_rounds, paired_score
from .lab_runner import (ATTEMPT_PREFIX as LAB_ATTEMPT_PREFIX, DAMAGE_BASIS,
                         OWNED_KEY, SPELL_BASIS, SUMMARY_PREFIX)


ATTEMPT_PREFIX = "tg:lab:discovery:"
FRESH_SECONDS = 15
MAX_RESULT_AGE = 7 * 86400
MAX_JSON_BYTES = 65536
MAX_ATTEMPTS = 128


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _positive(value):
    return type(value) is int and 0 < value <= 0xffffffff


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bounded_json(raw):
    if type(raw) is not str or len(raw) > MAX_JSON_BYTES:
        raise ValueError("oversized or missing lab evidence")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite lab evidence")
        return number
    def unique_pairs(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate lab evidence keys")
        return result
    value = json.loads(raw, parse_float=finite_float, object_pairs_hook=unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 8192 or depth > 12:
            raise ValueError("oversized lab evidence tree")
        if isinstance(item, (list, dict)):
            if len(item) > MAX_ATTEMPTS:
                raise ValueError("oversized lab evidence collection")
            pending.extend((v, depth + 1) for v in (item.values() if isinstance(item, dict) else item))
        elif isinstance(item, str) and len(item) > 2048:
            raise ValueError("oversized lab evidence string")
    return value


def _read(conn, key):
    row = conn.execute("SELECT CASE WHEN length(value)<=? THEN value END FROM meta WHERE key=?",
                       (MAX_JSON_BYTES, key)).fetchone()
    if row is None:
        return None
    return _bounded_json(row[0])


def _integer(value, lo=0, hi=0x7fffffffffffffff):
    return type(value) is int and lo <= value <= hi


def _score(pairs):
    return paired_score([p[0] for p in pairs], [p[1] for p in pairs])


@dataclass(frozen=True, slots=True)
class DiscoverySchedule:
    """Actual app.session_id and current owner/scheduler proof, never defaults.

    raid_active includes retained/ending raids; acquiring includes any opening
    guild/lab/benchmark work. Missing facts or adapter fail closed.
    """
    observed_at: float
    connected: bool
    presence_known: bool
    human_online: bool
    session_id: int
    raid_active: bool
    lab_active: bool
    benchmark_active: bool
    acquiring: bool


class _Reserved(Exception):
    pass


class Discoveries:
    def __init__(self, ports, cfg, *, schedule=None):
        self.ports, self.cfg, self.schedule = ports, cfg, schedule
        self.reason = ""

    def _safe(self):
        now = self.ports.clock.now()
        try:
            proof = self.schedule() if self.schedule is not None else None
            if (not self.cfg.enabled or not self.cfg.lab or not _finite(now)
                    or self.ports.human_online() is not True or not isinstance(proof, DiscoverySchedule)
                    or not _finite(proof.observed_at) or not 0 <= now - proof.observed_at <= FRESH_SECONDS
                    or any(getattr(proof, k) is not True for k in ("connected", "presence_known", "human_online"))
                    or not _positive(proof.session_id)
                    or any(getattr(proof, k) is not False for k in ("raid_active", "lab_active", "benchmark_active", "acquiring"))
                    or self.ports.conn.execute("SELECT 1 FROM guild_raid WHERE ended IS NULL LIMIT 1").fetchone()
                    or self.ports.conn.execute("SELECT 1 FROM tg_applied WHERE undone IS NULL LIMIT 1").fetchone()):
                return None
            return proof
        except Exception:
            return None

    def _verified(self):
        """Recompute the completed lab from its exact bounded receipt manifest.

        Only full, undropped stock/candidate/stock chains are announced here.
        Legacy ownership without a manifest and missing/changed native receipts
        stay silent. Derived SQL rates and copied summary scores are checks,
        never substitutes for the original results and restoration proof.
        """
        conn, now = self.ports.conn, self.ports.clock.now()
        try:
            owned = _read(conn, OWNED_KEY)
            if (not isinstance(owned, dict) or owned["phase"] != "complete" or owned["current"] is not None
                    or owned["halt"] != "dummy_rounds_complete" or type(owned["id"]) is not str
                    or not owned["id"] or len(owned["id"]) > 128 or not _finite(owned["started"])):
                return None
            night = date.fromisoformat(owned["night"]).isoformat()
            summary = _read(conn, SUMMARY_PREFIX + night)
            if (not isinstance(summary, dict) or summary["lab"] != owned["id"]
                    or summary["spec"] != owned["spec"] or not isinstance(summary["spec"], list)
                    or len(summary["spec"]) != 2 or any(type(v) is not int for v in summary["spec"])
                    or tuple(summary["spec"]) not in _DAMAGE_SPECS
                    or summary["reason"] != "dummy_rounds_complete" or summary["status"] != "dummy/provisional"
                    or summary["metric"] != "dummy_requested_damage_proxy" or summary["field_performance"] != "unavailable"
                    or not _finite(summary["ts"]) or not 0 <= now - summary["ts"] <= MAX_RESULT_AGE
                    or not 0 <= owned["started"] < summary["ts"]
                    or type(summary["pairs"]) is not int or not 4 <= summary["pairs"] <= 10
                    or type(summary["winner_round"]) is not int or not 0 <= summary["winner_round"] < 3
                    or type(summary["winner"]) is not str or summary["winner"] not in owned["remaining"]
                    or type(summary["attempts"]) is not int or summary["attempts"] != owned["attempts"]):
                return None
            guids = owned["guids"]
            if (not isinstance(guids, list) or not 2 <= len(guids) <= 10 or any(not _positive(g) for g in guids)
                    or len(set(guids)) != len(guids) or owned["preempted"] != [] or owned["gaps"] != []
                    or owned["alive"] != guids or summary["gaps"] != []):
                return None
            manifest, candidates = owned["attempt_ids"], owned["candidates"]
            if (not isinstance(manifest, list) or not 1 <= len(manifest) <= MAX_ATTEMPTS
                    or any(type(i) is not str or not 1 <= len(i) <= 128 for i in manifest)
                    or len(set(manifest)) != len(manifest) or len(manifest) != summary["attempts"]
                    or not isinstance(candidates, list) or not 2 <= len(candidates) <= 12):
                return None
            rotations = {}
            for candidate in candidates:
                name, rotation_id = candidate["name"], candidate["rotation"]
                if type(name) is not str or not 1 <= len(name) <= 128 or name in rotations or not _positive(rotation_id):
                    return None
                rotation = conn.execute("SELECT id,name,class,spec_tab,origin,status FROM tg_rotation WHERE id=?",
                                        (rotation_id,)).fetchone()
                if (rotation is None or rotation["name"] != name or rotation["origin"] != "lab"
                        or rotation["status"] != "dummy" or [rotation["class"], rotation["spec_tab"]] != summary["spec"]
                        or rotation_id in rotations.values()):
                    return None
                rotations[name] = rotation_id
            expected_rounds = [list(r) for r in halving_rounds(len(candidates))]
            if owned["rounds"] != expected_rounds:
                return None
            remaining, totals, rounds, receipts = list(rotations), {}, [], []
            seen_rows, cursor, previous = set(), 0, owned["started"] * 1000
            for round_, (_, seconds) in enumerate(expected_rounds):
                scores = {}
                for name in remaining:
                    pairs = []
                    for offset in range(0, len(guids), 5):
                        batch = guids[offset:offset + 5]
                        triplet = []
                        for leg in range(3):
                            attempt_id = manifest[cursor]
                            receipt = self._attempt(owned, summary, attempt_id, name if leg == 1 else "",
                                rotations[name] if leg == 1 else None, round_, seconds, batch, previous, seen_rows)
                            if receipt is None:
                                return None
                            previous = receipt["restoration_at"] * 1000
                            triplet.append(receipt)
                            receipts.append(receipt)
                            cursor += 1
                        # The producer orders each batch's pairs by string GUID.
                        for guid in sorted(map(str, batch)):
                            before, middle, after = (r["results"][guid] for r in triplet)
                            stock = (before["rate"] + after["rate"]) // 2
                            if middle["stock_rate"] != stock:
                                return None
                            pairs.append([middle["rate"], stock])
                    scores[name] = pairs
                    totals.setdefault(name, []).extend(pairs)
                rounds.append(dict(round=round_, scores=scores))
                ranked = sorted(scores, key=lambda name: (-_score(scores[name]), name))
                remaining = ranked[:(len(remaining) + 1) // 2]
            if (cursor != len(manifest) or owned["round_scores"] != rounds or owned["totals"] != totals
                    or owned["remaining"] != remaining or owned["round"] != len(rounds)
                    or summary["winner_round"] != len(rounds) - 1):
                return None
            last = rounds[-1]["scores"]
            qualified = sorted((n for n in remaining if len(last[n]) >= 4 and _score(last[n]) >= .02),
                               key=lambda n: (-_score(last[n]), n))
            if not qualified or summary["winner"] != qualified[0]:
                return None
            winner, pairs = qualified[0], last[qualified[0]]
            if summary["pairs"] != len(pairs) or summary["aggregate_pairs"] != len(totals[winner]):
                return None
            # Keep only checked fields. Arbitrary producer extras must never
            # become an expensive or nonfinite discovery fingerprint payload.
            checked_summary = {k: summary[k] for k in ("lab", "spec", "winner", "winner_round", "pairs",
                "aggregate_pairs", "attempts", "reason", "status", "metric", "field_performance", "ts")}
            return dict(summary=checked_summary, night=night, rotation=rotations[winner], pairs=pairs,
                        receipts=receipts, lab_started=owned["started"], participants=guids)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError, RecursionError):
            return None

    def _attempt(self, owned, summary, attempt_id, name, rotation, round_, seconds, guids, previous, seen_rows):
        current = _read(self.ports.conn, LAB_ATTEMPT_PREFIX + attempt_id)
        if (not isinstance(current, dict) or current["id"] != attempt_id or current["lab"] != owned["id"]
                or current["spec"] != owned["spec"] or not _integer(current["round"], 0, 2)
                or current["round"] != round_ or current["candidate_name"] != name
                or current["guids"] != guids or current["co"] != ["+custom::" + name if name else ""] * len(guids)
                or not _integer(current["seconds"], 1, 180) or current["seconds"] != seconds
                or current["metric"] != "dummy_requested_damage_proxy" or current["field_performance"] != "unavailable"
                or current["bad"] != "" or current.get("faults", []) != [] or current.get("rejected", False) is not False
                or current["ack"] != "accepted" or current["restoration"] != "complete_no_desired_setup"
                or not _integer(current["sent_ms"], 1) or current["sent_ms"] + 1 < previous
                or not _finite(current["restoration_at"]) or current["restoration_at"] > summary["ts"]):
            return None
        terminal, results, rows = current["terminal"], current["results"], current["rows"]
        if (not isinstance(terminal, dict) or terminal["id"] != attempt_id or terminal["kind"] != "dummy_trial"
                or terminal["state"] != "done" or terminal["reason"] != "" or terminal["stage"] != ""
                or terminal["guids"] != guids or terminal["skipped"] != [] or terminal.get("training_run", "") != ""
                or any(not _integer(terminal[k], 0, 0) for k in ("kills", "wipes", "instance_id", "guid", "level", "avg_ilvl"))
                or not _integer(terminal["ts"], current["sent_ms"])
                or terminal["ts"] > current["restoration_at"] * 1000 + 1
                or not isinstance(results, dict) or set(results) != set(map(str, guids))
                or not isinstance(rows, dict) or set(rows) != set(results)):
            return None
        checked = {}
        for guid in guids:
            key, result, row_id = str(guid), results[str(guid)], rows[str(guid)]
            if (not _integer(row_id, 1) or row_id in seen_rows or not isinstance(result, dict)
                    or result["id"] != attempt_id or not _positive(result["guid"]) or result["guid"] != guid
                    or result["dropped"] != "" or result["damage_basis"] != DAMAGE_BASIS or result["spell_basis"] != SPELL_BASIS
                    or not _integer(result["damage"], 1, 0xffffffffffffffff) or not _integer(result["hits"], 0, 0xffffffff)
                    or not _integer(result["ms"], seconds * 1000, seconds * 1000 + 5000)
                    or not _integer(result["ts"], current["sent_ms"] + result["ms"], terminal["ts"])):
                return None
            rate = result["damage"] * 1000 // result["ms"]
            row = self.ports.conn.execute("SELECT * FROM tg_dummy WHERE id=?", (row_id,)).fetchone()
            if (rate <= 0 or row is None or row["rotation"] != rotation or row["guid"] != guid or row["round"] != round_
                    or row["seconds"] != seconds or row["dps"] != rate or row["dropped"] != ""
                    or row["ts"] != current["sent_ms"] / 1000
                    or row["damage_basis"] != DAMAGE_BASIS or row["spell_basis"] != SPELL_BASIS
                    or not _integer(row["stock_dps"], 1 if name else 0) or not name and row["stock_dps"] != 0):
                return None
            seen_rows.add(row_id)
            checked[key] = dict(row=row_id, guid=guid, damage=result["damage"], ms=result["ms"], hits=result["hits"],
                                ts=result["ts"], rate=rate, stock_rate=row["stock_dps"])
        return dict(id=attempt_id, round=round_, candidate=name, sent_ms=current["sent_ms"],
                    terminal_ms=terminal["ts"], restoration_at=current["restoration_at"], results=checked)

    def _speakers(self, cls, week, *, reserved=None):
        preferred, officers = [], []
        away = self.ports.away()
        for guid, member in sorted(self.ports.cast().items()):
            persona = self.ports.persona(guid)
            if (not _positive(guid) or guid == self.ports.human_guid or member.guid != guid or member.online is not True
                    or persona is None or getattr(persona, "guid", None) != guid or guid in away
                    or self.ports.holder(guid) is not None
                    or guid != reserved and self.ports.conn.execute("SELECT 1 FROM meta WHERE key=?",
                        (f"{ATTEMPT_PREFIX}{guid}:{week}",)).fetchone()):
                continue
            if member.class_ == cls and getattr(getattr(persona, "game", None), "thinks_as", None) == "theorycrafter":
                preferred.append(guid)
            else:
                play = getattr(persona, "play", None)
                title = getattr(play, "officer_title", None)
                if getattr(play, "guild_role", None) == "officer" and type(title) is str and "theorycraft" in title.casefold():
                    officers.append(guid)
        return preferred + officers

    async def tick(self):
        """Attempt at most one normal ASIDE; True never means delivered speech.

        Work is local and bounded, with no await/model/bridge operation. Service
        owns timing. A durable per-speaker/week and per-lab claim precedes the
        synchronous proposal; failed/dropped/unknown proposals are not retried.
        """
        proof = self._safe()
        evidence = self._verified() if proof is not None else None
        if evidence is None:
            self.reason = "session_or_verified_dummy_result_unavailable"
            return False
        summary = evidence["summary"]
        local = self.ports.clock.local()
        year, number, _ = local.isocalendar()
        week = f"{year:04d}-W{number:02d}"
        speakers = self._speakers(summary["spec"][0], week)
        if not speakers:
            self.reason = "eligible_speaker_unavailable"
            return False
        speaker = speakers[0]
        keys = (f"{ATTEMPT_PREFIX}{speaker}:{week}", f"{ATTEMPT_PREFIX}lab:{summary['lab']}")
        receipt = dict(state="attempted", speaker=speaker, week=week, session=proof.session_id,
            at=self.ports.clock.now(), evidence=evidence, delivery="unconfirmed",
            fingerprint=hashlib.sha256(_json(evidence).encode()).hexdigest())
        try:
            with self.ports.conn:
                for key in keys:
                    if not self.ports.conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
                                                  (key, _json(receipt))).rowcount:
                        raise _Reserved()
        except _Reserved:
            self.reason = "already_attempted"
            return False
        fresh = self._safe()
        speakers_now = self._speakers(summary["spec"][0], week, reserved=speaker) if fresh is not None else []
        # No awaits split this operation, but a provider/current facts may still
        # change while proving the first snapshot. Keep cancelled attempts.
        if (fresh is None or fresh.session_id != proof.session_id or self._verified() != evidence
                or not speakers_now or speakers_now[0] != speaker):
            self._mark(keys, receipt, "cancelled")
            self.reason = "admission_changed"
            return False
        cls, tab = summary["spec"]
        instruction = (f"Share this verified lab result briefly in guild chat in your own voice: the {_SPECS[cls][tab]} "
            f"{CLASSES[cls].lower()} rotation candidate remains provisional after {summary['pairs']} paired training-dummy "
            "comparisons using a requested-damage proxy. Actual DPS and performance in raids remain unmeasured. "
            "Keep those limits clear. Do not invent a percentage, a specific rotation change, a raid verdict, "
            "or a claim that you personally ran the tests.")
        try:
            self.ports.propose(speaker, "guild", instruction, kind=TurnKind.ASIDE)
        except Exception:
            self._mark(keys, receipt, "proposal_failed")
            self.reason = "proposal_failed"
            return False
        self._mark(keys, receipt, "proposed_unconfirmed")
        self.reason = "proposed_unconfirmed"
        return True

    def _mark(self, keys, receipt, state):
        receipt = receipt | {"state": state}
        with self.ports.conn:
            for key in keys:
                self.ports.conn.execute("UPDATE meta SET value=? WHERE key=?", (_json(receipt), key))
