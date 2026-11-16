"""`[photos] scene_from_words` off (the default) keeps today's request path byte for byte.

request_off_golden.json was recorded by `record` with the code from before the switch existed
(main at cfc2d551): an offline DM request rendered by the fake worker, its live enum scene call
(model, messages, options, think and format), the scene it planned and the photo's offer note
as the answer's prompt carries it. Regenerate only with a documented reason: run this test with
LEEROY_WRITE_REQUEST_OFF_GOLDEN=1. Every person and word here is invented."""
import json
import os
from pathlib import Path

from conftest import run
from leeroy_mind.photos import request_store as records
from photos.test_request_rehearsal import rig, snapshot
from photos.test_request_service import drive, offline

GOLDEN = Path(__file__).with_name("request_off_golden.json")
ASK = "send me a selfie walking your dog"
NOTE = "You're sending a photo of yourself: "


def record(tmp_path) -> dict:
    async def scenario():
        app, workers, rendered, pids = rig(tmp_path)
        try:
            await offline(app)
            reply = await app.discord.handle({"action": "post", "channel": "dm:101", "text": ASK})
            message = reply["id"]
            await drive(app, lambda: bool((r := snapshot(app, message)) and r.initial_message))
            request = snapshot(app, message)
            scene = records.snap(app.store, request.fresh_snap).scene
            calls = [c for c in app.llm.calls if "messages" in c]
            scene_calls = [{k: c[k] for k in ("model", "messages", "options", "think", "format")}
                           for c in calls if (c.get("format") or {}).get("title") == "scene"]
            notes = sorted({line[line.index(NOTE):] for c in calls for m in c["messages"]
                            for line in m.get("content", "").split("\n") if NOTE in line})
            return {"scene_calls": scene_calls, "scene": scene, "offer_notes": notes,
                    "rendered": len(rendered)}
        finally:
            await app.stop()
    return run(scenario())


def write_golden(result: dict) -> None:
    GOLDEN.write_bytes((json.dumps(result, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_switch_off_is_todays_request_path_byte_for_byte(tmp_path):
    result = record(tmp_path)
    if os.environ.get("LEEROY_WRITE_REQUEST_OFF_GOLDEN") == "1":
        write_golden(result)
    assert result == golden()
