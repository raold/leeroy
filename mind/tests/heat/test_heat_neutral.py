"""Ruling 1 of the review of #373: `[heat] enabled = true` with no heat value stored is exactly
today, bit for bit, at every world romance dial (the live one is 10, not 9) and for every dial a
woman has of her own. Heat governs nobody until the owner sets a world heat.

Compared through the engine, heat attached and unset against no heat at all: the full prompt of
turns to him over every stance, arc stage, channel, own dial and world dial 1-10 (the romance
dial's whole range) for women of low, middling and high extraversion; the clocks (the flirt
conversion's rate, the whisper planner's draws and gaps); the photo side (the offer rate, the
tier, what may be sent); and the dials the older readers take. Stored member and group offsets
change none of it while the world value is unset."""
import asyncio
import itertools
import random
from dataclasses import replace

import pytest

from conftest import HUMAN, make_persona, seed_test_cast
from engine_rig import make_rig, save_member
from heat.rig import ON, attach, set_values
from leeroy_mind.heat import live as heat_live
from leeroy_mind.heat.table import P4
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.storage.rows import ROMANCE_STAGES, Relationship
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.romance import WhisperPlanner, flirt_chance, romance_lift
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.tuning.world import merge

GUID = 104
CHANNELS = ("whisper", f"dm:{GUID}", "guild", "officer", "party", "raid", "say", "voice:1",
            "discord:general")
STANCES = ("fond", "drawn", "lust")
OWN = (None, 4, 10)
EXTRAVERSION = (3, 5, 8)  # a lust woman reads "quiet", "open" and "bold": only her own 10 lifts it
HEATED = "can't stop thinking about you"


def _persona(stance, own, ext):
    return make_persona(GUID, "Dunna", player_gender="female", stance=stance, dial=own,
                        extraversion={"score": ext, "behaviour": "chats when she feels like it"})


def _turns(channel):
    private = channel in ("whisper", f"dm:{GUID}")
    out = [Turn(kind=TurnKind.REPLY_HUMAN, speaker=GUID, channel=channel, addressee=HUMAN,
                to_guid=HUMAN if private else 0)]
    if channel == "whisper":
        out.append(Turn(kind=TurnKind.WHISPER, speaker=GUID, channel=channel, addressee=HUMAN,
                        to_guid=HUMAN))
    if channel == "guild":
        out.append(Turn(kind=TurnKind.FLIRT, speaker=GUID, channel=channel, addressee=HUMAN))
        out.append(Turn(kind=TurnKind.GREET, speaker=GUID, channel=channel, addressee=HUMAN))
    return out


async def _prompt(engine, turn, p, seed):
    answers = turn.kind is TurnKind.REPLY_HUMAN
    row = ChatLineRow(id=0, ts=engine.clock.now(), channel=turn.channel, from_guid=HUMAN,
                      from_name=engine.human.character_name, to_guid=turn.to_guid, text=HEATED,
                      map=0, zone=0, session_id=None, thread_id=None,
                      origin="human") if answers else None
    c = await engine._compose(turn, TurnMeta(turn_id="preview", seed=seed), live=False,
                              persona=p, window=[row] if row else [],
                              trigger_text=HEATED if answers else None)
    return [(m["role"], m["content"]) for m in c.messages], c.tail


def _rig(store, clock, tmp_path):
    seed_test_cast(store)
    save_member(store, GUID, player_gender="female", stance="lust")
    r = make_rig(store, clock, tmp_path)
    live = attach(r.engine, store, clock, tmp_path, ON, world=None)  # on, and not set
    return r, live


@pytest.mark.parametrize("stance", STANCES)
def test_on_and_unset_every_prompt_is_the_prompt_without_heat(store, clock, tmp_path, stance):
    r, live = _rig(store, clock, tmp_path)
    engine, base = r.engine, r.engine.cfg
    # offsets he stored before setting a world value: they wait, and change nothing
    set_values(store, engine, [("member", str(GUID), -5), ("group", "everyone", -3)])
    assert engine.personas.tuning.heat_world is None

    async def go():
        compared = 0
        for own, ext, stage in itertools.product(OWN, EXTRAVERSION, ROMANCE_STAGES):
            p = _persona(stance, own, ext)
            store.put_relationship(Relationship(a=GUID, b=HUMAN, affinity=40.0, trust=40.0,
                                                attraction=80.0, familiarity=50.0,
                                                romance_stage=stage))
            for dial, channel in itertools.product(range(1, 11), CHANNELS):
                engine.cfg = replace(base, romance=replace(base.romance, dial=dial))
                for n, turn in enumerate(_turns(channel)):
                    seed = dial * 100 + n
                    engine.heat = live
                    assert live.turn(p, channel=channel) is None
                    on = await _prompt(engine, turn, p, seed)
                    engine.heat = None
                    off = await _prompt(engine, turn, p, seed)
                    assert on == off, (own, ext, stage, dial, channel, turn.kind)
                    compared += 1
        return compared
    done = asyncio.run(go())
    assert done == len(OWN) * len(EXTRAVERSION) * len(ROMANCE_STAGES) * 10 * (len(CHANNELS) + 3)


def test_on_and_unset_the_clocks_the_photo_side_and_the_older_dials_are_todays(
        store, clock, tmp_path):
    r, live = _rig(store, clock, tmp_path)
    engine, base = r.engine, r.engine.cfg
    set_values(store, engine, [("member", str(GUID), 5), ("group", "everyone", 2)])
    swim = dict(style="lust", outfit="a red halter bikini", setting="the beach at sunset")
    women = [_persona(stance, own, ext)
             for stance, own, ext in itertools.product(STANCES, OWN, EXTRAVERSION)]
    for dial in range(1, 11):
        engine.cfg = replace(base, romance=replace(base.romance, dial=dial))
        assert merge(engine.cfg, engine.personas.tuning) is engine.cfg  # no derived dial
        for p in women:
            # the flirt conversion and the starts: no multiplier, never "casual only"
            assert (live.starts(p), live.never_starts(p), live.offers(p)) == (1.0, False, 1.0)
            assert flirt_chance(p, 1.3) * live.starts(p) == flirt_chance(p, 1.3)
            # photos: the lists as they are, everything may be sent, offered as before
            assert (live.photo_tier(p), heat_live.photo_tier(p)) == (P4, P4)
            assert heat_live.sends(p, swim) and heat_live.sends_tier(p, P4)
            assert heat_live.offers_of(p) == 1.0
            # learn, outreach and the bond read the dial they always read
            for legacy in range(0, 11):
                assert heat_live.member_dial(p, legacy) == legacy
        # the whisper clock: the same whisperer and the same next gap, draw for draw
        cands = {100 + i: (replace(p, guid=100 + i), Relationship(a=100 + i, b=HUMAN,
                                                                 affinity=10.0 * (i % 5)))
                 for i, p in enumerate(women)}
        for seed in range(25):
            with_heat = WhisperPlanner(random.Random(seed), per_hour=4.0)
            without = WhisperPlanner(random.Random(seed), per_hour=4.0)
            now = clock.now()
            picks = [(with_heat.start(now + 700 * k, cands, lift=romance_lift(dial),
                                      starts=live.starts),
                      without.start(now + 700 * k, cands, lift=romance_lift(dial)))
                     for k in range(4)]
            assert all(a == b for a, b in picks) and with_heat.next_at == without.next_at


def test_the_everyone_button_is_the_one_thing_that_reaches_her_while_unset(store, clock,
                                                                             tmp_path):
    """His own sentence or button for the evening is an act of his, not a stored setting: it
    reaches the member on a world value derived from the romance dial, and ends as it always
    does. Nobody else is touched."""
    r, live = _rig(store, clock, tmp_path)
    p, other = _persona("lust", None, 5), replace(_persona("lust", None, 5), guid=102)
    assert live.turn(p) is None
    live.sessions.set(GUID, "clean", medium="discord")
    got = live.turn(p)
    assert got is not None and (got.heat, got.tier, got.session) == (2, "warm", "clean")
    assert live.turn(other) is None and live.photo_tier(other) == P4
    live.sessions.clear(GUID)
    assert live.turn(p) is None


def test_once_the_world_is_set_heat_takes_over(store, clock, tmp_path):
    r, live = _rig(store, clock, tmp_path)
    p = _persona("lust", 10, 5)
    assert live.turn(p) is None
    set_values(store, r.engine, [("world", "", 0)])
    got = live.turn(p)
    assert (got.heat, got.tier) == (1, "warm")  # her own 10 is +1 on the world's 0
    assert live.member_dial(_persona("lust", None, 5), 10) == 0  # heat 0: romance left out
    assert live.member_dial(make_persona(101, "Arkon"), 7) == 7  # a man: untouched
