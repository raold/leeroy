from datetime import datetime
import math
import random

from conftest import make_persona
from leeroy_mind.discord.presence import Presence
from leeroy_mind.server.rhythm import choose_cast, choose_seed_and_channel, length, next_gap, starts_per_hour
from leeroy_mind.server.seeds import Seed
from leeroy_mind.server.state import DiscordState
from leeroy_mind.tuning.effective import effective, MemberTuning


def state(presence='online'):
    return DiscordState(Presence(presence,False,0),None,None,None,None,0,())


def fixture():
    return {g:make_persona(g,f'Test{g}') for g in range(101,106)}, {g:state() for g in range(101,106)}


def rate(local,**kwargs):
    return starts_per_hour(local,**dict(dict(per_hour=2.5,chattiness=4,in_game=False,in_game_share=.5),**kwargs))


def test_default_day_lands_in_the_spec_band():
    rng=random.Random(12)
    mean=sum(length(rng) for _ in range(10000))/10000
    starts=sum(rate(datetime(2026,11,12,h)) for h in range(24) if not 3<=h<8)
    assert 300<=starts*mean<=500


def test_chattiness_zero_stops():
    assert rate(datetime(2026,11,12,19),chattiness=0)==0
    assert math.isinf(next_gap(0,random.Random(1)))


def test_in_game_halves_the_rate():
    local=datetime(2026,11,12,19)
    assert rate(local,in_game=True)==rate(local)/2


def test_gap_and_length_stay_in_their_bounds():
    rng=random.Random(2)
    assert all(300<=next_gap(2.5,rng)<=5400 for _ in range(1000))
    assert all(5<=length(rng)<=20 for _ in range(1000))


def test_officer_channel_needs_two_free_officers():
    seed=Seed('officer:1','officer',('general','officer-chat'),'Private fact',officers_only=True)
    kwargs=dict(local=datetime(2026,11,12,19),rng=random.Random(1))
    assert choose_seed_and_channel([seed],officers_free=1,**kwargs) is None
    assert choose_seed_and_channel([seed],officers_free=2,**kwargs)==(seed,'officer-chat')
    assert choose_seed_and_channel([Seed('x','purpose',('announcements',),'Info')],officers_free=2,**kwargs) is None


def draw(personas,states,*,seed=None,channel='general',rng=None,**kwargs):
    options=dict(officers=frozenset({102,104}),human=1,in_play=frozenset(),ties=lambda a,b:1,rng=rng or random.Random(1))
    options.update(kwargs)
    return choose_cast(channel,seed or Seed('x','purpose',(channel,),'Topic'),states,personas,**options)


def test_cast_excludes_offline_in_play_and_feuds():
    personas,states=fixture()
    states[101]=state('offline')
    for n in range(100):
        cast=draw(personas,states,rng=random.Random(n),in_play=frozenset({102}),
                  ties=lambda a,b:0 if {a,b}=={103,104} else 1)
        assert 101 not in cast and 102 not in cast and not {103,104}.issubset(cast)
    assert not draw(personas,states,away=frozenset({102,103,104}))


def test_seed_member_usually_joins():
    personas,states=fixture()
    rng=random.Random(45)
    seed=Seed('life:1','life',('general',),'A fact',(101,))
    joined=sum(101 in draw(personas,states,seed=seed,rng=rng) for _ in range(200))
    assert joined>=140


def test_class_channel_favours_its_class():
    personas,states=fixture()
    rng=random.Random(12)
    starts=[draw(personas,states,channel='warrior',classes={101:1,102:11,103:8,104:2,105:9},rng=rng)[0] for _ in range(500)]
    assert starts.count(101)>200
    assert all(starts.count(101)>starts.count(g)*2 for g in range(102,106))


def test_officer_cast_never_contains_a_nonofficer():
    personas,states=fixture()
    for n in range(50):
        assert set(draw(personas,states,channel='officer-chat',rng=random.Random(n)))=={102,104}


def test_talkative_zero_members_never_speak():
    personas,states=fixture()
    personas[101]=effective(personas[101],MemberTuning({'talkative':0}))
    for n in range(30):
        assert 101 not in draw(personas,states,rng=random.Random(n))
