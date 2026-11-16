import random
from datetime import datetime
from conftest import make_persona
from leeroy_mind.life.presence import (Attention, attention_weight, away_words, discord_state,
                                       game_cap_s, latency, presence_at)

def test_attention_by_medium():
    p = make_persona(101, "Arkon", rhythm="9-5 office")
    work = datetime(2026, 11, 19, 12, 0)
    assert presence_at(p, work, medium="game").attention is Attention.GLANCING
    assert presence_at(p, work, medium="game", in_his_group=True).attention is Attention.FULL
    assert presence_at(p, datetime(2026, 11, 19, 3, 0), medium="discord").attention is Attention.NONE

def test_weights_scale_with_availability():
    assert attention_weight(Attention.NONE, 0) == 1.0
    assert attention_weight(Attention.NONE, 6) == 0.05
    assert attention_weight(Attention.FULL, 10) == 1.0
    assert attention_weight(Attention.GLANCING, 3) == 0.2 ** 0.5

def test_latency_heavy_tailed_and_capped():
    rng = random.Random(1)
    full = sorted(latency(Attention.FULL, 6, rng, medium="game") for _ in range(2000))
    glance = sorted(latency(Attention.GLANCING, 6, rng, medium="game") for _ in range(2000))
    assert 2.0 < full[1000] < 4.5 and 100 < glance[1000] < 220
    assert max(glance) <= game_cap_s(6) == 384
    assert max(latency(Attention.NONE, 6, rng, medium="discord") for _ in range(500)) <= 2700

def test_discord_state_and_words():
    p = make_persona(101, "Arkon", rhythm="9-5 office", household="two kids")
    lp = presence_at(p, datetime(2026, 11, 19, 18, 30), medium="discord")
    assert discord_state(lp, dnd_day=False) == "online" and away_words(lp, "discord") == "with the kids"
