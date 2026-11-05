"""python -m leeroy_dojo replay <episode dir>: a recorded episode's observation and action timeline, without a game client.

It verifies the recording (recording.verify_episode), rebuilds the action catalog and observation schema from the
header and checks them against the worker's published hashes, then walks every decision: each actor's observation fits
the schema, its mask has the noop open and the requested action open, its action id is decision * actors + actor, the
next observation is the one the following decision answered, terminal rewards appear only at the end and sum to zero,
and every execution event's action id belongs to a request already made. The header's policies are rebuilt and fed the
same observations and masks; a deterministic policy must request exactly what was recorded. An aborted or incomplete
episode is reported and excluded, never replayed into a learning stream.

count_events() sums what the events say happened (damage, deaths, completed casts) for reconciliation with the core.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import actions, observation, policies
from .recording import _read_chunk, verify_episode

_INSTANT = re.compile(r"(?:^|; )cast (\w+) ok: cast(?:;|$)")


def read_records(path: Path, chunks: int) -> list[dict[str, Any]]:
    records = []
    for index in range(chunks):
        lines, _ = _read_chunk(Path(path) / f"chunk-{index:06d}.jsonl")
        records.extend(json.loads(line) for line in lines)
    return records


def rebuild(header: dict[str, Any]) -> tuple[actions.Catalog, observation.Schema, list[str]]:
    """The catalog and schema a recording was made with, and every way they differ from what the worker published."""
    problems = []
    catalog = actions.build_catalog(header["catalog_kit"])
    schema = observation.build_schema(catalog)
    published = header.get("schemas", {})
    if published.get("action") != f"{actions.SCHEMA}:{catalog.sha256}":
        problems.append(f"the action schema {published.get('action')!r} is not this catalog's {catalog.sha256[:12]}")
    if published.get("observation") != f"{observation.SCHEMA}:{schema.sha256}":
        problems.append(f"the observation schema {published.get('observation')!r} is not {schema.sha256[:12]}")
    if header.get("actions") != [policies.wire_name(e.name) for e in catalog.entries]:
        problems.append("the worker's action names are not this catalog's")
    return catalog, schema, problems


def completed_casts(event: dict[str, Any]) -> list[str]:
    """The kit abilities an execution event says went off: a cast that completed, or an instant the command cast."""
    if event.get("kind") == "cast" and event.get("outcome") == "completed":
        return ["?"]
    if event.get("kind") == "command" and event.get("producer") == "executor" and event.get("outcome") == "executed":
        return _INSTANT.findall(event.get("detail", ""))
    return []


def count_events(records: list[dict[str, Any]], catalog: actions.Catalog, actors: int = 2) -> list[dict[str, Any]]:
    """Per actor: damage done and taken, hits, deaths and completed kit casts by ability, from the events alone."""
    counts = [{"damage_done": 0, "damage_taken": 0, "hits": 0, "deaths": 0, "casts": Counter()} for _ in range(actors)]
    started: dict[tuple[int, int], str] = {}
    for record in records:
        requested = {a["action_id"]: catalog.entries[a["action"]] for a in record["actors"]}
        for e in record["events"]:
            kind = e.get("kind")
            if kind == "damage":
                if e.get("actor") is not None:
                    counts[e["actor"]]["damage_done"] += e["amount"]
                    counts[e["actor"]]["hits"] += 1
                if e.get("target") is not None:
                    counts[e["target"]]["damage_taken"] += e["amount"]
            elif kind == "death":
                counts[e["actor"]]["deaths"] += 1
            elif kind == "command" and e.get("outcome") == "queued" and e.get("action_id") is not None:
                entry = requested.get(e["action_id"])
                if entry is not None:
                    started[(e["actor"], e["action_id"])] = catalog.abilities[entry.ability].name
            for name in completed_casts(e):
                if name == "?":
                    name = started.get((e.get("actor"), e.get("action_id")), "?")
                counts[e["actor"]]["casts"][name] += 1
    return [{**c, "casts": dict(sorted(c["casts"].items()))} for c in counts]


def replay_episode(path: Path, *, timeline: bool = True) -> dict[str, Any]:
    path = Path(path)
    check = verify_episode(path)
    report: dict[str, Any] = {"episode": path.name, "complete": check.complete, "usable": check.usable,
                              "outcome": check.outcome, "transitions": check.transitions,
                              "problems": list(check.problems), "reproduced": None}
    try:
        header = json.loads((path / "header.json").read_text(encoding="utf-8"))
        footer = json.loads((path / "footer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        report["problems"].append(f"unreadable header or footer: {e}")
        report["ok"] = False
        return report
    if not check.usable:
        report["excluded"] = f"{check.outcome or 'incomplete'}: kept out of every learning stream"
        report["ok"] = check.complete and check.outcome == "aborted" and not check.problems
        return report
    problems = report["problems"]
    catalog, schema, mismatch = rebuild(header)
    problems += mismatch
    slots = [a["slot"] for a in header["actors"]]
    actor_count = len(slots)
    specs = sorted(header["policies"], key=lambda p: p["actor"])
    players = [policies.make(p, schema, catalog) for p in specs]
    for p in players:
        p.reset(header["episode"])
    records = read_records(path, check.valid_chunks)
    reproduced = 0
    differing: list[str] = []
    histogram = [Counter() for _ in slots]
    terminal_sum = [0.0 for _ in slots]
    shaping_sum = [0.0 for _ in slots]
    issued: set[int] = set()
    lines = []
    previous = None
    for k, record in enumerate(records):
        decision = record["decision"]
        last = k == len(records) - 1
        for a in record["actors"]:
            slot = a["actor"]
            obs, mask, action = a["obs"], a["mask"], a["action"]
            problems += [f"decision {decision} actor {slot}: {p}" for p in schema.check(obs)][:3]
            if len(mask) != len(catalog.entries) or mask[0] != 1:
                problems.append(f"decision {decision} actor {slot}: the mask does not fit the catalog or closes the noop")
            elif not mask[action]:
                problems.append(f"decision {decision} actor {slot}: requested {action}, which its mask closed")
            if a["action_id"] != decision * actor_count + slot:
                problems.append(f"decision {decision} actor {slot}: action id {a['action_id']}")
            if previous is not None:
                before = previous["actors"][slot]
                if before["next_obs"] != obs or before["next_mask"] != mask:
                    problems.append(f"decision {decision} actor {slot}: not the observation decision {decision - 1} led to")
            choice = players[slot].act(obs, mask)
            if choice == action:
                reproduced += 1
            elif len(differing) < 5:
                differing.append(f"decision {decision} actor {slot}: recorded {action}, the policy now asks {choice}")
            histogram[slot][catalog.entries[action].name] += 1
            terminal_sum[slot] += a["reward"]["terminal"]
            shaping_sum[slot] += a["reward"]["shaping"]
            if a["reward"]["terminal"] and not last:
                problems.append(f"decision {decision} actor {slot}: a terminal reward before the end")
            issued.add(a["action_id"])
        for e in record["events"]:
            aid = e.get("action_id")
            if e.get("producer") in ("executor", "protocol", "core") and "executor_id" in e and aid is None:
                problems.append(f"decision {decision}: an {e['producer']} {e['kind']} record of no request")
            elif aid is not None and aid not in issued:
                problems.append(f"decision {decision}: an event of action {aid}, which was not requested yet")
            if e.get("producer", "").startswith("stock"):
                problems.append(f"decision {decision}: stock behaviour ({e.get('kind')} {e.get('detail', '')[:60]})")
        if timeline:
            notable = [e for e in record["events"] if e.get("kind") in ("damage", "death")
                       or completed_casts(e) or e.get("outcome") in ("rejected", "refused")]
            if notable or k == 0 or last:
                lines.append({"decision": decision, "t_ms": record["t_ms"], "elapsed_ms": record["elapsed_ms"],
                              "actions": [catalog.entries[a["action"]].name for a in record["actors"]],
                              "events": [_brief(e) for e in notable]})
        previous = record
    if records:
        end = records[-1]
        want = {"reason": footer.get("reason"), "winner": footer.get("winner")}
        if end["outcome"] != want:
            problems.append(f"the last decision's outcome {end['outcome']} is not the footer's {want}")
        if abs(sum(terminal_sum)) > 1e-9:
            problems.append(f"terminal rewards sum to {sum(terminal_sum)}")
        reason = (end["outcome"] or {}).get("reason")
        deaths = [e["actor"] for r in records for e in r["events"] if e.get("kind") == "death"]
        if reason == "death" and not deaths:
            problems.append("a death outcome without a death event")
        if reason == "simultaneous_death" and sorted(set(deaths)) != sorted(slots):
            problems.append("simultaneous death without both actors' death events")
        if reason == "time_limit" and deaths:
            problems.append("a time-limit draw with a death event")
    report.update({
        "reproduced": {"decisions": reproduced, "of": sum(len(r["actors"]) for r in records), "differing": differing},
        "policies": specs, "actions": [dict(h.most_common(8)) for h in histogram],
        "returns": [{"terminal": round(t, 6) + 0.0, "shaping": round(s, 6) + 0.0}
                    for t, s in zip(terminal_sum, shaping_sum)],
        "counts": count_events(records, catalog, actor_count), "duration_ms": records[-1]["t_ms"] if records else 0})
    if timeline:
        report["timeline"] = lines
    report["ok"] = not problems and not differing
    return report


def _brief(e: dict[str, Any]) -> str:
    kind = e.get("kind")
    if kind == "damage":
        return f"{e.get('actor')}->{e.get('target')} {e.get('amount')}"
    if kind == "death":
        return f"actor {e.get('actor')} died"
    return f"actor {e.get('actor')} {kind} {e.get('outcome')} {e.get('detail', '')[:60]}".strip()
