"""Independent regressions for history provenance and retelling checkpoints."""
from conftest import run
from leeroy_mind.nightly import stories
from leeroy_mind.nightly.store import insert_story
from nightly.conftest import yes


def moment(h, ancestor):
    key = "boss:631:11:2026-10-29"
    for guid in (101, 102, 103):
        h.mem(guid, "We wiped 6 times on Sindragosa, then killed it.",
              importance=7, key=key, actors=(101, 102, 103),
              evidence=(f"memory:{ancestor}",), boss_id=11, raid_id=631)
    return key


def test_history_checks_private_ancestry_beyond_three_levels(h):
    line = h.say(h.T, h.human, "That pull was private.", channel="whisper", to=101)
    ancestor = h.mem(101, "That pull was private.", evidence=(f"chat:{line}",),
                     scope="private", scope_with=h.human)
    for _ in range(5):
        ancestor = h.mem(101, "The pull.", evidence=(f"memory:{ancestor}",))
    moment(h, ancestor)
    assert stories.find_moments(h.store, h.ctx().window) == []
    assert h.count("story") == 0 and h.rows(0) == []


def test_story_rechecks_an_ancestor_changed_during_generation(h):
    event = h.boss(h.T, 631, 11, "killed", (101, 102, 103))
    ancestor = h.mem(101, "We killed Sindragosa.", evidence=(f"event:{event}",),
                     actors=(101, 102, 103))
    moment(h, ancestor)
    found, = stories.find_moments(h.store, h.ctx().window)

    def changed(_):
        h.store.conn.execute("UPDATE memory SET scope=?,scope_with=? WHERE id=?",
                             ("private", h.human, ancestor))
        return {"title": "The hard night", "summary": "We finally killed Sindragosa.",
                "first_person": "I remember that hard night.", "evidence": [1]}

    ctx = h.ctx({"story": [changed], "judge": [yes]})
    run(stories.write_story(ctx, found))
    assert h.count("story") == 0 and h.rows(0) == []
    assert not ctx.done("stories", f"story:{found.key}")


def test_retell_receipts_survive_overlap_and_source_memory_removal(h):
    sid = insert_story(h.store, title="The hard night", summary="We remember it.",
                       participants=(101, 102), evidence=(), ts=h.T)
    first = h.mem(101, "I remember it.", evidence=(f"story:{sid}",), kind="story")
    second = h.mem(102, "I was there.", evidence=(f"story:{sid}",), kind="story")
    h.utter(h.T + 1, 101, "That was a night.", (first, first))
    h.utter(h.T + 2, 102, "We still talk about it.", (second,))
    h.utter(h.T + 3, 102, "Unsent.", (second,), dropped="synthetic")
    assert stories.count_retells(h.store, (h.T, h.T + 4)) == 2
    assert stories.count_retells(h.store, (h.T + 1, h.T + 5)) == 0
    h.store.conn.execute("DELETE FROM memory WHERE id=?", (first,))
    h.utter(h.T + 6, 102, "Another retelling.", (second, second))
    assert stories.count_retells(h.store, (h.T, h.T + 7)) == 1
    assert h.store.conn.execute("SELECT retold_n FROM story WHERE id=?", (sid,)).fetchone()[0] == 3
