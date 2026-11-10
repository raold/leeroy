"""A nudge's before/after, through the existing preview path and resident-model gate."""
import asyncio
from dataclasses import replace

from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.talk.turns import Turn, TurnKind

from . import nudges as N
from .preview import LEASE, LINE_DEADLINE_S, REFUSE, admission


async def preview(app, args):
    pic, now = app.tuning.picture(), app.clock.now()
    current = app.personas.tuning
    args = N.checked(args, pic, set(current.groups), app.engine.human)
    n = N.Nudge(0, args["target_kind"], args["target"], args["text"], args["strength"],
                args["scope"], now, now + args["seconds"], now)
    subjects = [g for g, p in sorted(pic.personas.items()) if g in pic.cast and
                N.for_member((n,), p, pic.cast[g], current.groups, pic.human)][:3]
    if not subjects:
        return {"ok": False, "error": REFUSE["nobody"]}
    why = await admission(app)
    if why:
        return {"ok": False, "error": REFUSE[why]}
    candidate = replace(current, nudges=current.nudges + (n,))
    arbiter, rows = app.arbiter, []
    first = arbiter.hold(LEASE)
    try:
        for guid in subjects:
            channel = "discord:general" if n.scope == "discord" else (
                "whisper" if n.scope == "whispers" else "guild")
            to = pic.human if channel == "whisper" else None
            row = {"guid": guid, "name": pic.personas[guid].character_name}
            for key, tuning in (("before", current), ("after", candidate)):
                turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid, channel=channel,
                            to_guid=to, addressee=pic.human, max_lines=1,
                            priority=Priority.BACKGROUND,
                            deadline=app.clock.now() + LINE_DEADLINE_S)
                p = tuning.apply(pic.personas[guid], pic.cast[guid])
                try:
                    text, reason = await app.engine.preview_line(
                        turn, p, trigger="how are you doing today?", seed=guid)
                except ValueError:
                    text, reason = "", "tells"
                row[key] = {"text": "" if reason else text,
                            "note": REFUSE["silent"] if reason == "silent" else
                            REFUSE["tells"] if reason else ""}
            rows.append(row)
        return {"ok": True, "kind": "lines", "subjects": rows}
    finally:
        await arbiter.release_live(LEASE)
        if first and arbiter.holders:
            asyncio.ensure_future(arbiter.hand_over())
