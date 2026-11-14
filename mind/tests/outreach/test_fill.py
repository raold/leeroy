"""Deterministic nightly intentions, using invented sources and no model calls."""
from dataclasses import replace
from datetime import datetime
import json
import random
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import run
from nightly.conftest import h  # noqa: F401
from leeroy_mind.claims import store as claims
from leeroy_mind.discord import store as discord
from leeroy_mind.outreach.fill import fill_member
from leeroy_mind.outreach.night import stage_outreach
from leeroy_mind.outreach import store as items
from leeroy_mind.outreach.sources import source_alive, source_embedding, DAY, MISS_YOU_ABOUT
from leeroy_mind.nightly.life import ensure_thread
from leeroy_mind.personas.io import load_persona
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_llm import fake_embed
from leeroy_mind.tuning.effective import MemberTuning, effective


def life(h, guid=101, *, text="The garden finally has its first flowers.", at=None):
    at = h.T - 100 if at is None else at
    tid = ensure_thread(h.store, guid, "An invented garden", at - DAY, random.Random(1))
    with h.store.transaction():
        bid = h.store.conn.execute("INSERT INTO life_beat(thread_id,night,text,ts) VALUES(?,?,?,?)",
            (tid, "2026-10-29", text, at)).lastrowid
    memory = h.mem(guid, text, t=at, source="offscreen", evidence=(f"beat:{bid}",))
    return bid, memory


def line(h, *, guid=101, to=1, channel="whisper", text="I plan to finish the garden.",
         readers=None, at=None):
    at = h.T - 100 if at is None else at
    lid = h.say(at, guid, text, channel=channel, to=to)
    if readers is not None:
        with h.store.transaction():
            h.store.conn.execute("UPDATE chat_line SET audience=? WHERE id=?", (json.dumps(readers), lid))
    if channel.startswith(("dm:", "discord:", "gdm:")):
        mid = discord.post(h.store, channel=channel.removeprefix("discord:"), author=guid,
                           origin="human" if guid == 1 else "mind", text=text, now=at)
        h.store.set_meta(f"discord:line:{mid}", str(lid))
    h.clock.set(h.T)
    return lid


def plan(h, *, guid=101, expires=None, **kw):
    lid = line(h, guid=guid, **kw)
    text = "I plan to finish the garden."
    cid = claims.add(h.store, guid, kind="plan", subject="garden", text=text, polarity=1,
        embedding=pack_vec(fake_embed(text)), source_line=lid, expires=expires, now=h.T - 100)
    return cid, lid


def follow(h, guid=101, *, channel="whisper", to=101, readers=None):
    root = line(h, guid=1, to=to, channel=channel, readers=readers,
                text="My invented exam is tomorrow.")
    mid = h.mem(guid, "Ask him how the exam went.", key=f"followup:chat:{root}",
                evidence=(f"chat:{root}",), scope="private" if channel == "whisper" else "open",
                scope_with=1 if channel == "whisper" else 0)
    return root, mid


def fill(h, guid=101, *, p=None, local=None, now=None, **kw):
    h.clock.set(h.T if now is None else now)
    return fill_member(h.store, guid, now=h.clock.now(), local=local or h.clock.local(),
        human=1, persona=p or load_persona(h.store, guid), ident=h.ident, **kw)


def all_items(h, guid=101):
    return [items.get(h.store, row[0]) for row in h.store.conn.execute(
        "SELECT id FROM outreach_item WHERE guid=? ORDER BY id", (guid,))]


def test_own_life_only_last_day_backed_by_active_memory_and_deduped(h):
    bid, memory = life(h)
    life(h, 102)
    life(h, at=h.T - DAY - 1, text="An older garden day.")
    assert fill(h) == 1
    item, = items.open_items(h.store, 101)
    assert (item.kind, item.ref, item.audience, item.weight, item.expires) == (
        "life", f"life:{bid}", "pair", 3, h.T + 2 * DAY)
    assert source_embedding(h.store, item) == h.store.get_memories([memory])[0].embedding
    assert fill(h) == 0
    with h.store.transaction():
        h.store.conn.execute("UPDATE memory SET status='superseded',superseded_by=? WHERE id=?", (memory, memory))
    assert not source_alive(h.store, item, now=h.T, human=1)
    fill(h)
    assert items.get(h.store, item.id).ended == "superseded"


def test_missing_life_memory_is_not_an_authorized_seed(h):
    bid, memory = life(h)
    with h.store.transaction():
        h.store.conn.execute("UPDATE memory SET status='rejected' WHERE id=?", (memory,))
    assert fill(h) == 0


@pytest.mark.parametrize("expires,days", [(None, 7), (2, 2), (20, 7)])
def test_live_own_plan_lasts_until_claim_or_seven_days(h, expires, days):
    cid, _ = plan(h, expires=None if expires is None else h.T + expires * DAY)
    assert fill(h) == 1
    item, = items.open_items(h.store, 101)
    assert (item.kind, item.ref, item.audience, item.weight, item.expires) == (
        "plan", f"plan:{cid}", "pair", 2, h.T + days * DAY)
    assert source_embedding(h.store, item)
    with h.store.transaction():
        h.store.conn.execute("UPDATE member_claim SET superseded_by=id WHERE id=?", (cid,))
    fill(h)
    assert items.get(h.store, item.id).ended == "superseded"


def test_expired_claim_and_others_private_plan_are_not_filled(h):
    plan(h, expires=h.T - 1)
    plan(h, to=102)
    plan(h, guid=102)
    assert fill(h) == 0


def test_followup_is_owned_root_deduped_and_three_days(h):
    root, memory = follow(h)
    follow(h, 102, to=102)
    assert fill(h) == 1
    item, = items.open_items(h.store, 101)
    assert (item.kind, item.ref, item.audience, item.weight, item.expires) == (
        "followup", f"followup:chat:{root}", "pair", 4, h.T + 3 * DAY)
    assert source_embedding(h.store, item) == h.store.get_memories([memory])[0].embedding
    assert fill(h) == 0
    with h.store.transaction():
        h.store.conn.execute("UPDATE memory SET text='Ask something else.' WHERE id=?", (memory,))
    fill(h)
    assert items.get(h.store, item.id).ended == "superseded"


def test_his_day_requires_human_and_member_witnesses_and_valid_outcome(h):
    mine = h.boss(h.T - 10, 631, 0, "killed", [1, 101])
    h.boss(h.T - 11, 631, 0, "killed", [1, 102])
    h.boss(h.T - 12, 631, 0, "wiped", [1, 101])
    h.boss(h.T - 13, 631, 0, "killed", [101, 102])
    assert fill(h) == 1
    item, = items.open_items(h.store, 101)
    assert (item.ref, item.audience, item.weight, item.expires) == (f"his_day:{mine}", "guild", 2, h.T + DAY)
    assert "killed" in item.about
    assert source_embedding(h.store, item) is None


def test_unknown_boss_is_generic_and_receipt_prose_cannot_supply_its_name(h):
    eid = h.boss(h.T - 10, 99999, 99999, "killed", [1, 101])
    with h.store.transaction():
        h.store.conn.execute("UPDATE game_event SET payload=json_set(payload,'$.boss_name','invented unsafe override') WHERE id=?", (eid,))
    fill(h)
    item, = items.open_items(h.store, 101)
    assert item.about == "You were there when he killed a boss."


def test_his_loot_uses_members_receipt_and_never_other_recipients(h):
    eid = h.boss(h.T - 10, 631, 0, "killed", [1, 101])
    with h.store.transaction():
        h.store.conn.execute("UPDATE game_event SET type='loot_won',guid=1,payload=? WHERE id=?",
            (json.dumps({"members": [1, 101], "item_id": 12345, "name": "Invented Lantern"}), eid))
    assert fill(h) == 1
    item, = items.open_items(h.store, 101)
    assert "[Invented Lantern]" in item.about
    with h.store.transaction():
        h.store.conn.execute("UPDATE game_event SET guid=102 WHERE id=?", (eid,))
    assert not source_alive(h.store, item, now=h.T, human=1)


@pytest.mark.parametrize("library_name,receipt_name,expected", [
    ("Invented Library Lantern", "Invented Receipt Lantern", "Invented Library Lantern"),
    (None, "Invented Receipt Lantern", "Invented Receipt Lantern"),
    ("as an AI", "Invented Receipt Lantern", "Invented Receipt Lantern"),
    (None, "as an AI", "an item")])
def test_his_loot_name_uses_screened_library_then_receipt_then_generic(h, library_name, receipt_name, expected):
    eid = h.loot(h.T - 10, 1, receipt_name, item_id=12345)
    with h.store.transaction():
        h.store.conn.execute("UPDATE game_event SET payload=json_set(payload,'$.members',json('[1,101]')) WHERE id=?", (eid,))
    library = SimpleNamespace(item_info=lambda ids: {12345: {"name": library_name}})
    fill(h, library=library)
    item, = items.open_items(h.store, 101)
    assert item.about == f"You were there when he won [{expected}]."
    assert source_alive(h.store, item, now=h.T, human=1, ident=h.ident, library=library)


def test_his_loot_name_screens_full_receipt_for_real_name(h):
    eid = h.loot(h.T - 10, 1, f"|Hitem:{h.ident.real_name}|h[Invented Lantern]|h", item_id=12345)
    with h.store.transaction():
        h.store.conn.execute("UPDATE game_event SET payload=json_set(payload,'$.members',json('[1,101]')) WHERE id=?", (eid,))
    fill(h)
    item, = items.open_items(h.store, 101)
    assert item.about == "You were there when he won [an item]."


def test_native_loot_uses_prior_group_in_its_session_and_not_current_membership(h):
    sid = h.store.start_session(h.T - 100)
    group = h.group(h.T - 20, [1, 101])
    eid = h.loot(h.T - 10, 1, "Invented Lantern", item_id=12345)
    later = h.group(h.T - 5, [1, 102])
    with h.store.transaction():
        h.store.conn.execute("UPDATE game_event SET session_id=? WHERE id IN (?,?,?)", (sid, group, eid, later))
    fill(h)
    item, = items.open_items(h.store, 101)
    assert item.ref == f"his_day:{eid}" and "[Invented Lantern]" in item.about
    fill(h, 102)
    assert items.open_items(h.store, 102) == []


@pytest.mark.parametrize("scenario", ["no_session", "other_session", "left", "disband", "malformed", "same_time_after"])
def test_native_loot_without_proven_prior_group_witnesses_does_not_fill(h, scenario):
    sid = h.store.start_session(h.T - 100)
    prior = h.group(h.T - 20, [1, 101])
    if scenario in {"left", "disband", "malformed"}:
        later = h.group(h.T - 15, [] if scenario == "disband" else [1, 102])
        if scenario == "malformed":
            with h.store.transaction():
                h.store.conn.execute("UPDATE game_event SET payload='{}' WHERE id=?", (later,))
    eid = h.loot(h.T - 10, 1, "Invented Lantern", item_id=12345)
    if scenario == "same_time_after":
        h.group(h.T - 10, [1, 101])
        with h.store.transaction():
            h.store.conn.execute("DELETE FROM game_event WHERE id=?", (prior,))
    with h.store.transaction():
        h.store.conn.execute("UPDATE game_event SET session_id=?", (sid,))
        if scenario == "no_session":
            h.store.conn.execute("UPDATE game_event SET session_id=NULL WHERE id=?", (eid,))
        elif scenario == "other_session":
            other_session = h.store.start_session(h.T - 200)
            h.store.conn.execute("UPDATE game_event SET session_id=? WHERE type='group'", (other_session,))
    fill(h)
    assert items.open_items(h.store, 101) == []


def test_birthday_uses_her_regional_day_and_expires_at_her_midnight(h):
    local = datetime(2026, 10, 29, 21, 30, tzinfo=ZoneInfo("America/New_York"))
    p = h.persona(101, region="UK", birthday="10-30")
    now = local.timestamp()
    fill(h, p=p, local=local, now=now)
    item, = items.open_items(h.store, 101)
    assert (item.ref, item.audience, item.weight) == ("birthday:2026-10-30", "guild", 5)
    assert item.expires == datetime(2026, 10, 31, tzinfo=ZoneInfo("Europe/London")).timestamp()
    assert not source_alive(h.store, item, now=item.expires, human=1,
                            local=datetime.fromtimestamp(item.expires, ZoneInfo("America/New_York")))


@pytest.mark.parametrize("gender,stance,age,expected", [
    ("female", "drawn", 25, True), ("female", "lust", 40, True),
    ("female", "none", 30, False), ("female", "fond", 30, False),
    ("male", "lust", 30, False), ("female", "lust", 24, False)])
def test_miss_you_only_adult_drawn_or_lust_women(h, gender, stance, age, expected):
    p = h.persona(101, player_gender=gender, stance=stance, age=age)
    fill(h, p=p)
    assert bool(items.open_items(h.store, 101)) is expected
    if expected:
        item, = items.open_items(h.store, 101)
        assert item.about == MISS_YOU_ABOUT and item.audience == "pair"
        assert item.expires == h.T + DAY


@pytest.mark.parametrize("channel,author", [("whisper", 1), ("whisper", 101), ("dm:101", 1), ("dm:101", 101)])
def test_either_person_in_either_medium_breaks_miss_you_silence(h, channel, author):
    p = h.woman(101, "drawn", age=30)
    line(h, guid=author, to=101 if author == 1 else 1, channel=channel, at=h.T - 2 * DAY + 1)
    fill(h, p=p)
    assert items.open_items(h.store, 101) == []


def test_miss_you_weight_follows_romance_and_forward_and_zero_forward_stops_it(h):
    p = h.woman(101, "lust", age=30)
    tuned = effective(p, MemberTuning(values={"forward": 10}))
    fill(h, p=tuned, romance_dial=10)
    item, = items.open_items(h.store, 101)
    assert item.weight == 3
    assert not source_alive(h.store, item, now=h.T, human=1,
        persona=effective(p, MemberTuning(values={"forward": 0})))


def test_night_is_deterministic_model_free_and_fills_at_zero_chattiness(h, monkeypatch):
    life(h)
    h.settings = replace(h.settings, chattiness=0)
    ctx = h.ctx()
    run(stage_outreach(ctx))
    assert items.open_items(h.store, 101) and ctx.done("outreach", "101")
    assert ctx.reasoner.llm.calls == []
    before = all_items(h)
    run(stage_outreach(h.ctx()))
    assert all_items(h) == before


def test_one_bad_member_does_not_end_later_units(h, monkeypatch):
    life(h, 101); life(h, 102)
    from leeroy_mind.outreach import night
    original = night.fill_member
    def failing(store, guid, **kwargs):
        if guid == 101:
            raise ValueError("synthetic_unit_failure")
        return original(store, guid, **kwargs)
    monkeypatch.setattr(night, "fill_member", failing)
    ctx = h.ctx()
    run(stage_outreach(ctx))
    assert not ctx.done("outreach", "101") and ctx.done("outreach", "102")
    assert items.open_items(h.store, 102)


def test_startup_stage_follows_life_and_human_world_and_reuses_cached_library(h, tmp_path, monkeypatch):
    from leeroy_mind.nightly.pipeline import STAGES
    from leeroy_mind.nightly.runner import NightRunner
    from leeroy_mind.outreach.attach import attach
    from leeroy_mind.outreach import service
    (tmp_path / "mind.toml").write_text("[outreach]\nenabled=true\n", encoding="utf-8")
    library = SimpleNamespace(item_info=lambda ids: {})
    runner = NightRunner(STAGES, lambda night, window: h.ctx())
    monkeypatch.setattr(service, "Outreach", lambda app: object())
    assert attach(SimpleNamespace(data_dir=tmp_path, night_runner=runner, library=library)) is not None
    names = [stage.name for stage in runner.stages]
    assert names.index("life") < names.index("human_world") < names.index("outreach")
    stage = runner.stages[names.index("outreach")]
    assert stage.model is None and stage.fn.keywords["library"] is library
    life(h)
    run(stage.fn(h.ctx()))
    assert items.open_items(h.store, 101)
