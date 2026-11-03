"""Shared stories are public, grounded, resumable, and kept in each witness's recall."""
import json
from dataclasses import replace

import pytest

from conftest import run
from nightly.conftest import yes
from leeroy_mind.nightly import stories
from leeroy_mind.nightly.llm import ReflectionError
from leeroy_mind.nightly.store import insert_story


def moment(h, n=0, *, owners=(101, 102, 103), importance=7, key=None, source="game"):
    at = h.T + n
    event = h.boss(at, 631, 11, "wiped", owners)
    ids = [h.mem(guid, "We wiped 6 times on Sindragosa, then killed it.", t=at,
                 importance=importance, key=key, boss_id=11, raid_id=631,
                 source=source, actors=tuple(owners), evidence=(f"event:{event}",))
           for guid in owners]
    return event, ids


def answer(**changes):
    return {"title": "The stubborn pull", "summary": "We wiped 6 times on Sindragosa, then killed it.",
            "first_person": "We wiped 6 times on Sindragosa, then killed it.", "evidence": [1], **changes}


def context(h, *answers, **kwargs):
    return h.ctx({"story": list(answers) or [answer()], "judge": [yes] * 20}, **kwargs)


def only_moment(h):
    m, = stories.find_moments(h.store, h.ctx().window)
    return m


def no_extras(monkeypatch):
    from leeroy_mind.nightly import chronicle

    async def skip(ctx):
        pass

    for name in ("write_recap", "write_chronicle", "stage_awards"):
        monkeypatch.setattr(chronicle, name, skip)


def test_groups_boss_keys_requires_three_owners_and_drops_whole_private_group(h):
    key = "boss:631:11:2026-10-29"
    event, ids = moment(h, key=key)
    moment(h, 1, owners=(101, 102))
    moment(h, 2, source="hearsay", importance=9)
    _, mixed = moment(h, 3, owners=(101, 102, 104), importance=9)
    h.store.conn.execute("UPDATE memory SET scope='private',scope_with=1 WHERE id=?", (mixed[-1],))
    moments = stories.find_moments(h.store, h.ctx().window)
    assert [m.key for m in moments] == [key]
    assert moments[0].owners == {101, 102, 103}
    assert moments[0].memory_ids == ids and moments[0].refs == [f"event:{event}"]


def test_window_status_source_importance_and_total_importance_order(h):
    one, _ = moment(h, 0)
    two, _ = moment(h, 1, importance=9)
    moment(h, 2, importance=6)
    moment(h, 3, source="reflection", importance=10)
    moment(h, 4, source="offscreen", importance=10)
    _, archived = moment(h, 5, importance=10)
    h.store.conn.execute("UPDATE memory SET status='archived' WHERE id=?", (archived[-1],))
    assert [m.key for m in stories.find_moments(h.store, (h.T, h.T + 10))] == [f"ev:event:{two}", f"ev:event:{one}"]
    assert stories.find_moments(h.store, (h.T + 20, h.T + 30)) == []


def test_existing_story_or_story_copy_prevents_repeating_a_moment(h):
    _, first = moment(h)
    _, second = moment(h, 1)
    ident = insert_story(h.store, title="Earlier", summary="A pull", participants=[101, 102, 103],
                         evidence=[f"memory:{first[0]}"], ts=h.T)
    h.mem(0, "Earlier story copy", evidence=(f"story:{ident}", f"memory:{second[0]}"), source="reflection")
    assert stories.find_moments(h.store, h.ctx().window) == []


def test_story_keeps_every_source_and_writes_public_participant_copies(h):
    _, ids = moment(h)
    ctx = context(h)
    run(stories.write_story(ctx, only_moment(h)))
    row = h.store.conn.execute("SELECT * FROM story").fetchone()
    assert json.loads(row["participants"]) == [101, 102, 103]
    assert h.store.get_meta(f"nightly:story-night:{row['id']}") == ctx.night
    assert {f"memory:{ident}" for ident in ids} <= set(json.loads(row["evidence"]))
    shared, = h.rows(0)
    assert shared.kind == "story" and shared.importance == 8 and len(shared.text) <= 200
    assert shared.text.startswith("The stubborn pull:") and len(shared.evidence) <= 20
    for guid in (101, 102, 103):
        copied = h.rows(guid)[-1]
        assert copied.text == answer()["first_person"] and copied.source == "reflection"
        assert copied.evidence == (f"story:{row['id']}",)
        assert copied.actors == tuple(g for g in (101, 102, 103) if g != guid)
        assert (copied.scope, copied.scope_with) == ("open", 0)
    assert h.rows(h.human) == []
    assert stories.find_moments(h.store, ctx.window) == []


@pytest.mark.parametrize("field,text", [
    ("summary", "We wiped 7 times on Sindragosa."),
    ("title", "Ebbo stole the show"),
    ("first_person", "We killed the Lich King."),
    ("summary", "Quillon watched us wipe."),
])
def test_unsupported_story_or_title_writes_nothing(h, field, text):
    moment(h)
    m = only_moment(h)
    ctx = context(h, answer(**{field: text}))
    run(stories.write_story(ctx, m))
    assert h.count("story") == 0 and h.count("memory") == 3
    assert ctx.done("stories", f"story:{m.key}")
    assert h.store.conn.execute("SELECT 1 FROM grounding_log WHERE verdict='rejected'").fetchone()


def test_unknown_alias_does_not_fall_back_to_memory_id(h):
    moment(h)
    ctx = context(h, answer(evidence=[999999]))
    run(stories.write_story(ctx, only_moment(h)))
    assert h.count("story") == 0 and h.count("memory") == 3


def test_actor_markup_is_normalized_before_grounding(h):
    moment(h)
    ctx = context(h, answer(summary="|cffffffffEbbo|r won the argument."))
    run(stories.write_story(ctx, only_moment(h)))
    assert h.count("story") == 0
    assert h.store.conn.execute("SELECT 1 FROM grounding_log WHERE reason LIKE 'name not in evidence:%'").fetchone()


def test_story_privacy_change_in_uncited_shown_memory_aborts_atomic_commit(h):
    _, ids = moment(h)

    def change(_):
        h.store.conn.execute("UPDATE memory SET scope='private',scope_with=1 WHERE id=?", (ids[-1],))
        return answer(evidence=[1])

    ctx = context(h, change)
    run(stories.write_story(ctx, only_moment(h)))
    assert ctx.failures["stories"] == 1
    assert h.count("story") == 0 and h.count("memory") == 3


def test_memory_failure_rolls_back_story_all_copies_and_step(h, monkeypatch):
    moment(h)
    m = only_moment(h)
    ctx = context(h)
    original, calls = ctx.writer.put, 0

    def fail_second(row, **kwargs):
        nonlocal calls
        calls += 1
        result = original(row, **kwargs)
        if calls == 2:
            raise RuntimeError("synthetic story copy failure")
        return result

    monkeypatch.setattr(ctx.writer, "put", fail_second)
    run(stories.write_story(ctx, m))
    assert ctx.failures["stories"] == 1 and not ctx.done("stories", f"story:{m.key}")
    assert h.count("story") == 0 and h.count("memory") == 3


def test_stage_counts_completed_units_across_resumes_and_keeps_backlog(h, monkeypatch):
    no_extras(monkeypatch)
    for n in range(5):
        moment(h, n)
    ctx = context(h, *[answer() for _ in range(5)])
    run(stories.write_story(ctx, stories.find_moments(h.store, ctx.window)[0]))
    run(stories.stage_stories(ctx))
    assert h.count("story") == 3
    calls = len(ctx.reasoner.llm.calls)
    run(stories.stage_stories(ctx))
    assert h.count("story") == 3 and len(ctx.reasoner.llm.calls) == calls
    h.clock.advance(86400)
    tomorrow = context(h, answer(), answer(), night="2026-10-30")
    run(stories.stage_stories(tomorrow))
    assert h.count("story") == 5  # Yesterday's two capped moments were not lost.


def test_rejected_completed_story_consumes_this_nights_slot(h, monkeypatch):
    no_extras(monkeypatch)
    moment(h)
    moment(h, 1)
    ctx = context(h, answer(title="Ebbo told a story"))
    ctx.settings = replace(ctx.settings, stories_per_night=1)
    run(stories.stage_stories(ctx))
    calls = len(ctx.reasoner.llm.calls)
    run(stories.stage_stories(ctx))
    assert h.count("story") == 0 and len(ctx.reasoner.llm.calls) == calls
    assert h.store.conn.execute("SELECT count(*) FROM night_step WHERE stage='stories' AND unit LIKE 'story:%'").fetchone()[0] == 1


def test_stage_runs_extras_after_capped_stories_in_order(h, monkeypatch):
    from leeroy_mind.nightly import chronicle
    seen = []

    def recorder(name):
        async def call(ctx):
            seen.append(name)
        return call

    for name in ("write_recap", "write_chronicle", "stage_awards"):
        monkeypatch.setattr(chronicle, name, recorder(name))
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, stories_per_night=0)
    run(stories.stage_stories(ctx))
    assert seen == ["write_recap", "write_chronicle", "stage_awards"]
    assert ctx.done("stories", "retell")


def test_story_copies_include_historical_participant_currently_away(h):
    moment(h)
    h.store.conn.execute("INSERT INTO away(guid,left_at,returns_at,reason) VALUES (103,?,?,'trip')",
                        (h.T - 1, h.T + 86400))
    run(stories.write_story(context(h), only_moment(h)))
    assert h.rows(103)[-1].kind == "story"


@pytest.mark.parametrize("fatal", [None, False, True])
def test_recap_model_failure_isolated_unless_fatal(h, monkeypatch, fatal):
    from leeroy_mind.nightly import chronicle
    moment(h)
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, stories_per_night=0)
    seen = []

    async def broken_json(*args, **kwargs):
        if fatal is None:
            raise ValueError("synthetic recap failure")
        raise ReflectionError("synthetic recap failure", fatal=fatal)

    async def chronicled(ctx):
        seen.append("chronicle")

    async def awarded(ctx):
        seen.append("awards")

    monkeypatch.setattr(ctx.reasoner, "json", broken_json)
    monkeypatch.setattr(chronicle, "write_chronicle", chronicled)
    monkeypatch.setattr(chronicle, "stage_awards", awarded)
    if fatal:
        with pytest.raises(ReflectionError, match="recap failure"):
            run(stories.stage_stories(ctx))
        assert seen == [] and not ctx.done("stories", "retell")
    else:
        run(stories.stage_stories(ctx))
        assert ctx.failures["stories"] == 1
        assert seen == ["chronicle", "awards"] and ctx.done("stories", "retell")


def test_prompt_includes_at_most_twelve_raw_chat_lines(h):
    refs = [f"chat:{h.say(h.T + i, 101, f'Guild line {i} about the pull.')}" for i in range(14)]
    for guid in (101, 102, 103):
        h.mem(guid, "We remember the pull.", actors=(101, 102, 103), importance=7,
              evidence=tuple(refs))
    ctx = context(h, answer(title="The pull", summary="We remember the pull.", first_person="We remember the pull."))
    run(stories.write_story(ctx, only_moment(h)))
    row = h.store.conn.execute("SELECT evidence FROM story").fetchone()
    assert len([r for r in json.loads(row[0]) if r.startswith("chat:")]) == 12


def test_public_snapshot_rejects_deep_private_missing_and_cyclic_provenance(h):
    event, _ = moment(h)
    hidden = h.mem(101, "private", evidence=(f"event:{event}",), scope="private", scope_with=102)
    top = hidden
    for _ in range(5):
        top = h.mem(101, "legacy wrapper", evidence=(f"memory:{top}",))
    assert stories.public_snapshot(h.store, [f"memory:{top}"]) is None
    assert stories.public_snapshot(h.store, ["event:999999"]) is None
    a = h.mem(101, "A", evidence=())
    b = h.mem(101, "B", evidence=(f"memory:{a}",))
    h.store.conn.execute("UPDATE memory SET evidence=? WHERE id=?", (json.dumps([f"memory:{b}"]), a))
    assert stories.public_snapshot(h.store, [f"memory:{a}"]) is None


def test_public_snapshot_checks_story_and_chronicle_rows_and_ignores_access_counts(h):
    event, ids = moment(h)
    first = stories.public_snapshot(h.store, [f"memory:{ids[0]}"])
    h.store.touch_memories(ids, h.T + 1)
    assert stories.public_snapshot(h.store, [f"memory:{ids[0]}"]) == first
    ident = insert_story(h.store, title="Earlier", summary="A pull", participants=[101],
                         evidence=[f"memory:{ids[0]}"], ts=h.T)
    chron = h.store.conn.execute("INSERT INTO chronicle(week,era,author,text,evidence,created) "
        "VALUES ('2026-W43','autumn',101,'A pull',?,?)", (json.dumps([f"story:{ident}"]), h.T)).lastrowid
    assert stories.public_snapshot(h.store, [f"chronicle:{chron}"]) is not None
    h.store.conn.execute("UPDATE memory SET status='archived' WHERE id=?", (ids[0],))
    assert stories.public_snapshot(h.store, [f"chronicle:{chron}"]) is not None
    h.store.conn.execute("UPDATE memory SET scope=?,scope_with=? WHERE id=?", ("secret", 102, ids[0]))
    assert stories.public_snapshot(h.store, [f"chronicle:{chron}"]) is None


def test_retells_are_per_line_per_story_sent_only_and_overlap_safe(h):
    event, _ = moment(h)
    ident = insert_story(h.store, title="Earlier", summary="A pull", participants=[101, 102],
                         evidence=[f"event:{event}"], ts=h.T)
    a = h.mem(101, "A pull", evidence=(f"story:{ident}",), kind="story")
    duplicate = h.mem(101, "The same pull", evidence=(f"story:{ident}",), kind="story")
    b = h.mem(102, "A pull", evidence=(f"story:{ident}",), kind="story")
    h.utter(h.T + 1, 101, "remember that pull", [a, duplicate])
    h.utter(h.T + 2, 102, "yes that pull", [b])
    h.utter(h.T + 3, 102, "dropped", [b], dropped="cooldown")
    assert stories.count_retells(h.store, (h.T, h.T + 2)) == 1
    assert stories.count_retells(h.store, (h.T + 1, h.T + 4)) == 1
    assert stories.count_retells(h.store, (h.T, h.T + 4)) == 0
    assert h.store.conn.execute("SELECT retold_n FROM story").fetchone()[0] == 2
    h.store.conn.execute("DELETE FROM memory WHERE id IN (?,?,?)", (a, duplicate, b))
    assert stories.count_retells(h.store, (h.T, h.T + 4)) == 0
    assert h.store.conn.execute("SELECT retold_n FROM story").fetchone()[0] == 2


def test_one_utterance_can_retell_two_stories_but_is_one_counted_line(h):
    event, _ = moment(h)
    ids = [insert_story(h.store, title=str(i), summary="A pull", participants=[101],
                        evidence=[f"event:{event}"], ts=h.T) for i in range(2)]
    memory = h.mem(101, "Two pulls", evidence=tuple(f"story:{ident}" for ident in ids), kind="story")
    h.utter(h.T + 1, 101, "Both pulls", [memory])
    assert stories.count_retells(h.store, (h.T, h.T + 2)) == 1
    assert [r[0] for r in h.store.conn.execute("SELECT retold_n FROM story")] == [1, 1]


def test_retell_receipt_freezes_attribution_across_later_memory_edits(h):
    event, _ = moment(h)
    first, second = [insert_story(h.store, title=str(i), summary="A pull", participants=[101],
                                  evidence=[f"event:{event}"], ts=h.T) for i in range(2)]
    memory = h.mem(101, "First pull", evidence=(f"story:{first}",), kind="story")
    h.utter(h.T + 1, 101, "That first pull", [memory])
    assert stories.count_retells(h.store, (h.T, h.T + 2)) == 1
    h.store.conn.execute("UPDATE memory SET evidence=? WHERE id=?",
                        (json.dumps([f"story:{second}"]), memory))
    assert stories.count_retells(h.store, (h.T, h.T + 2)) == 0
    assert [r[0] for r in h.store.conn.execute("SELECT retold_n FROM story ORDER BY id")] == [1, 0]
    h.utter(h.T + 2, 101, "That second pull", [memory])
    assert stories.count_retells(h.store, (h.T, h.T + 3)) == 1
    assert [r[0] for r in h.store.conn.execute("SELECT retold_n FROM story ORDER BY id")] == [1, 1]


def test_retell_counter_and_receipt_roll_back_together(h, monkeypatch):
    event, _ = moment(h)
    ident = insert_story(h.store, title="Earlier", summary="A pull", participants=[101],
                         evidence=[f"event:{event}"], ts=h.T)
    memory = h.mem(101, "A pull", evidence=(f"story:{ident}",), kind="story")
    h.utter(h.T + 1, 101, "A pull", [memory])
    original = h.store.set_meta

    def fail(key, value):
        original(key, value)
        raise RuntimeError("synthetic receipt failure")

    monkeypatch.setattr(h.store, "set_meta", fail)
    with pytest.raises(RuntimeError, match="receipt"):
        stories.count_retells(h.store, (h.T, h.T + 2))
    assert h.store.conn.execute("SELECT retold_n FROM story").fetchone()[0] == 0
    monkeypatch.setattr(h.store, "set_meta", original)
    assert stories.count_retells(h.store, (h.T, h.T + 2)) == 1
