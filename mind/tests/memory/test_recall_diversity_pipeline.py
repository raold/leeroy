"""[talk] recall_diversity through the talk pipeline: off, recall and its prompt lines are exactly
as before (no `diversity` and no `aged` reach Recall); on, a past plan reads as past in "You
remember:", a memory her first answer in a whisper showed is not shown again in that whisper an
hour later, and a live line's recipe names the rules' version. The test cast, invented memories
and lines only."""
from dataclasses import replace

import pytest

from conftest import HUMAN, make_mem, seed_test_cast
from engine_rig import engine_clock, make_rig, numbered
from leeroy_mind.learn.recipe import RECALL_PREFIX
from leeroy_mind.memory import diversity as d
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_llm import fake_embed

DAY = 86400.0
PLAN = "Finish the quilt border tomorrow so I can show Zan at the fair."


@pytest.fixture
def clock():
    return engine_clock()


def remember(store, clock, text, *, age=0.0, **kw):
    now = int(clock.now())
    kw.setdefault("source", "extract")
    return store.insert_memory(make_mem(owner=102, text=text, actors=(HUMAN,), evidence=(),
                                        embedding=pack_vec(fake_embed(text)),
                                        t_event=int(now - age), t_created=int(now - age),
                                        t_accessed=now - 7 * DAY, **kw))


def rig_with(store, clock, tmp_path, on):
    seed_test_cast(store)
    r = make_rig(store, clock, tmp_path, script=numbered("sure, line {n}"))
    if on is not None:
        r.engine.cfg = replace(r.engine.cfg, talk=replace(r.engine.cfg.talk, recall_diversity=on))
    return r


def remembered(prompt: str) -> list[str]:
    """The "You remember:" lines of a prompt."""
    if "You remember:" not in prompt:
        return []
    block = prompt.split("You remember:", 1)[1].split("\n\n", 1)[0]
    return [x for x in block.splitlines() if x.startswith("- ")]


def whisper_prompts(r):
    return [p for p in r.prompts() if "You remember:" in p]


@pytest.mark.parametrize("on", [None, False])
def test_off_reaches_recall_exactly_as_before(store, clock, tmp_path, monkeypatch, on):
    r = rig_with(store, clock, tmp_path, on)
    remember(store, clock, PLAN, age=5 * DAY, kind="plan", source="reflection")
    seen = []
    real_recall, real_format = r.engine.recall.recall, r.engine.recall.format

    async def recall(*args, **kw):
        seen.append(("recall", sorted(kw)))
        return await real_recall(*args, **kw)

    def fmt(m, **kw):
        seen.append(("format", sorted(kw)))
        return real_format(m, **kw)
    monkeypatch.setattr(r.engine.recall, "recall", recall)
    monkeypatch.setattr(r.engine.recall, "format", fmt)
    r.whisper_from_him(102, "hey you")
    r.run()
    assert ("format", []) in seen
    assert all("diversity" not in kw and "aged" not in kw for _, kw in seen)
    lines = remembered(whisper_prompts(r)[0])
    assert lines and all(d.PAST_SOON not in x and d.PAST_LONG not in x for x in lines)
    r.close()


def test_on_a_past_plan_reads_as_past(store, clock, tmp_path):
    r = rig_with(store, clock, tmp_path, True)
    remember(store, clock, PLAN, age=5 * DAY, kind="plan", source="reflection")
    r.whisper_from_him(102, "hey you")
    r.run()
    lines = remembered(whisper_prompts(r)[0])
    assert "- five days ago: " + d.PAST_SOON + PLAN in lines
    r.close()


@pytest.mark.parametrize("on", [False, True])
def test_on_a_memory_shown_in_a_whisper_is_not_shown_again_there(store, clock, tmp_path, on):
    r = rig_with(store, clock, tmp_path, on)
    for text in ("I keep bees behind the bakery with Zan's cousin.",
                 "Zan helped me carry a cracked teapot home from the market.",
                 "The ferry timetable Zan showed me changed for the winter.",
                 "Zan and I watched the lighthouse keeper splice rope.",
                 "Zan taught me to tie a bowline at the docks.",
                 "Zan lent me a lantern for the night walk.",
                 "Zan and I argued about which pie wins at the fair."):
        remember(store, clock, text, age=3 * DAY)
    r.whisper_from_him(102, "hey you")
    r.run()
    r.run(40 * 60)  # past the half hour a used memory rests (recall.REST_S)
    r.whisper_from_him(102, "so what else is going on")
    r.run()
    first, second = (set(remembered(p)) for p in whisper_prompts(r)[:2])
    r.close()
    assert first and second
    # off: the rest is over and the same memories come back; on: not in this conversation
    assert bool(first & second) is not on


def test_on_a_live_line_records_the_recall_version_in_its_recipe(store, clock, tmp_path):
    for on in (False, True):
        r = rig_with(store, clock, tmp_path, on)
        r.engine.learn_enabled = True
        r.whisper_from_him(102, "hey you")
        r.run()
        r.close()
    ids = [row[0] for row in store.conn.execute(
        "SELECT p.template_id FROM talk_recipe p JOIN chat_line c ON c.id=p.line_id ORDER BY c.id")]
    tag = RECALL_PREFIX + d.VERSION
    assert ids and tag not in ids[0] and any(tag in x.split("+") for x in ids[1:])
