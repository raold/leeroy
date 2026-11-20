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

Re-recorded on 2026-11-16 for DM quality (test_tuning_invariant.py says what moved): the hint's
topics are content words only and his unanswered questions are answered first; the beat's `use`
bears directly on his last line or is null. The draws and the lines sent are unchanged.

The core golden, golden_core.json (record(..., core=True); human-likeness core plan Task 8), runs
the same script with [life] availability 6, [life] state, [talk] intent and [talk] situation on and
the beat off, so every block after an instruction is the code's (the drawn want and the state's
"It lands"). The rig's clock is fixed (Thursday 2026-10-29, 20:00 UTC, which the test cast's UK
members read as 20:00 GMT, the week UK and US clocks differ; the script ends near 20:36): every
member is at her PC playing then (her gaming block, full attention in game; early shifts, asleep
from 21:15), so nobody is away by schedule and each answer comes after her attention's seeded
latency draw. Re-recorded with [talk] shape on as well (core plan Task 9): each eligible answer's
length is its drawn shape's line ("One short line, about 5 words.", "Keep it short and ask him
something back.", the evening whisper's "Say it in a line or two, about 16 words.") in place of
her usual line; every answer in the script is still said (none drew a shape that leaves it on
read or only reacts), so the sent lines are as before.

[romance] realism (talk/romance.py REALISM_LEAD, 2026-11-16) is off in all of these;
golden_romance.json (record_romance) holds the plain scenario with it on: her romance text with
the lead and without what pushed every reply to escalate, his card's note as one current
sentence, old plans out of recall. With it off every prompt is the one golden_prompts.json
recorded.

His DM as the Chat's last line (2026-11-16, Pipeline._his_dm_row: every DM answer to him, no
switch) changed none of these goldens: the scenario has no Discord DM, only game channels and
whispers (whose trigger line was already in the Chat). tests/discord/test_dm_prompt.py and
tests/test_romance_realism.py cover it.

Re-recorded on 2026-11-20 for geography ([geography], on by default; geography/README.md): every
whisper and DM prompt gains her "where you live, right now" line just before "Right now:" (the
test cast is British with no place in its words, so it is the UK as a whole: "Where you live: the
UK, ..."). Nothing else moved: their cards gain no home line (the UK as a whole has none), his
[owner] home is unset, the clock reads the test cast's UK time as before, and the draws and the
lines sent are unchanged.

Voice sheets (Persona.speech, 2026-11-17) changed none of these four goldens: no member of the
scenario's cast has one. golden_voice.json (record_voice) holds the same script in both prompt
modes ("plain" and "core") with invented sheets (tests/sheets.py) on Belra (102) and Coros
(103): their cards without the trait sentences and the attachment and humour lines, no "How you
type" samples, and the voice block just before each of their instructions. The other three
members' prompts in it are the ones the other goldens hold.

WoW grounding ([talk] wow_grounding, talk/grounding.py, 2026-11-20; on by default in mind.toml)
is off in every rig unless a test asks (engine_rig.make_rig), so none of these goldens moved.
golden_wow.json (record_wow) holds the plain scenario over invented raid rows (wow_rows) with it
on: each guild prompt gains the raid and guild card, her example lines and the glossary after
the awareness slot; every whisper prompt, the lines sent and everything else are as
golden_prompts.json recorded them (tests/tuning/test_wow_golden.py proves both). Re-recorded with
geography (2026-11-20): its six whisper prompts gain the place line as golden_prompts.json's do;
the guild prompts, which carry the grounding, get no place line (a game channel)."""
import asyncio
import json
import random
from pathlib import Path

from conftest import HUMAN, TEST_CAST
from engine_rig import engine_clock, make_rig, numbered, save_member
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.protocol import Login, LootWon
from leeroy_mind.talk.romance import flirt_chance, attitude_text
from leeroy_mind.talk.select import SpeakerContext, speaker_weight
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.states import roll_session_state
from leeroy_mind.talk import beat
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

GOLDEN = Path(__file__).with_name("golden_prompts.json")
BEAT_GOLDEN = Path(__file__).with_name("golden_beat.json")
VOICE_GOLDEN = Path(__file__).with_name("golden_voice.json")
VOICED = (102, 103)  # the scenario's members with a voice sheet in record_voice
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


async def _run(store, clock, data_dir, core: bool = False, *, realism: bool = False,
               heat=None, wow: bool = False) -> dict:
    r = make_rig(store, clock, data_dir, script=numbered("ok so line {n} then"), seed=7,
                 availability=6 if core else 0, life_state=core, intent=core, situation=core,
                 shape=core, romance_realism=realism, wow_grounding=wow)
    if heat is not None:  # [heat] on (tests/heat): a function that attaches it to the rig
        heat(r)
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
    if core:  # his Discord DM to 102, answered through the whole pipeline (the shape's DM lines)
        mid = dstore.post(store, channel="dm:102", author=HUMAN, origin="human",
                          text="how did the dungeon run go after i left", now=r.clock.now())
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN)
        meta = TurnMeta(r.engine._new_id(), answers_human=True, why="discord:reply",
                        discord=DiscordTarget(reply_to=mid, thread=None))
        task = asyncio.create_task(r.engine._speak(turn, meta))
        for _ in range(200):
            if task.done():
                break
            await r.advance(1)
            await asyncio.sleep(0.002)
        task.result()
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


def record(store, clock, data_dir, core: bool = False, heat=None) -> dict:
    """`clock`: engine_clock(), the store's own. `core`: the core prompt's switches on, as
    golden_core.json was recorded. `heat`: attaches [heat] to the rig (tests/heat)."""
    cast(store)
    return asyncio.run(_run(store, clock, data_dir, core, heat=heat))


def voiced_cast(store) -> None:
    """cast(), with a voice sheet on Belra (a woman drawn to him: flirt) and Coros (a man:
    warm)."""
    from dataclasses import replace

    from leeroy_mind.personas.io import load_persona, save_persona
    from leeroy_mind.wow import CLASSES, RACES
    from leeroy_mind.personas.voices import stamped
    from sheets import BRIGHT, GRUFF, sheet
    cast(store)
    for guid, data in zip(VOICED, (BRIGHT, GRUFF)):
        _, race, cls = TEST_CAST[guid]
        p = load_persona(store, guid)
        save_persona(store, replace(p, speech=stamped(sheet(data), p)), source="seed",
                     ts=1000.0, race=RACES[race], class_name=CLASSES[cls])


def record_voice(store, clock, data_dir, core: bool = False) -> dict:
    """The scenario with voice sheets on two members (golden_voice.json's "plain", or "core"
    with the core prompt's switches on): the prompts and the lines sent."""
    voiced_cast(store)
    got = asyncio.run(_run(store, clock, data_dir, core))
    return {"prompts": got["prompts"], "sent": got["sent"]}


def write_golden_voice(result: dict) -> None:
    VOICE_GOLDEN.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8",
                            newline="\n")


def golden_voice() -> dict:
    return json.loads(VOICE_GOLDEN.read_text(encoding="utf-8"))


def record_romance(store, clock, data_dir, heat=None) -> dict:
    """The scenario with [romance] realism on (golden_romance.json)."""
    cast(store)
    return asyncio.run(_run(store, clock, data_dir, realism=True, heat=heat))


def write_golden_romance(result: dict) -> None:
    ROMANCE_GOLDEN.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n",
                              encoding="utf-8", newline="\n")


def golden_romance() -> dict:
    return json.loads(ROMANCE_GOLDEN.read_text(encoding="utf-8"))


WOW_GOLDEN = Path(__file__).with_name("golden_wow.json")
# The scenario's members' guild lines from before the switch, two days before it starts (the
# grounding's example lines), and the guild's slang
WOW_EXAMPLES = {101: ("pulling the next pack in five, stay behind me",
                      "who has the soulstone tonight anyway"),
                102: ("drink up before the boss please, i need mana",
                      "healers ready, pull when you want"),
                103: ("my dps was way better on that last pull",),
                104: ("anyone selling frost lotus for the flasks",
                      "brez is on cooldown so dont die on me"),
                105: ("bubble hearth if it goes wrong again lol",)}


def wow_rows(store, clock) -> None:
    """What the WoW golden's grounding reads, invented: a Naxxramas 25 that ended three hours
    before the scenario (Kel'Thuzad killed after a wipe to Frost Blast), Belra's loot from it,
    everyone's DKP and attendance, her example lines and one slang term."""
    now = clock.now()
    with store.transaction():
        c = store.conn
        raid = c.execute("INSERT INTO guild_raid(started,ended,raid_key,size,heroic,offline,"
                         "lead_guid,tank_guid,loot_guid,kills,wipes) VALUES (?,?,'naxx',25,0,0,"
                         "104,101,104,13,1)", (now - 6 * 3600, now - 3 * 3600)).lastrowid
        for guid in TEST_CAST:
            c.execute("INSERT INTO guild_attendance(raid_id,guid,status) VALUES (?,?,?)",
                      (raid, guid, "benched" if guid == 105 else "raided"))
            c.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id) VALUES (?,?,?,'kill',?)",
                      (now - 3 * 3600, guid, 20 + guid % 100 * 5, raid))
        for ts, state in ((now - 4 * 3600, "wiped"), (now - 3.5 * 3600, "killed")):
            c.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,'boss',0,?)",
                      (ts, json.dumps({"map": 533, "instance_id": 9, "boss_id": 14, "boss_name": "",
                                       "state": state, "members": list(TEST_CAST)})))
        c.execute("INSERT INTO tg_pull(id,context,map,instance_id,boss_id,difficulty,size,outcome,"
                  "start_ms,end_ms,members,assisted,boss_entry,upd_mean_ms,kind,complete,scoreable,"
                  "received,raid_id) VALUES ('wow-1','533:14:1',533,9,14,1,25,'wiped',?,?,25,0,0,"
                  "0,'his',1,1,?,?)",
                  (int((now - 4 * 3600 - 300) * 1000), int(now - 4 * 3600) * 1000, now, raid))
        for guid in (101, 103, 104):
            c.execute("INSERT INTO tg_death(pull,guid,at_ms,spell,source,x,y,hits) "
                      "VALUES ('wow-1',?,0,27808,0,0,0,'[]')", (guid,))
        c.execute("INSERT INTO meta(key,value) VALUES ('tg:spell_names',?)",
                  (json.dumps({"27808": "Frost Blast"}),))
        c.execute("INSERT INTO meta(key,value) VALUES (?,?)", (f"guild:loot:{raid}:wow", json.dumps(
            {"name": "Ring of Invention", "award": {"guid": 102, "paid": 35}})))
        first = None
        for guid, lines in WOW_EXAMPLES.items():
            for i, text in enumerate(lines):
                row = c.execute("INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,"
                                "origin) VALUES (?,?,?,?,0,?,'mind')",
                                (now - 2 * 86400 + guid * 60 + i, "raid" if i else "guild", guid,
                                 TEST_CAST[guid][0], text)).lastrowid
                first = first or row
        c.execute("INSERT INTO lexicon(kind,term,origin_ref,originator,born,uses,last_used) "
                  "VALUES ('slang','gnomed',?,101,?,4,?)", (f"chat:{first}", now - 86400, now))


def record_wow(store, clock, data_dir, grounding: bool = True) -> dict:
    """The plain scenario over wow_rows with [talk] wow_grounding on (golden_wow.json), or off
    (which must record golden_prompts.json: the rows alone change no prompt)."""
    cast(store)
    wow_rows(store, clock)
    got = asyncio.run(_run(store, clock, data_dir, wow=grounding))
    return {"prompts": got["prompts"], "sent": got["sent"]}


def write_golden_wow(result: dict) -> None:
    WOW_GOLDEN.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8",
                          newline="\n")


def golden_wow() -> dict:
    return json.loads(WOW_GOLDEN.read_text(encoding="utf-8"))


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


__all__ = ["TEST_CAST", "VOICED", "cast", "engine_clock", "golden", "golden_beat", "golden_core",
           "golden_romance", "golden_voice", "record", "record_beat", "record_romance",
           "record_voice", "voiced_cast", "write_golden", "write_golden_beat",
           "write_golden_core", "write_golden_romance", "write_golden_voice", "golden_wow",
           "record_wow", "wow_rows", "write_golden_wow"]
