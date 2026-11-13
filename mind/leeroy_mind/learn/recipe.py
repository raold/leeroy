"""Atomic generation identifiers; no prompt, persona, name or message content."""
from hashlib import sha256
from dataclasses import dataclass, replace
import json
from leeroy_mind.tuning.dials import MEMBER_DIALS, WORLD_DIALS
from leeroy_mind.tuning.effective import dials_of

VERDICTS = frozenset({"not_run", "send", "rewrite", "react", "drop", "failed_open"})


@dataclass(frozen=True)
class Snapshot:
    template_id: str
    template_version: str
    dials_hash: str
    model_id: str
    arm_ids: tuple[str, ...] = ()
    bandit_context: str | None = None
    lesson_ids: tuple[int, ...] = ()
    callback_id: int | None = None
    num_predict: int | None = None
    parameters: tuple = ()


def describe(turn, persona, config):
    dials = dials_of(persona)
    values = {name: dials.value(name, persona) for name in MEMBER_DIALS}
    values.update({"world:" + name: getattr(getattr(config, dial.section), dial.key)
                   for name, dial in WORLD_DIALS.items()})
    digest = sha256(json.dumps(values, sort_keys=True, separators=(",", ":"),
                              allow_nan=False).encode("ascii")).hexdigest()
    from leeroy_mind.talk import prompt
    template = ("talk.template." if turn.template is not None else "talk.instruction.") + turn.kind.value
    text = turn.template if turn.template is not None else prompt.COMMS_RULES + prompt.INSTRUCTIONS[turn.kind]
    if turn.kind.value in ("bit","aside","emote_react"):
        text += turn.instruction
    version = sha256(text.encode("utf-8",errors="replace")).hexdigest()[:16]
    return Snapshot(template, version, digest,
                    "none" if turn.template is not None else config.models.live)


def templates_used(meta, used):
    snapshot = getattr(meta,"recipe_snapshot",None)
    if isinstance(snapshot,Snapshot):
        digest = sha256(json.dumps([snapshot.template_version,sorted(used.items())],
                                  separators=(",",":")).encode("ascii")).hexdigest()[:16]
        meta.recipe_snapshot = replace(snapshot,template_id="+".join(sorted({snapshot.template_id,*used})),
                                       template_version=digest)


def called_model(meta, model, options=None):
    snapshot = getattr(meta, "recipe_snapshot", None)
    if isinstance(snapshot, Snapshot):
        params = tuple((k,v) for k,v in (options or {}).items()
                       if k in ('temperature','min_p','top_p','num_predict','num_ctx','seed')
                       and type(v) in (int,float))
        meta.recipe_snapshot = replace(snapshot, model_id=model, parameters=params)


def record(conn, line_id, turn, meta, persona, config, *, settings=None):
    from .control import frozen
    if frozen(conn, settings):
        return False
    anchor = getattr(meta, "recipe_line_id", None)
    if anchor is not None and conn.execute(
            "SELECT 1 FROM talk_recipe p JOIN chat_line c ON c.id=p.line_id "
            "WHERE p.line_id=? AND c.from_guid=? AND c.channel=? AND c.to_guid=?",
            (anchor, turn.speaker, turn.channel, turn.to_guid)).fetchone():
        conn.execute("INSERT INTO talk_recipe_line VALUES (?,?)", (line_id, anchor))
        return True
    snapshot = getattr(meta, "recipe_snapshot", None)
    if not isinstance(snapshot, Snapshot):
        snapshot = describe(turn, persona, config)
    verdict = getattr(meta, "guard_verdict", "not_run")
    if verdict not in VERDICTS:
        verdict = "not_run"
    conn.execute("INSERT INTO talk_recipe(line_id,template_id,template_version,dials_hash,"
                 "model_id,guard_verdict,arm_ids,lesson_ids) VALUES (?,?,?,?,?,?,?,?)",
                 (line_id, snapshot.template_id, snapshot.template_version, snapshot.dials_hash,
                  snapshot.model_id, verdict, "[]", json.dumps(snapshot.lesson_ids)))
    if snapshot.arm_ids:
        from .bandit import valid_key
        if not valid_key(snapshot.bandit_context):
            raise ValueError("recipe_bandit_context_invalid")
        conn.execute("UPDATE talk_recipe SET arm_ids=?,bandit_context=? WHERE line_id=?",
                     (json.dumps(snapshot.arm_ids),snapshot.bandit_context,line_id))
    if snapshot.callback_id is not None or snapshot.num_predict is not None:
        conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (f"learn:recipe:{line_id}",
                     json.dumps(dict(version=1, callback_id=snapshot.callback_id,
                                     num_predict=snapshot.num_predict,
                                     parameters=dict(snapshot.parameters)), separators=(",", ":"))))
    conn.execute("INSERT INTO talk_recipe_line VALUES (?,?)", (line_id, line_id))
    meta.recipe_line_id = line_id
    return True
