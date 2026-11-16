"""[bakeoff] capture: the sent turn's exact request is kept after the line is recorded, never
when the switch is off, never at the cost of the line; old rows are pruned. Test cast only."""
import json
from types import SimpleNamespace

import pytest

from conftest import seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.bakeoff import capture
from leeroy_mind.bakeoff.config import BakeoffCfg, load
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind


@pytest.fixture
def clock():
    return engine_clock()


@pytest.fixture
def rig(store, clock, tmp_path):
    seed_test_cast(store)
    save_member(store, 102, typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    r = make_rig(store, clock, tmp_path)
    yield r
    r.close()


def rows(store):
    return store.conn.execute("SELECT * FROM bakeoff_turn ORDER BY id").fetchall()


@pytest.mark.parametrize("text,on", [("[bakeoff]\ncapture = true\n", True),
                                     ("[bakeoff]\ncapture = false\n", False),
                                     ("[bakeoff]\ncapture = 1\n", False),
                                     ("[bakeoff]\ncapture = true\nextra = 1\n", False),
                                     ("[bakeoff\n", False), ("", False)])
def test_the_switch_is_a_boolean_and_off_by_default(tmp_path, text, on):
    (tmp_path / "mind.toml").write_text(text)
    assert load(tmp_path / "mind.toml") == BakeoffCfg(on)
    assert load(tmp_path / "absent.toml") == BakeoffCfg()


def test_off_captures_nothing(rig):
    rig.script("yeah i'm around tonight")
    rig.whisper_from_him(102, "you around tonight?")
    rig.run()
    assert rig.sent.says()
    assert rows(rig.store) == []


def test_on_keeps_the_exact_request_of_the_sent_turn(rig):
    rig.engine.bakeoff_capture = True
    rig.script("yeah i'm around tonight")
    rig.whisper_from_him(102, "you around tonight?")
    rig.run()
    [row] = rows(rig.store)
    line = rig.store.conn.execute("SELECT * FROM chat_line WHERE id=?", (row["line_id"],)).fetchone()
    assert line["origin"] == "mind" and line["from_guid"] == 102 == row["guid"]
    assert row["channel"] == "whisper" and row["kind"] == TurnKind.REPLY_HUMAN.value
    assert json.loads(row["refs"]) == [f"c:{row['line_id']}"] and row["discord_id"] is None
    [call] = rig.chat_calls()
    assert json.loads(row["messages"]) == call["messages"]
    assert json.loads(row["options"]) == call["options"]
    assert row["think"] == 0 and call["think"] is False
    assert row["model"] == rig.cfg.models.live == call["model"]
    assert row["reply"] == "yeah i'm around tonight"
    shape = json.loads(row["shape"])
    assert set(shape) == {"max_lines", "words", "two_lines"} and shape["max_lines"] >= 1
    assert shape["words"] == 0 and shape["two_lines"] is False
    assert row["ts"] == pytest.approx(line["ts"], abs=1)


def test_the_request_kept_is_the_one_whose_words_were_sent(rig):
    """A first silence in answer to his whisper is asked again: the retry is the one kept."""
    rig.engine.bakeoff_capture = True
    rig.script("(nothing)", "yeah see you at 8")
    rig.whisper_from_him(102, "hey you")
    rig.run()
    calls = rig.chat_calls()
    assert len(calls) == 2 and [s.text for s in rig.sent.says()] == ["yeah see you at 8"]
    [row] = rows(rig.store)
    assert json.loads(row["messages"]) == calls[1]["messages"] != calls[0]["messages"]
    assert row["reply"] == "yeah see you at 8"


def test_a_kept_first_draft_keeps_its_own_request(store, clock, tmp_path):
    """An owed answer's first draft (soft tells only) stands when the retry is worse
    (test_owed_answers): the call it came from is the one kept, not the later retry."""
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    rig = make_rig(store, clock, tmp_path)
    try:
        rig.engine.bakeoff_capture = True
        rig.script("i missed you so much today, feel free to tell me everything",
                   "come over already")
        rig.whisper_from_him(102, "hey you")
        rig.run()
        calls = rig.chat_calls()
        assert len(calls) == 2 and "missed you" in " ".join(s.text for s in rig.sent.says())
        [row] = rows(store)
        assert row["reply"].startswith("i missed you")
        assert json.loads(row["messages"]) == calls[0]["messages"] != calls[1]["messages"]
    finally:
        rig.close()


def test_a_capture_failure_never_costs_the_line(rig, caplog):
    rig.engine.bakeoff_capture = True
    rig.store.conn.execute("DROP TABLE bakeoff_turn")
    rig.store.conn.commit()
    rig.script("yeah i'm around tonight")
    rig.whisper_from_him(102, "you around tonight?")
    rig.run()
    assert [s.text for s in rig.sent.says()] == ["yeah i'm around tonight"]
    assert rig.store.conn.execute("SELECT count(*) FROM chat_line WHERE origin='mind' "
                                  "AND from_guid=102").fetchone()[0] == 1
    assert "bakeoff_capture_failed" in caplog.text
    assert "around tonight" not in caplog.text


def _req(reply="hi there"):
    return capture.request("m", [{"role": "user", "content": "x"}], {"num_predict": 80}, False,
                           reply, max_lines=2, words=90, two_lines=False)


def _meta(*replies):
    meta = TurnMeta("t1")
    for reply in replies or ("hi there",):
        capture.keep(meta, _req(reply))
    return meta


def record(store, turn, meta, line_id, *, discord_id=None, now=100.0, words="hi there"):
    capture.on_record(SimpleNamespace(store=store), turn, meta, line_id=line_id,
                      discord_id=discord_id, now=now, words=words)


def test_a_generation_s_later_lines_add_their_refs_and_a_new_request_a_new_row(store):
    turn = Turn(TurnKind.REPLY_HUMAN, 101, "dm:1", to_guid=1, addressee=1, max_lines=1)
    meta = _meta()
    with store.transaction():
        record(store, turn, meta, 10, discord_id=7)
        record(store, turn, meta, 11, discord_id=8, words="*there")  # a burst's next message
    [row] = rows(store)
    assert json.loads(row["refs"]) == ["d:7", "d:8"] and row["discord_id"] == 7
    assert meta.bakeoff_requests == [] and meta.bakeoff_row == row["id"]
    capture.keep(meta, _req("second go"))  # a second generation on the same turn
    with store.transaction():
        record(store, turn, meta, 12, words="second go")
    assert [json.loads(r["refs"]) for r in rows(store)] == [["d:7", "d:8"], ["c:12"]]
    untouched = TurnMeta("t2")
    with store.transaction():
        record(store, turn, untouched, 13)
    assert len(rows(store)) == 2


def test_the_sent_words_pick_their_call_and_a_line_not_the_model_s_is_not_kept(store):
    turn = Turn(TurnKind.REPLY_HUMAN, 101, "whisper", to_guid=1, addressee=1, max_lines=1)
    meta = _meta("i missed you so much today", "come over already")  # a kept first draft
    with store.transaction():
        record(store, turn, meta, 1, words="i missed you so much")
    assert [r["reply"] for r in rows(store)] == ["i missed you so much today"]
    meta = _meta("ok then", "ok then")  # a tie: the latest
    meta.bakeoff_requests[1]["reply"] = "OK then!"
    with store.transaction():
        record(store, turn, meta, 2, words="ok then")
    assert rows(store)[-1]["reply"] == "OK then!"
    meta = _meta("draft one")
    with store.transaction():
        record(store, turn, meta, 3, words="something code wrote")
    assert len(rows(store)) == 2 and meta.bakeoff_row is None and meta.bakeoff_requests == []
    meta = TurnMeta("t3")
    for i in range(capture.KEPT + 3):
        capture.keep(meta, _req(f"r{i}"))
    assert len(meta.bakeoff_requests) == capture.KEPT
    voice = Turn(TurnKind.REPLY_HUMAN, 101, "voice:raid1", max_lines=1)  # spoken: not kept
    meta = _meta()
    with store.transaction():
        record(store, voice, meta, 4)
    assert len(rows(store)) == 2 and meta.bakeoff_requests == []


def test_prune_keeps_fourteen_days(store):
    turn = Turn(TurnKind.REPLY_HUMAN, 101, "guild", max_lines=1)
    now = 2_000_000_000.0
    with store.transaction():
        for i, age in enumerate((15, 13)):
            record(store, turn, _meta(), i, now=now - age * 86400)
        assert capture.prune(store.conn, now=now) == 1
    assert [r["line_id"] for r in rows(store)] == [1]


class Runner:
    def __init__(self):
        self.stages = []

    def add_stage(self, name, fn, *, after, model):
        self.stages.append((name, after, model, fn))


def _app(store, clock, tmp_path, text):
    (tmp_path / "mind.toml").write_text(text)
    return SimpleNamespace(data_dir=tmp_path, store=store, clock=clock, engine=SimpleNamespace(),
                           night_runner=Runner())


def test_attach_reads_the_switch_prunes_and_adds_the_night_prune_only_when_needed(store, clock, tmp_path):
    app = _app(store, clock, tmp_path, "")
    capture.attach(app)
    assert app.engine.bakeoff_capture is False and app.night_runner.stages == []
    app = _app(store, clock, tmp_path, "[bakeoff]\ncapture = true\n")
    capture.attach(app)
    assert app.engine.bakeoff_capture is True
    assert [s[:3] for s in app.night_runner.stages] == [("bakeoff", "threads", None)]
    turn = Turn(TurnKind.REPLY_HUMAN, 101, "guild", max_lines=1)
    with store.transaction():
        for i, age in enumerate((20, 1)):
            record(store, turn, _meta(), i, now=clock.now() - age * 86400)
    app = _app(store, clock, tmp_path, "")  # off again, but a row is still there to prune
    capture.attach(app)
    assert [r["line_id"] for r in rows(store)] == [1]
    assert [s[0] for s in app.night_runner.stages] == ["bakeoff"]


def test_the_night_stage_prunes_once(store, clock):
    turn = Turn(TurnKind.REPLY_HUMAN, 101, "guild", max_lines=1)
    with store.transaction():
        record(store, turn, _meta(), 1, now=clock.now() - 30 * 86400)
    done = set()

    def commit(stage, unit, write):
        with store.transaction():
            write(store)
        done.add((stage, unit))
        return True
    ctx = SimpleNamespace(store=store, clock=clock, night="2026-10-28",
                          done=lambda s, u: (s, u) in done, commit=commit)
    import asyncio
    asyncio.run(capture.stage_bakeoff(ctx))
    assert rows(store) == [] and done == {("bakeoff", "prune")}
    asyncio.run(capture.stage_bakeoff(ctx))
