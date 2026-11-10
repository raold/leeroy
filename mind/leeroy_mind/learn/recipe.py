"""Atomic generation identifiers; no prompt, persona, name or message content."""
from hashlib import sha256
from dataclasses import dataclass, replace
import json
from leeroy_mind.tuning.dials import MEMBER_DIALS, WORLD_DIALS
from leeroy_mind.tuning.effective import dials_of
from .weights import available

VERDICTS = frozenset({"not_run", "send", "rewrite", "react", "drop", "failed_open"})
TEMPLATE_VERSION = 1


@dataclass(frozen=True)
class Snapshot:
    template_id: str
    template_version: int
    dials_hash: str
    model_id: str


def describe(turn, persona, config):
    dials = dials_of(persona)
    values = {name: dials.value(name, persona) for name in MEMBER_DIALS}
    values.update({"world:" + name: getattr(getattr(config, dial.section), dial.key)
                   for name, dial in WORLD_DIALS.items()})
    digest = sha256(json.dumps(values, sort_keys=True, separators=(",", ":"),
                              allow_nan=False).encode("ascii")).hexdigest()
    template = "talk.template" if turn.template is not None else (
        "talk.bit" if turn.kind.value == "bit" and turn.max_lines > 1 else "talk.live")
    return Snapshot(template, TEMPLATE_VERSION, digest,
                    "none" if turn.template is not None else config.models.live)


def called_model(meta, model):
    snapshot = getattr(meta, "recipe_snapshot", None)
    if isinstance(snapshot, Snapshot):
        meta.recipe_snapshot = replace(snapshot, model_id=model)


def record(conn, line_id, turn, meta, persona, config):
    if not available(conn):
        return False
    snapshot = getattr(meta, "recipe_snapshot", None)
    if not isinstance(snapshot, Snapshot):
        snapshot = describe(turn, persona, config)
    verdict = getattr(meta, "guard_verdict", "not_run")
    if verdict not in VERDICTS:
        verdict = "not_run"
    conn.execute("INSERT INTO talk_recipe(line_id,template_id,template_version,dials_hash,"
                 "model_id,guard_verdict,arm_ids,lesson_ids) VALUES (?,?,?,?,?,?,?,?)",
                 (line_id, snapshot.template_id, snapshot.template_version, snapshot.dials_hash,
                  snapshot.model_id, verdict, "[]", "[]"))
    return True
