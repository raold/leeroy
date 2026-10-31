import asyncio
from datetime import datetime

from conftest import run
from leeroy_mind.clock import night_of
from leeroy_mind.text import (estimate_tokens, has_meta, normalize_words, strip_emoji,
                              strip_name_prefix, strip_wow_markup, word_jaccard)


def test_night_of_rolls_over_at_6am():
    assert night_of(datetime(2026, 10, 30, 2, 30)) == "2026-10-29"
    assert night_of(datetime(2026, 10, 30, 7, 0)) == "2026-10-30"


def test_fake_clock_wakes_sleepers_in_order(clock):
    woke = []
    async def s(n): await clock.sleep(n); woke.append(n)
    async def go():
        ts = [asyncio.create_task(s(n)) for n in (9, 2, 5)]
        await clock.advance(6); assert woke == [2, 5]
        await clock.advance(4); assert woke == [2, 5, 9]
    run(go())


def test_fake_clock_yields_mode_settles_without_real_time():
    from datetime import timezone
    from leeroy_mind.testing.fake_clock import FakeClock
    clock = FakeClock(datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc), yields=64)
    done = []

    async def chain():  # a task whose work after waking takes several loop turns
        await clock.sleep(3)
        for _ in range(20):
            await asyncio.sleep(0)
        done.append(clock.now())

    async def go():
        task = asyncio.create_task(chain())
        await asyncio.sleep(0)  # it starts sleeping at the start time
        await clock.advance(5, step=1)
        assert done == [clock.now() - 2] and task.done()
    run(go())


def test_strip_wow_markup():
    assert strip_wow_markup("need |cffa335ee|Hitem:50415:0:0:0:0:0:0:0:80|h[Bryntroll, the Bone Arbiter]|h|r ?") \
        == "need [Bryntroll, the Bone Arbiter] ?"


def test_estimate_tokens_and_meta():
    assert estimate_tokens("") == 0 and estimate_tokens("a" * 400) == 100
    assert has_meta("as an AI i can't") and has_meta("my programming says") and not has_meta("nice pull")
    assert not has_meta("the boss glitched") and not has_meta("good prompt reply")


def test_strip_wow_markup_plain_links_and_stray_codes():
    assert strip_wow_markup("ty |Hplayer:Arkon|h[Arkon]|h!") == "ty [Arkon]!"
    assert strip_wow_markup("|cff71d5ff|Hspell:53|h[Backstab]|h|r is nice") == "[Backstab] is nice"
    assert strip_wow_markup("|Hquest:1:80|h[A Quest]|h done |cffff0000red|r") == "[A Quest] done red"
    assert strip_wow_markup("broken |Hitem:5|hname|h end |r") == "broken name end "


def test_normalize_words_and_jaccard():
    assert normalize_words("Lol SAME... bad  pull!") == "lol same bad pull" == normalize_words(
        "lol same bad pull")
    assert normalize_words("") == "" and word_jaccard("", "") == 0.0
    assert word_jaccard("a b c d", "a b c d e") == 0.8
    assert word_jaccard("Bad pull", "bad PULL!") == 1.0 and word_jaccard("a", "b") == 0.0


def test_strip_emoji_and_name_prefix():
    assert strip_emoji("gg \U0001F600\U0001F525 ty \U0001F44D\U0001F3FD \u2764\uFE0F") == "gg  ty  "
    assert strip_emoji("ok ;) :P <3 — fine") == "ok ;) :P <3 — fine"
    assert strip_name_prefix("Arkon: lol", "Arkon") == "lol"
    assert strip_name_prefix("[Guild] [Belra]: gz", "Belra") == "gz"
    assert strip_name_prefix("Belra (whisper to Zan): hey", "belra") == "hey"
    # only the speaker's own tag goes: a member's name and a colon is an address
    assert strip_name_prefix("Belra: you're up", "Arkon") == "Belra: you're up"
    assert strip_name_prefix("Arkon: lol", "") == "Arkon: lol"
    for keep in ("lol: same", "LOL: same", "D: no", "need 2 more: tank and heals", "gz Arkon: nice"):
        assert strip_name_prefix(keep, "Arkon") == keep


def test_strip_wow_markup_textures_and_escaped_pipes():
    assert strip_wow_markup("|TInterface\\Icons\\Foo:16|t nice") == " nice"
    assert strip_wow_markup("a || b ||cff not a code") == "a | b |cff not a code"
