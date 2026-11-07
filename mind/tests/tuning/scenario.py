"""A fixed talk scenario over a synthetic cast, recorded as every prompt the engine composed,
every line it sent and the speaker weights it drew from. The first invariant of tuning ("no
settings means identical prompts") compares a run of it against golden_prompts.json, which was
written by this same scenario with the code from before tuning existed. Re-recorded since for
members starting conversations (2026-11-07): its one evening whisper says she starts it and
what she brings up, and it no longer comes from Belra, whom he had whispered a minute before
(a member in a whisper with him starts none), so the draws after it moved too."""
import asyncio
import json
import random
from pathlib import Path

from conftest import HUMAN, TEST_CAST
from engine_rig import engine_clock, make_rig, numbered, save_member
from leeroy_mind.protocol import Login, LootWon
from leeroy_mind.talk.romance import flirt_chance, attitude_text
from leeroy_mind.talk.select import SpeakerContext, speaker_weight
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.states import roll_session_state

GOLDEN = Path(__file__).with_name("golden_prompts.json")


def cast(store) -> None:
    """Two women who want him, a fond one, an envious man and a drinker."""
    save_member(store, 101, jealousy_mode="teaser", thinks_as="lore",
                extraversion={"score": 3, "behaviour": "keeps to themselves"},
                habits=[{"substance": "alcohol", "when": "daily", "odds": 0.6,
                         "max_state": "drunk"}])
    save_member(store, 102, player_gender="female", stance="drawn", style="bold",
                agreeableness={"score": 3, "behaviour": "pushes back on plans"})
    save_member(store, 103, jealousy_mode="sulker", thinks_as="social")
    save_member(store, 104, player_gender="female", stance="lust", style="theatrical",
                thinks_as="collector")
    save_member(store, 105, player_gender="female", stance="fond", humour="teasing",
                habits=[{"substance": "weed", "when": "daily", "odds": 0.5, "max_state": "high"}])


async def _run(store, clock, data_dir) -> dict:
    r = make_rig(store, clock, data_dir, script=numbered("ok so line {n} then"), seed=7)
    # the initiation clocks came later and have their own tests (test_initiate.py): off here,
    # so the scenario's draws stay the ones the golden recorded
    r.engine.whispers.set_rate(0.0)
    r.engine.budget.set_to_him_rate(0.0)
    await r.session()
    await r.event(Login(ts=0, guid=HUMAN, human=True))
    await r.advance(90, step=2)
    await r.human("hey all")
    await r.advance(40, step=2)
    await r.human("anyone know what gear a tank should chase first?")
    await r.advance(40, step=2)
    await r.human("what are you talking about?")
    await r.advance(40, step=2)
    await r.human("hey you", channel="whisper", to_guid=102)
    await r.advance(40, step=2)
    await r.event(LootWon(ts=0, guid=HUMAN, item_id=1, name="Shiny Helm", method="roll"))
    await r.advance(60, step=2)
    for n in range(6):  # he stays active: social bursts
        await r.advance(280, step=4)
        await r.human(f"lol {'nice' if n % 2 else 'true'}")
    await r.advance(60, step=2)
    await r.engine.end_session()
    ctx = SpeakerContext(now=clock.now(), topic={"gear", "tank"}, opener=True,
                         phase=DayPhase.EVENING)
    personas = r.engine.personas.all()
    weights = {str(g): repr(speaker_weight(p, store.get_state(g), None, ctx, None, 1.0))
               for g, p in personas.items()}
    flirts = {str(g): repr(flirt_chance(p, 1.3)) for g, p in personas.items()}
    attitudes = {str(g): attitude_text(p) for g, p in personas.items()
                 if p.identity.player_gender == "female"}
    states = {str(g): roll_session_state(p, clock.local(), None, clock.now(),
                                          random.Random(g)).value
              for g, p in personas.items()}
    return {"prompts": r.prompts(), "sent": [[s.guid, s.channel, s.text] for s in r.sent.says()],
            "weights": weights, "flirts": flirts, "attitudes": attitudes, "states": states}


def record(store, clock, data_dir) -> dict:
    """`clock`: engine_clock(), the store's own."""
    cast(store)
    return asyncio.run(_run(store, clock, data_dir))


def write_golden(result: dict) -> None:
    GOLDEN.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8",
                      newline="\n")


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


__all__ = ["TEST_CAST", "cast", "engine_clock", "golden", "record", "write_golden"]
