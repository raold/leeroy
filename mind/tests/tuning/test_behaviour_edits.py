"""D6 Part A task 2: hangouts, skill and day-plan inputs as godmode overlays."""
import random
from dataclasses import replace
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from conftest import make_persona, seed_test_cast
from leeroy_mind.discord.presence import Presence
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import PersonaCache, load_persona
from leeroy_mind.playstyle import style
from leeroy_mind.server.rhythm import choose_cast, choose_seed_and_channel
from leeroy_mind.server.seats import away_seats
from leeroy_mind.server.seeds import Seed
from leeroy_mind.server.state import DiscordState
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning import edits as E
from leeroy_mind.tuning.effective import edits_of, with_edits

HUMAN = HumanIdentity(1, "Testmage", "Zan", "Quillon", True)
NOW = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc).timestamp()


def overlay(p, **kw):
    e = E.MemberEdits(**{k: MappingProxyType(v) if isinstance(v, dict) else v for k, v in kw.items()})
    return with_edits(p, e)


def state():
    return DiscordState(Presence("online", False, 0), None, None, None, None, 0, ())


def cast_share(personas, channel, n=500):
    rng = random.Random(7)
    seed = Seed("purpose:x", "purpose", (channel,), "topic")
    states = {g: state() for g in personas}
    hits = 0
    for _ in range(n):
        cast = choose_cast(channel, seed, states, personas, officers=frozenset(), human=1,
                           in_play=frozenset(), ties=lambda a, b: 1, rng=rng)
        hits += 103 in cast
    return hits / n


def test_hangout_weights_move_her_channel_share():
    personas = {g: make_persona(g, f"Test{g}") for g in range(101, 106)}
    base_irl, base_general = cast_share(personas, "irl"), cast_share(personas, "general")
    personas[103] = overlay(personas[103], channels={"irl": 3, "general": 0})
    assert cast_share(personas, "irl") > base_irl + 0.1
    assert cast_share(personas, "general") == 0 < base_general


def test_hangout_weights_move_her_opening_channel():
    seed = Seed("life:1", "life", ("irl", "general", "food"), "fact", (103,))
    local = datetime(2026, 11, 12, 14)

    def share(hangouts):
        rng = random.Random(3)
        return sum(choose_seed_and_channel([seed], local=local, officers_free=0, rng=rng,
                                           hangouts=hangouts)[1] == "food" for _ in range(500)) / 500
    plain = share(None)
    p = overlay(make_persona(103, "Test103"), channels={"food": 3, "general": 0})
    assert share({103: edits_of(p)}) > plain + 0.15
    rng = random.Random(4)
    assert all(choose_seed_and_channel([seed], local=local, officers_free=0, rng=rng,
                                       hangouts={103: edits_of(p)})[1] != "general"
               for _ in range(200))


def test_no_hangouts_leave_every_draw_as_it_was():
    seed = Seed("life:1", "life", ("irl", "general"), "fact", (103,))
    local = datetime(2026, 11, 12, 14)
    a = [choose_seed_and_channel([seed], local=local, officers_free=0, rng=random.Random(9))
         for _ in range(50)]
    b = [choose_seed_and_channel([seed], local=local, officers_free=0, rng=random.Random(9),
                                 hangouts=None) for _ in range(50)]
    assert a == b


def test_away_seats_prefer_her_rooms(monkeypatch):
    monkeypatch.setattr("leeroy_mind.server.seats.VOICE_SHARE", 1)
    p = make_persona(101, "Test101")
    p = replace(p, game=replace(p.game, thinks_as="lore"))
    presences = {101: Presence("online", False, 0)}
    evening = datetime(2026, 11, 12, 19)
    assert away_seats({101: p}, presences, evening) == {"lore": (101,)}
    assert away_seats({101: overlay(p, rooms={"kitchen": 3})}, presences, evening) == {"kitchen": (101,)}
    assert away_seats({101: overlay(p, rooms={"lore": 0})}, presences, evening) == {}


@pytest.mark.parametrize("role", ["melee", "tank"])
def test_skill_overlay_changes_style_within_its_bounds(role):
    p = make_persona(101, "Test101", raid_role=role, conscientiousness={"score": 4,
                                                                         "behaviour": "loose"})
    plain = style(p)
    assert not plain.potions
    star = style(overlay(p, skill="star"))
    assert star.potions and star.wait == plain.wait
    fire = style(overlay(p, skill="fire"))
    assert not fire.potions and fire.wait == (6 if role == "melee" else 0)
    middle = style(overlay(p, skill="middle"))
    assert middle == plain
    for s in (star, fire, middle):
        assert s.wait in (0, 2, 3, 4, 5, 6)


def test_activities_reach_the_planner_inputs(store):
    seed_test_cast(store)
    store.set_meta("human_guid", "1")
    store.conn.commit()
    pic = C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})
    r = E.set_edit(store, {"kind": "activities", "args": {"member": 101,
                   "activities": ["fishing", "inn"], "with": [103]}}, human=HUMAN,
                   source="console", now=NOW, pic=pic)
    p = PersonaCache(store).get(101)
    assert edits_of(p).activities == ("fishing", "inn") and edits_of(p).partners == (103,)
    with pytest.raises(E.EditRefused):
        E.set_edit(store, {"kind": "activities", "args": {"member": 101, "with": [1]}},
                   human=HUMAN, source="console", now=NOW, pic=pic)
    C.undo(store, r["change"], pic, source="console", now=NOW + 1)
    assert edits_of(PersonaCache(store).get(101)).activities == ()


def test_hangout_edits_check_channels_and_rooms(store):
    seed_test_cast(store)
    store.set_meta("human_guid", "1")
    store.conn.commit()
    pic = C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})
    for a, code in ((dict(channels={"rules": 2}), "channel"), (dict(rooms={"nowhere": 1}), "room")):
        with pytest.raises(E.EditRefused) as e:
            E.set_edit(store, {"kind": "hangouts", "args": {"member": 101, **a}}, human=HUMAN,
                       source="console", now=NOW, pic=pic)
        assert e.value.code == code
    E.set_edit(store, {"kind": "hangouts", "args": {"member": 101, "channels": {"irl": 2},
                                                    "rooms": {"kitchen": 3}}},
               human=HUMAN, source="console", now=NOW, pic=pic)
    e = edits_of(PersonaCache(store).get(101))
    assert e.channel_weight("irl") == 2 and e.channel_weight("general") == 1
    assert e.room_weight("kitchen") == 3
