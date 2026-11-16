from datetime import date
from conftest import make_persona
from leeroy_mind.life.day import DayEvent, day_plan
from leeroy_mind.life.state import Impulse, State, apply, baseline, decayed, tone, words
Z = State(0, 0, 0, 0, 0)
HE = ("he", "him", "his")

def test_decay_half_life():
    s = decayed(State(0.4, 0.4, 0.4, 0.4, 0.4), Z, 3 * 3600)
    assert (round(s.valence, 3), round(s.toward_him, 3)) == (0.2, round(0.4 * 2 ** -0.5, 3))

def test_impulses_clamp():
    s = Z
    for _ in range(12):
        s = apply(s, Impulse.HIS_HARSH)
    assert s.toward_him == -1.0 and s.stress == 1.0

def test_tone():
    assert tone("thanks love, you're the best") == "warm"
    assert tone("are you kidding me, that was so stupid") == "harsh"
    assert tone("heading to dalaran") == "plain"

def test_words_and_privacy():
    tired = State(0.0, -0.5, 0.35, -0.3, 0.5)
    assert words(tired, to_him=False, forms=HE) == "You're tired and a bit short today."
    assert words(tired, to_him=True, forms=HE) == "You're happy with him right now, tired and a bit short today."
    assert words(Z, to_him=True, forms=HE) == ""

def test_lands_from_state_and_tone():
    from leeroy_mind.life.state import lands
    assert lands(Z, "harsh", agreeableness=8, forms=HE) == "it stings"
    assert lands(State(0, 0, 0, 0, -0.4), "warm", agreeableness=5, forms=HE) == \
        "nice, but you're still a bit sore with him"
    assert lands(State(0, -0.5, 0, 0, 0), "plain", agreeableness=5, forms=HE) == "you're too tired to say much"
    assert lands(Z, "plain", agreeableness=5, forms=HE) == ""


THU, SAT = date(2026, 11, 19), date(2026, 11, 21)

def _base(p, day, hh, mm, event=DayEvent.NONE, mood=0.1, arousal=0.2, toward_seed=0.0):
    return baseline(p, mood=mood, arousal=arousal, plan=day_plan(p, day, event=event),
                    minute=hh * 60 + mm, toward_seed=toward_seed)

def test_baseline_follows_her_day():
    p = make_persona(101, "Arkon")   # early shifts: up 04:45, work 06:00-14:30, bed 21:15
    r = lambda s: tuple(round(x, 3) for x in (s.valence, s.energy, s.stress, s.social, s.toward_him))
    calm, chatty = round(-3.5 / 9, 3), round(1.5 / 9, 3)
    assert r(_base(p, THU, 5, 0)) == (0.1, -0.3, calm, chatty, 0.0)               # just up
    assert r(_base(p, THU, 10, 0)) == (0.1, 0.1, round(-3.5 / 9 + 0.15, 3), chatty, 0.0)  # at work
    assert r(_base(p, THU, 14, 45))[1] == -0.05                                     # off shift
    assert r(_base(p, THU, 20, 0))[1] == -0.2                                       # near bed
    assert r(_base(p, THU, 12, 0, mood=0.4, arousal=-0.2, toward_seed=0.25)) == \
        (0.4, -0.1, round(-3.5 / 9 + 0.15, 3), chatty, 0.25)
    assert r(_base(p, SAT, 9, 0))[1] == -0.3                                        # a lie-in
    assert r(_base(p, SAT, 13, 0))[1] == 0.1

def test_baseline_day_events_and_clamp():
    p = make_persona(101, "Arkon", household="two kids at home",
                     neuroticism={"score": 9, "behaviour": "frets over small slips"})
    bad = _base(p, THU, 12, 0, event=DayEvent.BAD_DAY)
    assert round(bad.valence, 3) == -0.15 and round(bad.stress, 3) == round(3.5 / 9 + 0.15 + 0.2, 3)
    sick = _base(p, THU, 12, 0, event=DayEvent.SICK_KID)
    assert round(sick.valence, 3) == 0.0 and round(sick.stress, 3) == round(3.5 / 9 + 0.25, 3)
    out = _base(p, SAT, 20, 0, event=DayEvent.NIGHT_OUT)
    assert round(out.valence, 3) == 0.2
    low = _base(p, THU, 12, 0, mood=-0.95, event=DayEvent.BAD_DAY, toward_seed=-2.0)
    assert low.valence == -1.0 and low.toward_him == -1.0

def test_tone_reads_phone_apostrophes_and_case():
    assert tone("You\u2019re AMAZING") == "warm"
    assert tone("ok shut up now") == "harsh"
    assert tone("gj on that pull") == "warm"
    assert tone("why is the boat late") == "plain"

def test_words_keep_her_feeling_about_him_off_other_turns():
    sore = State(0.5, 0.0, 0.0, 0.5, -0.6)
    assert words(sore, to_him=False, forms=HE) == "You're in a great mood and chatty today."
    assert words(sore, to_him=True, forms=HE) == \
        "You're a bit annoyed with him right now, in a great mood and chatty today."
    assert words(State(0, 0, 0, 0, 0.6), to_him=True, forms=HE) == "You're happy with him right now."
    assert (words(State(0.5, 0, 0, -0.5, 0), to_him=False, forms=HE)
            == "You're in a great mood and not feeling chatty today.")
    assert words(State(0, 0, 0, 0, 0.6), to_him=True, forms=("she", "her", "her")) == \
        "You're happy with her right now."
