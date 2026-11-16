import random
from datetime import datetime
from conftest import make_persona
from leeroy_mind.life.presence import (NO_PHOTO, Attention, attention_weight, away_words,
                                       discord_state, free_after, game_cap_s, latency,
                                       presence_at)

def test_attention_by_medium():
    p = make_persona(101, "Arkon", rhythm="office admin, nine till five")
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
    p = make_persona(101, "Arkon", rhythm="office admin, nine till five", household="three kids at home")
    lp = presence_at(p, datetime(2026, 11, 19, 18, 30), medium="discord")
    assert discord_state(lp, dnd_day=False) == "online" and away_words(lp, "discord") == "with the kids"

def test_free_after_is_the_start_of_her_next_block_she_can_take_a_photo_in():
    """Asleep and driving are the blocks a requested photo waits out (photos/requests.py)."""
    p = make_persona(101, "Arkon", rhythm="office admin, nine till five")
    assert free_after(p, datetime(2026, 11, 19, 3, 0)) == datetime(2026, 11, 19, 7, 30)
    assert free_after(p, datetime(2026, 11, 19, 8, 30)) == datetime(2026, 11, 19, 9, 0)
    assert free_after(p, datetime(2026, 11, 19, 17, 10)) == datetime(2026, 11, 19, 17, 45)
    assert free_after(p, datetime(2026, 11, 19, 12, 0)) is None  # at work she can sneak one
    early = make_persona(101, "Arkon")  # early shifts: in bed from 21:15 before a workday
    assert free_after(early, datetime(2026, 11, 19, 22, 0)) == datetime(2026, 11, 20, 4, 45)
    work_too = NO_PHOTO | {"work"}
    assert free_after(p, datetime(2026, 11, 19, 12, 0), busy=work_too) == datetime(2026, 11, 19, 17, 45)
