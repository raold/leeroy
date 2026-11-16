"""A fixed talk scenario over a synthetic cast, recorded as every prompt the engine composed,
every line it sent and the speaker weights it drew from. The first invariant of tuning ("no
settings means identical prompts") compares a run of it against golden_prompts.json, which was
written by this same scenario with the code from before tuning existed. Re-recorded since for
members starting conversations (2026-11-07): its one evening whisper says she starts it and
what she brings up, and it no longer comes from Belra, whom he had whispered a minute before
(a member in a whisper with him starts none), so the draws after it moved too. Its greetings keep
the draw they were recorded with (2-3 greeters, no plain hello): the 0-2 greeters of 2026-11-15
have their own tests (test_orbit.py), as the initiation clocks do. Re-recorded on 2026-11-15
for the identity card (identity audit): each card opens with the member's age and sex in
words, pronouns and household ("You are Sam, a 34-year-old woman from the UK (she/her)...",
"At home: ..."), and each other member is shown once with their pronoun ("Belra (she): ...").
Re-recorded for every message of his answered (2026-11-15, talk/owed.py): his bare remarks
always get an answer and his hello in the login quiet keeps one, so more turns are planned and
the draws after them moved; no prompt's wording changed. The evening whisper after his loot is
a fixed draw since then (Dunna, 104, whose draw the extra turns had consumed), besides the
natural one the planner may still make, so the whisper-start instruction stays covered.

The beat before an answer to his whisper or DM ([talk] think_private, talk/beat.py, 2026-11-16) is
off in this scenario, so its whisper answer is the one recorded before; the beat has its own
golden, golden_beat.json (record_beat): two answers to his whispers with the beat on, every
request as sent (the beat's prompt, format and options, and each line's prompt with the beat's
lines after its instruction).

The core golden, golden_core.json (record(..., core=True); human-likeness core plan Task 8), runs
the same script with [life] availability 6, [life] state, [talk] intent and [talk] situation on and
the beat off, so every block after an instruction is the code's (the drawn want and the state's
"It lands"). The rig's clock is fixed (Thursday 2026-10-29, 20:00 UTC, which the test cast's UK
members read as 20:00 GMT, the week UK and US clocks differ; the script ends near 20:36): every
member is at her PC playing then (her gaming block, full attention in game; early shifts, asleep
from 21:15), so nobody is away by schedule and each answer comes after her attention's seeded
latency draw.

[romance] realism (talk/romance.py REALISM_LEAD, 2026-11-16) is off in all of these;
golden_romance.json (record_romance) holds the plain scenario with it on: her romance text with
the lead and without what pushed every reply to escalate, his card's note as one current
sentence, old plans out of recall. With it off every prompt is the one golden_prompts.json
recorded."""
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
from leeroy_mind.talk import beat

GOLDEN = Path(__file__).with_name("golden_prompts.json")
BEAT_GOLDEN = Path(__file__).with_name("golden_beat.json")
CORE_GOLDEN = Path(__file__).with_name("golden_core.json")
ROMANCE_GOLDEN = Path(__file__).with_name("golden_romance.json")


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


async def _run(store, clock, data_dir, core: bool = False, *, realism: bool = False) -> dict:
    r = make_rig(store, clock, data_dir, script=numbered("ok so line {n} then"), seed=7,
                 availability=6 if core else 0, life_state=core, intent=core, situation=core,
                 romance_realism=realism)
    # the initiation clocks came later and have their own tests (test_initiate.py): off here,
    # so the scenario's draws stay the ones the golden recorded
    r.engine.whispers.set_rate(0.0)
    r.engine.budget.set_to_him_rate(0.0)
    r.engine.greeter_draw = lambda rng: rng.randint(2, 3)
    r.engine.plain_hello_p = 0.0
    r.engine.think_private = False  # the beat has its own golden (record_beat)
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
    # the evening whisper after his loot, as a fixed draw: 104 starts it (2026-11-16)
    r.engine._queue_whisper(104, "evening", clock.now(), about="Zan just won Shiny Helm.")
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


def record(store, clock, data_dir, core: bool = False) -> dict:
    """`clock`: engine_clock(), the store's own. `core`: the core prompt's switches on, as
    golden_core.json was recorded."""
    cast(store)
    return asyncio.run(_run(store, clock, data_dir, core))


def record_romance(store, clock, data_dir) -> dict:
    """The scenario with [romance] realism on (golden_romance.json)."""
    cast(store)
    return asyncio.run(_run(store, clock, data_dir, realism=True))


def write_golden_romance(result: dict) -> None:
    ROMANCE_GOLDEN.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n",
                              encoding="utf-8", newline="\n")


def golden_romance() -> dict:
    return json.loads(ROMANCE_GOLDEN.read_text(encoding="utf-8"))


def write_golden(result: dict) -> None:
    GOLDEN.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8",
                      newline="\n")


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def write_golden_core(result: dict) -> None:
    CORE_GOLDEN.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8",
                           newline="\n")


def golden_core() -> dict:
    return json.loads(CORE_GOLDEN.read_text(encoding="utf-8"))


def _beat_model():
    """The model in record_beat: a beat (bringing in the first thing it was offered, when there
    is one) for the beat's request, the next line for the line's."""
    lines = iter(["mm hey you, was hoping you'd whisper", "ugh, what did he do this time?"])

    def reply(model, messages) -> str:
        last = messages[-1]["content"]
        if beat.MARK not in last:
            return next(lines)
        return json.dumps({"read": "he had a rough day and wants someone to listen",
                           "lands": "a soft spot, a little worried", "want": "share",
                           "use": 1 if "\n1. " in last else None})
    return reply


async def _run_beat(store, clock, data_dir) -> dict:
    r = make_rig(store, clock, data_dir, script=_beat_model(), seed=7, beat=True)
    r.engine.whispers.set_rate(0.0)
    r.engine.budget.set_to_him_rate(0.0)
    await r.session()
    await r.human("hey you", channel="whisper", to_guid=102)
    await r.advance(40, step=2)
    await r.human("long day, my boss was on my case again", channel="whisper", to_guid=102)
    await r.advance(40, step=2)
    calls = [{k: c[k] for k in ("messages", "format", "options", "think")}
             for c in r.chat_calls()]
    return {"calls": calls, "sent": [[s.guid, s.channel, s.text] for s in r.sent.says()]}


def record_beat(store, clock, data_dir) -> dict:
    """Two answers to his whispers with the beat on: every request as sent, and the lines."""
    cast(store)
    return asyncio.run(_run_beat(store, clock, data_dir))


def write_golden_beat(result: dict) -> None:
    BEAT_GOLDEN.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8",
                           newline="\n")


def golden_beat() -> dict:
    return json.loads(BEAT_GOLDEN.read_text(encoding="utf-8"))


__all__ = ["TEST_CAST", "cast", "engine_clock", "golden", "golden_beat", "golden_core",
           "golden_romance", "record", "record_beat", "record_romance", "write_golden",
           "write_golden_beat", "write_golden_core", "write_golden_romance"]
