"""Public, grounded guild history with complete provenance and resumable commits."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import run
from leeroy_mind.nightly import chronicle as ch, knowledge
from leeroy_mind.nightly.store import insert_story
from nightly.conftest import UTC, no_real_name, yes


def raid(h, *, members=(101, 102, 103, 104, 1), at=None, map_id=631, boss_id=11, wipes=3):
    at = h.T if at is None else at
    return [h.boss(at + i, map_id, boss_id, "wiped", members) for i in range(wipes)] + [
        h.boss(at + wipes, map_id, boss_id, "killed", members)]


def recap(**changes):
    return {"title": "Cold victory", "recap": "Sindragosa fell after 3 wipes.", "evidence": [1]} | changes


def recap_ctx(h, data=None, *, judge=yes):
    return h.ctx({"recap": [recap() if data is None else data], "judge": [judge]})


def history(h, *, text="We killed Sindragosa after 3 wipes.", participants=(101, 102),
            refs=None, at=None, title="Cold victory"):
    if refs is None:
        refs = [f"event:{raid(h)[-1]}"]
    return insert_story(h.store, title=title, summary=text, participants=participants,
                        evidence=refs, ts=h.T if at is None else at)


def chronicle_ctx(h, data=None, *, night="2026-11-01", judge=yes):
    return h.ctx({"chronicle": [{"entry": "We killed Sindragosa after 3 wipes.", "evidence": [1]}
                              if data is None else data], "judge": [judge]}, night=night)


def generated_prompt(ctx, title):
    call = next(c for c in ctx.reasoner.llm.calls if (c.get("format") or {}).get("title") == title)
    return "\n".join(m["content"] for m in call["messages"])


def historian(h, guid=105):
    h.persona(guid, officer_title="Raid Lead, Guild Historian", guild_role="officer")


def test_recap_uses_boss_totals_mvp_laughs_and_retains_participants(h):
    events = raid(h)
    mvp = h.mem(0, "Arkon was the MVP.", key="mvp:2026-10-29", actors=(101,),
                evidence=(f"event:{events[-1]}",))
    line = h.say(h.T + 10, 101, "Quillon carried us while I inspected the floor.", channel="raid")
    h.say(h.T + 11, h.human, "lol", channel="raid")
    ctx = recap_ctx(h)
    run(ch.write_recap(ctx))
    text = generated_prompt(ctx, "recap")
    assert "Tonight: 3 wipes, 1 kills." in text
    assert "Arkon was the MVP" in text and "Testy carried us" in text and "Quillon" not in text
    story = h.store.conn.execute("SELECT * FROM story").fetchone()
    assert story["summary"] == recap()["recap"]
    assert set(json.loads(story["evidence"])) == {*(f"event:{i}" for i in events),
                                                f"memory:{mvp}", f"chat:{line}"}
    assert json.loads(story["participants"]) == [101, 102, 103, 104]
    for owner in (0, 101, 102, 103, 104):
        memory = h.store.keyed_memory(owner, "recap:2026-10-29")
        assert memory is not None and memory.scope == "open" and len(memory.text) <= 200
        assert memory.importance == (7 if owner == 0 else 6)
    assert h.store.keyed_memory(h.human, "recap:2026-10-29") is None
    assert knowledge.scope_leaks(h.store) == [] and no_real_name(h.store) == []


def test_recap_skips_no_raid_and_nonfight_steps_without_model(h):
    h.boss(h.T, 631, 13, "killed", (101,))
    h.boss(h.T, 33, 0, "killed", (101,))
    ctx = h.ctx()
    run(ch.write_recap(ctx))
    assert h.count("story") == 0 and not ctx.reasoner.llm.calls
    assert ctx.done("stories", "recap:2026-10-29")


@pytest.mark.parametrize("changes,reason", [
    ({"recap": "Sindragosa fell after 7 wipes."}, "number not in evidence"),
    ({"recap": "The Lich King fell."}, "boss/item not in evidence"),
    ({"evidence": [999]}, "unknown evidence"),
    ({"title": "Ebbo won the argument"}, "name not in evidence: ebbo"),
])
def test_recap_rejects_unsupported_numbers_bosses_aliases_and_title(h, changes, reason):
    raid(h)
    ctx = recap_ctx(h, recap(**changes))
    run(ch.write_recap(ctx))
    assert h.count("story") == h.count("memory") == 0
    assert h.store.conn.execute("SELECT 1 FROM grounding_log WHERE reason=?", (reason,)).fetchone()


def test_recap_rejects_judges_partly_supported_result(h):
    raid(h)
    def partly(text):
        data = yes(text)
        data["verdicts"][0]["support"] = "partly"
        return data
    ctx = recap_ctx(h, judge=partly)
    run(ch.write_recap(ctx))
    assert h.count("story") == h.count("memory") == 0


def test_recap_includes_away_raider_and_ignores_private_mvp_ancestry(h):
    raid(h)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (104,'away',0,9e9)")
    line = h.say(h.T + 10, h.human, "The bakery contract is secret.", channel="whisper", to=101)
    ref = f"chat:{line}"
    for i in range(6):
        ident = h.mem(101, f"The bakery contract detail {i}.", evidence=(ref,))
        ref = f"memory:{ident}"
    h.mem(0, "The bakery contract is the MVP news.", key="mvp:2026-10-29", evidence=(ref,))
    ctx = recap_ctx(h)
    run(ch.write_recap(ctx))
    assert "bakery" not in generated_prompt(ctx, "recap")
    assert h.store.keyed_memory(104, "recap:2026-10-29") is not None


def test_recap_bounds_laugh_sources_and_excludes_whispers(h):
    raid(h)
    for i in range(14):
        h.say(h.T + 10 + 100 * i, 101, f"Public joke {i}.", channel="guild")
        h.say(h.T + 11 + 100 * i, h.human, "lol", channel="guild")
    h.say(h.T + 1500, 101, "The private cinnamon joke.", channel="whisper", to=h.human)
    h.say(h.T + 1501, h.human, "lol", channel="whisper", to=101)
    ctx = recap_ctx(h)
    run(ch.write_recap(ctx))
    text = generated_prompt(ctx, "recap")
    assert "Public joke 11" in text and "Public joke 12" not in text and "cinnamon" not in text
    story = h.store.conn.execute("SELECT evidence FROM story").fetchone()[0]
    assert sum(ref.startswith("chat:") for ref in json.loads(story)) == 12


def test_recap_unselected_source_becoming_private_aborts_all_writes(h):
    events = raid(h)
    mvp = h.mem(0, "Arkon was the MVP.", key="mvp:2026-10-29", actors=(101,),
                evidence=(f"event:{events[-1]}",))
    def changed(_):
        with h.store.transaction():
            h.store.conn.execute("UPDATE memory SET scope='private',scope_with=101 WHERE id=?", (mvp,))
        return recap(evidence=[1])
    ctx = recap_ctx(h, changed)
    run(ch.write_recap(ctx))
    assert h.count("story") == 0 and h.count("memory") == 1
    assert not ctx.done("stories", "recap:2026-10-29")


def test_recap_full_provenance_survives_owner_zero_evidence_bound(h):
    events = raid(h, wipes=23)
    ctx = recap_ctx(h, recap(recap="Sindragosa fell."))
    run(ch.write_recap(ctx))
    story = h.store.conn.execute("SELECT evidence FROM story").fetchone()
    assert set(json.loads(story[0])) == {f"event:{i}" for i in events}
    assert len(h.store.keyed_memory(0, "recap:2026-10-29").evidence) <= 20


def test_recap_atomic_failure_and_replay(h, monkeypatch):
    raid(h)
    ctx = recap_ctx(h)
    original = ctx.writer.put
    writes = 0
    def failed(*args, **kw):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("synthetic recap copy failure")
        return original(*args, **kw)
    monkeypatch.setattr(ctx.writer, "put", failed)
    run(ch.write_recap(ctx))
    assert h.count("story") == h.count("memory") == h.count("grounding_log") == 0
    assert not ctx.done("stories", "recap:2026-10-29")
    retry = recap_ctx(h)
    run(ch.write_recap(retry))
    calls = len(retry.reasoner.llm.calls)
    run(ch.write_recap(retry))
    assert h.count("story") == 1 and h.count("memory") == 5
    assert len(retry.reasoner.llm.calls) == calls


def test_era_names_count_recent_fights_and_skip_nonfight_steps(h):
    for day in range(3):
        h.boss(datetime(2026, 10, 20 + day, 21, tzinfo=UTC).timestamp(), 603, 0, "killed", (101,))
    h.boss(datetime(2026, 10, 21, 22, tzinfo=UTC).timestamp(), 631, 0, "killed", (101,))
    for i in range(6):
        h.boss(h.T + i, 631, 13, "killed", (101,))
    end = datetime(2026, 10, 30, 12, tzinfo=UTC).timestamp()
    assert ch.era_name(h.store, end, UTC) == "the Ulduar autumn"
    assert ch.era_name(h.store, datetime(2027, 2, 1, tzinfo=UTC).timestamp(), UTC) == "the quiet winter"


def test_unmapped_raid_has_local_name_fallback_and_season_uses_supplied_zone(h):
    end = datetime(2026, 12, 1, 1, tzinfo=UTC).timestamp()
    h.boss(end - 100, 624, 0, "killed", (101,))
    west = timezone(timedelta(hours=-5))
    assert ch.era_name(h.store, end, west) == "the Vault of Archavon autumn"
    assert ch.era_name(h.store, end, UTC) == "the Vault of Archavon winter"
    assert 624 not in ch.ERA_RAID


def test_chronicler_uses_title_then_lowest_guid_lore_member(h):
    h.persona(101, thinks_as="lore")
    h.persona(102, thinks_as="lore")
    historian(h, 105)
    ctx = h.ctx()
    assert ch.chronicler(ctx) == 105
    h.persona(105, officer_title="Raid Lead")
    assert ch.chronicler(ctx) == 101
    h.persona(101, thinks_as="social")
    h.persona(102, thinks_as="social")
    assert ch.chronicler(ctx) is None


def test_chronicle_sunday_writes_owner_zero_historian_and_away_participants(h):
    historian(h)
    sid = history(h, participants=(101, 102, 1))
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (102,'away',0,9e9)")
    ctx = chronicle_ctx(h)
    run(ch.write_chronicle(ctx))
    row = h.store.conn.execute("SELECT * FROM chronicle").fetchone()
    assert row["week"] == "2026-W44" and row["author"] == 105 and row["era"] == "the Icecrown autumn"
    assert json.loads(row["evidence"]) == [f"story:{sid}"]
    for owner in (0, 101, 102, 105):
        memory = h.store.keyed_memory(owner, "chronicle:2026-W44")
        assert memory is not None and memory.scope == "open" and len(memory.text) <= 200
        assert memory.evidence == (f"chronicle:{row['id']}",)
    assert not h.rows(h.human)
    calls = len(ctx.reasoner.llm.calls)
    run(ch.write_chronicle(ctx))
    assert h.count("chronicle") == 1 and len(ctx.reasoner.llm.calls) == calls
    assert knowledge.scope_leaks(h.store) == []


def test_chronicle_can_name_nonraiding_public_speaker_without_giving_them_copies(h):
    raid(h)
    h.say(h.T + 10, 105, "Ebbo kept everyone laughing through the raid.", channel="guild")
    h.say(h.T + 11, h.human, "lol", channel="guild")
    recap_run = recap_ctx(h, recap(recap="Sindragosa fell. Ebbo kept everyone laughing.", evidence=[1, 5]))
    run(ch.write_recap(recap_run))
    story = h.store.conn.execute("SELECT participants FROM story").fetchone()
    assert 105 not in json.loads(story[0])
    historian(h, 104)
    ctx = chronicle_ctx(h, {"entry": "Ebbo kept everyone laughing while we killed Sindragosa.", "evidence": [1]})
    run(ch.write_chronicle(ctx))
    assert h.count("chronicle") == 1
    assert h.store.keyed_memory(105, "recap:2026-10-29") is None
    assert h.store.keyed_memory(105, "chronicle:2026-W44") is None


def test_delayed_sunday_recap_still_enters_its_original_week_chronicle(h):
    historian(h)
    sunday = datetime(2026, 11, 1, 21, tzinfo=UTC).timestamp()
    monday = datetime(2026, 11, 2, 21, tzinfo=UTC).timestamp()
    h.clock.set(monday)
    raid(h, at=sunday)
    ctx = h.ctx({"recap": [recap()], "chronicle": [
        {"entry": "We killed Sindragosa after 3 wipes.", "evidence": [1]}], "judge": [yes, yes]},
        night="2026-11-01")
    run(ch.write_recap(ctx))
    story = h.store.conn.execute("SELECT * FROM story").fetchone()
    assert story["t_created"] == monday
    assert h.store.get_meta(f"nightly:story-night:{story['id']}") == "2026-11-01"
    run(ch.write_chronicle(ctx))
    chronicle = h.store.conn.execute("SELECT * FROM chronicle").fetchone()
    assert chronicle["week"] == "2026-W44"
    assert json.loads(chronicle["evidence"]) == [f"story:{story['id']}"]


@pytest.mark.parametrize("missing", ["sunday", "historian", "evidence"])
def test_chronicle_requires_sunday_chronicler_and_evidence(h, missing):
    if missing != "historian":
        historian(h)
    if missing != "evidence":
        history(h)
    ctx = h.ctx(night="2026-10-31" if missing == "sunday" else "2026-11-01")
    run(ch.write_chronicle(ctx))
    assert h.count("chronicle") == 0 and not ctx.reasoner.llm.calls


def test_chronicle_can_draw_only_on_public_owner_zero_boss_memory(h):
    historian(h)
    event = raid(h)[-1]
    mid = h.mem(0, "We killed Sindragosa after 3 wipes.", key="boss:631:11:2026-10-29",
                actors=(101, 102), evidence=(f"event:{event}",))
    ctx = chronicle_ctx(h)
    run(ch.write_chronicle(ctx))
    row = h.store.conn.execute("SELECT * FROM chronicle").fetchone()
    assert json.loads(row["evidence"]) == [f"memory:{mid}"]
    assert h.store.keyed_memory(105, "chronicle:2026-W44") is not None
    assert h.store.keyed_memory(101, "chronicle:2026-W44") is None


def test_private_story_and_private_nested_story_are_never_shown(h):
    historian(h)
    public = history(h)
    line = h.say(h.T, h.human, "The cinnamon contract stays private.", channel="whisper", to=101)
    private = history(h, title="Cinnamon", text="The cinnamon contract was signed.", refs=(f"chat:{line}",))
    nested = history(h, title="Contract", text="The cinnamon contract mattered.", refs=(f"story:{private}",))
    ctx = chronicle_ctx(h)
    run(ch.write_chronicle(ctx))
    assert "cinnamon" not in generated_prompt(ctx, "chronicle")
    row = h.store.conn.execute("SELECT evidence FROM chronicle").fetchone()
    assert json.loads(row[0]) == [f"story:{public}"]


def test_chronicle_unselected_story_changes_during_generation_abort(h):
    historian(h)
    history(h)
    other = history(h, title="Other fight", text="Coros helped us kill Sindragosa.", participants=(103,))
    def changed(_):
        with h.store.transaction():
            h.store.conn.execute("UPDATE story SET summary='Coros stayed home.' WHERE id=?", (other,))
        return {"entry": "We killed Sindragosa after 3 wipes.", "evidence": [1]}
    ctx = chronicle_ctx(h, changed)
    run(ch.write_chronicle(ctx))
    assert h.count("chronicle") == 0 and h.count("memory") == 0
    assert not ctx.done("stories", "chronicle:2026-W44")


def test_chronicle_historian_loses_title_during_generation_abort(h):
    historian(h)
    history(h)
    def changed(_):
        h.persona(105, officer_title="Raid Lead")
        return {"entry": "We killed Sindragosa after 3 wipes.", "evidence": [1]}
    ctx = chronicle_ctx(h, changed)
    run(ch.write_chronicle(ctx))
    assert h.count("chronicle") == 0 and not ctx.done("stories", "chronicle:2026-W44")


def test_chronicle_rejects_invented_fact_and_uses_no_thinking_call(h):
    historian(h)
    history(h)
    ctx = chronicle_ctx(h, {"entry": "We killed Sindragosa after 9 wipes.", "evidence": [1]})
    run(ch.write_chronicle(ctx))
    assert h.count("chronicle") == 0
    assert not any(call.get("think") for call in ctx.reasoner.llm.calls)


def test_chronicle_rolls_back_partial_copy_failure_and_can_retry(h, monkeypatch):
    historian(h)
    history(h)
    ctx = chronicle_ctx(h)
    original = ctx.writer.put
    calls = 0
    def failed(*args, **kw):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic chronicle copy failure")
        return original(*args, **kw)
    monkeypatch.setattr(ctx.writer, "put", failed)
    run(ch.write_chronicle(ctx))
    assert h.count("chronicle") == h.count("memory") == h.count("grounding_log") == 0
    assert not ctx.done("stories", "chronicle:2026-W44")
    retry = chronicle_ctx(h)
    run(ch.write_chronicle(retry))
    assert h.count("chronicle") == 1 and h.count("memory") == 4


def test_chronicle_reexports_awards_api():
    from leeroy_mind.nightly.awards import AWARDS, stage_awards
    assert ch.AWARDS is AWARDS and ch.stage_awards is stage_awards
