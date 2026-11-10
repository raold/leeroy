import json
from dataclasses import replace

import pytest

from engine_rig import make_rig
from leeroy_mind.learn import recipe
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind


@pytest.fixture
def rig(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    yield r
    r.close()


def record(rig, channel="guild", *, text="invented private wheels", template=None):
    turn = Turn(TurnKind.REPLY_HUMAN, 101, channel, to_guid=1, addressee=1, template=template)
    meta = TurnMeta(rig.engine._new_id())
    p = rig.engine.personas.get(101)
    return rig.engine._record(turn, meta, p, text, [])


@pytest.mark.parametrize("channel", ["guild", "whisper", "party", "raid", "discord:general", "dm:101"])
def test_central_record_has_one_text_free_recipe_and_discord_has_no_game_clock(rig, channel):
    rig.engine.learn_enabled = True
    before = (rig.engine._last_talk_at, dict(rig.engine._last_line_id))
    lid = record(rig, channel)
    row = rig.store.conn.execute("SELECT * FROM talk_recipe WHERE line_id=?", (lid,)).fetchone()
    assert row["template_id"] == "talk.live"
    assert row["template_version"] == 1
    assert len(row["dials_hash"]) == 64
    assert row["model_id"] == rig.cfg.models.live
    assert row["guard_verdict"] == "not_run"
    assert json.loads(row["arm_ids"]) == json.loads(row["lesson_ids"]) == []
    assert "invented private wheels" not in json.dumps(dict(row))
    assert rig.store.conn.execute("SELECT count(*) FROM talk_recipe").fetchone()[0] == 1
    if channel in ("discord:general", "dm:101"):
        assert before == (rig.engine._last_talk_at, rig.engine._last_line_id)
        assert rig.store.conn.execute("SELECT session_id FROM chat_line WHERE id=?", (lid,)).fetchone()[0] is None


def test_off_changes_no_learning_tables_or_prompt(rig):
    record(rig)
    for table in ("talk_recipe", "talk_reward", "learn_weights"):
        assert rig.store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_recipe_failure_rolls_back_chat_line_and_recipe_together(rig, monkeypatch):
    rig.engine.learn_enabled = True
    original = recipe.record
    def broken(*args):
        original(*args)
        raise RuntimeError("injected_recipe_failure")
    monkeypatch.setattr(recipe, "record", broken)
    with pytest.raises(RuntimeError, match="injected_recipe_failure"):
        record(rig)
    assert rig.store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0] == 0
    assert rig.store.conn.execute("SELECT count(*) FROM talk_recipe").fetchone()[0] == 0


def test_template_recipe_uses_identifier_instead_of_text_or_model(rig):
    rig.engine.learn_enabled = True
    lid = record(rig, template="private template")
    row = rig.store.conn.execute("SELECT template_id,model_id FROM talk_recipe WHERE line_id=?", (lid,)).fetchone()
    assert tuple(row) == ("talk.template", "none")


def test_dials_hash_tracks_effective_member_and_world_values(rig, monkeypatch):
    from leeroy_mind.tuning.effective import MemberTuning, effective
    rig.engine.learn_enabled = True
    first = record(rig)
    p = effective(rig.engine.personas.get(101), MemberTuning({"talkative": 1}))
    monkeypatch.setattr(rig.engine.personas, "get", lambda _: p)
    second = record(rig)
    rig.engine.cfg = replace(rig.engine.cfg, romance=replace(rig.engine.cfg.romance, dial=1))
    third = record(rig)
    hashes = [r[0] for r in rig.store.conn.execute("SELECT dials_hash FROM talk_recipe ORDER BY line_id")]
    assert len(set(hashes)) == 3
