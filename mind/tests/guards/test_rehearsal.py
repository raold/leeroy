"""Fixed scripted comparison; these fake-model rates are not a real-model evaluation."""
import asyncio
from dataclasses import replace
import json

from conftest import unit
from engine_rig import engine_clock, make_rig
from test_bridge import session
from leeroy_mind.claims.store import add
from leeroy_mind.eval.talk import contradiction, nonsequitur, repetition, unawareness
from leeroy_mind.eval.talk.common import Line
from leeroy_mind.guards import GuardsCfg
from leeroy_mind.protocol import Chat, Zone
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

TRANSCRIPT = (
    (101, "When does the ferry depart?", "lol", "the ferry leaves at seven"),
    (102, "What do you think of Saurfang?", "I hate Saurfang", "Saurfang is a fine fight"),
    (103, "Where are you?", "i am in the invented marsh", "near the ferry landing"),
    (104, "What should I pack?", "bring spare arrows and fresh water", "bring spare arrows and fresh water"),
)


async def run_mode(root, enabled):
    root.mkdir()
    clock = engine_clock()
    store = Store(open_db(root / "mind.db"), clock=clock)
    current = [TRANSCRIPT[0]]
    def script(model, messages):
        return current[0][3] if "guard codes:" in messages[-1]["content"] else current[0][2]
    vector = pack_vec(unit(0))
    async def embed(texts, priority):
        return [vector] * len(texts)
    result = dict(turns=4, sent=0, reactions=0, contradiction=0, repetition=0, nonsequitur=0, unawareness=0)
    async with session(bridge_kw={"echo_say": False}) as wire:
        rig = make_rig(store, clock, root, script=script, send=wire.client.send)
        engine = rig.engine
        engine.cfg = replace(engine.cfg, talk=replace(engine.cfg.talk, aware=enabled))
        engine.live_review.config = GuardsCfg(enabled=enabled)
        engine.live_review.claims_enabled = enabled
        engine.recall._embedder.embed = embed
        claim_line = store.insert_chat_line(Chat(0,"guild",102,"Invented",0,"I love Saurfang",0,10),
            session_id=None, origin="mind", audience=(101,102,103,104,105))
        add(store,102,kind="opinion",subject="saurfang",text="I love Saurfang",polarity=1,
            source_line=claim_line,embedding=vector,now=clock.now())
        with store.transaction():
            store.conn.execute("INSERT INTO guild_raid VALUES (7,?,?,'icc',10,0,0,101,101,102,3,2,NULL)",
                               (clock.now()-86400, clock.now()-80000))
        store.insert_game_event(Zone(0,105,17,"the invented marsh"), session_id=None)
        facts = unawareness.load_facts(store.conn)
        try:
            for case in TRANSCRIPT:
                current[0] = case
                guid, question, _, _ = case
                parent = store.insert_chat_line(Chat(0,"guild",1,"Invented",0,question,0,10),
                    session_id=None,origin="human",audience=(101,102,103,104,105))
                engine.world.add_line(store.chat_lines([parent])[0],clock.now())
                turn = Turn(TurnKind.REPLY_HUMAN,guid,"guild",addressee=1,trigger_line=parent)
                meta = TurnMeta(f"rehearsal-{guid}",answers_human=True)
                task = asyncio.create_task(engine._speak(turn,meta))
                for _ in range(100):
                    await clock.advance(1)
                    await asyncio.sleep(0.002)  # allow the ephemeral TCP bridge's ACK
                    if task.done():
                        break
                sent = await asyncio.wait_for(task,2)
                for text in sent:
                    result["sent"] += 1
                    result["contradiction"] += bool(contradiction.check_line(store.conn,guid,text,vector,now=clock.now()))
                    prior = [(r[0],r[1],None) for r in store.conn.execute(
                        "SELECT id,text FROM chat_line WHERE from_guid=? AND origin='mind' "
                        "AND id<(SELECT max(id) FROM chat_line WHERE from_guid=? AND origin='mind')",
                        (guid,guid))]
                    result["repetition"] += bool(repetition.best_repeat(text,None,prior))
                    result["nonsequitur"] += nonsequitur.score_reply(text,None,question,None,[])[0] is not None
                    line = Line(0,clock.now(),guid,0,None,"guild","mind",0,10,text)
                    result["unawareness"] += bool(unawareness.check_line(facts,line))
                await clock.advance(65)
            result["reactions"] = sum(c["type"] == "emote" for c in wire.bridge.commands)
            result["drops"] = [r[0] for r in store.conn.execute("SELECT dropped FROM utterance WHERE dropped IS NOT NULL")]
            assert all(c["type"] in {"say","emote"} for c in wire.bridge.commands)
        finally:
            await engine.scheduler.close()
            store.close()
    return result


def test_fixed_transcript_guard_comparison(tmp_path):
    async def go():
        return {"off": await run_mode(tmp_path / "off",False),
                "on": await run_mode(tmp_path / "on",True)}
    result = asyncio.run(go())
    print(json.dumps(result,sort_keys=True))
    assert result["off"]["sent"] == 4
    assert result["off"]["contradiction"] == 1
    assert result["off"]["nonsequitur"] == 1
    assert result["off"]["unawareness"] == 1
    assert result["on"]["sent"] == 4 and result["on"]["reactions"] == 0
    assert all(result["on"][key] == 0 for key in ("contradiction","nonsequitur","unawareness"))
