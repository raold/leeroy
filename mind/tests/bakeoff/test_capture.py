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
    assert (row["calls"], row["attempt"], row["beat"], row["want"]) == (1, 0, 0, None)
    assert row["first_messages"] is None and row["first_reply"] is None and row["first_model"] is None
    shape = json.loads(row["shape"])
    assert set(shape) == {"max_lines", "words", "two_lines"} and shape["max_lines"] >= 1
    assert shape["words"] == 0 and shape["two_lines"] is False
    assert row["ts"] == pytest.approx(line["ts"], abs=1)


def test_a_retried_turn_keeps_the_sent_request_and_the_first_one(rig):
    """A first silence in answer to his whisper is asked again with a hint: the hinted retry is
    the sent request (attempt 1 of 2 calls), and the first request, without the hint, is kept
    beside it (review-328 P1-1)."""
    rig.engine.bakeoff_capture = True
    rig.script("(nothing)", "yeah see you at 8")
    rig.whisper_from_him(102, "hey you")
    rig.run()
    calls = rig.chat_calls()
    assert len(calls) == 2 and [s.text for s in rig.sent.says()] == ["yeah see you at 8"]
    [row] = rows(rig.store)
    assert json.loads(row["messages"]) == calls[1]["messages"] != calls[0]["messages"]
    assert row["reply"] == "yeah see you at 8"
    assert (row["calls"], row["attempt"]) == (2, 1)
    assert json.loads(row["first_messages"]) == calls[0]["messages"]
    assert json.loads(row["first_options"]) == calls[0]["options"]
    assert row["first_reply"] == "(nothing)" and row["first_think"] == 0
    assert row["first_model"] == rig.cfg.models.live


def test_the_owed_fallback_is_the_third_call_and_the_first_draft_is_kept(store, clock, tmp_path):
    """test_owed_answers: no draft can stand, so the owed fallback answers; it is attempt 2 of 3,
    and the first draft (before any hint) is kept beside it."""
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    rig = make_rig(store, clock, tmp_path)
    try:
        rig.engine.bakeoff_capture = True
        rig.script("happy to help with that", "come over already", "oh. wow. i don't know what to say")
        rig.whisper_from_him(102, "leave him for me")
        rig.run()
        calls = rig.chat_calls()
        assert len(calls) == 3
        [row] = rows(store)
        assert (row["calls"], row["attempt"]) == (3, 2)
        assert row["reply"] == "oh. wow. i don't know what to say"
        assert json.loads(row["messages"]) == calls[2]["messages"]
        assert json.loads(row["first_messages"]) == calls[0]["messages"]
        assert row["first_reply"] == "happy to help with that"
    finally:
        rig.close()


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
        assert (row["calls"], row["attempt"]) == (2, 0) and row["first_messages"] is None
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
    kept = meta.bakeoff_requests
    assert len(kept) == capture.KEPT and kept[0]["reply"] == "r0" and kept[0]["index"] == 0
    assert [r["index"] for r in kept[1:]] == list(range(4, capture.KEPT + 3))
    with store.transaction():
        record(store, turn, meta, 5, words="r10")
    row = rows(store)[-1]
    assert (row["calls"], row["attempt"], row["first_reply"]) == (capture.KEPT + 3, 10, "r0")
    store.conn.execute("DELETE FROM bakeoff_turn WHERE line_id=5")
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


BEAT = "You read it as: he wants her to come\nIt lands: warm\nYou want to: tease Zan"


def test_a_beat_s_lines_never_reach_the_store():
    """review-329: a private answer's beat (talk/beat.py, appended to the instruction before any
    hint) is taken back out; only that there was one and its want code are kept."""
    base = [{"role": "system", "content": "card"}, {"role": "user", "content": "Answer Zan."}]
    with_beat = [base[0], {"role": "user", "content": "Answer Zan.\n" + BEAT + "\nAnswer in a line."}]
    req = capture.request("m", with_beat, {}, False, "x", max_lines=1, words=0, two_lines=False,
                          beat=BEAT)
    assert req["messages"][1]["content"] == "Answer Zan.\nAnswer in a line."
    assert req["beat"] is True and req["want"] == "tease"
    assert "tease Zan" not in json.dumps(req)
    rewrite = [*with_beat, {"role": "assistant", "content": "draft"},
               {"role": "user", "content": "Say it again."}]
    assert capture.without_beat(rewrite, BEAT)[1]["content"] == "Answer Zan.\nAnswer in a line."
    plain = capture.request("m", base, {}, False, "x", max_lines=1, words=0, two_lines=False)
    assert plain["messages"] == base and plain["beat"] is False and plain["want"] is None
    assert capture.want_of("You want to: something else") is None and capture.want_of(None) is None


def test_the_ops_switchboard_lists_the_capture_switch(tmp_path):
    from leeroy_mind.console import opsview
    import time
    for text, state in (("", "off"), ("[bakeoff]\ncapture = true\n", "on"),
                        ("[bakeoff]\ncapture = 1\n", "invalid")):
        (tmp_path / "mind.toml").write_text(text)
        rows_ = opsview.switches_view(tmp_path, time.monotonic() + 10, mind_started=None)["rows"]
        assert next(r for r in rows_ if r["table"] == "bakeoff")["state"] == state


def test_a_live_beat_is_taken_out_of_the_captured_request(store, clock, tmp_path):
    """review-329 rule, through the pipeline: with [talk] think_private on, his whisper's answer
    has a beat; the stored request is the line request before the beat's lines, with beat = 1
    and the want code; the beat's words are nowhere in the row."""
    from leeroy_mind.talk import beat as beats
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    rig = make_rig(store, clock, tmp_path, beat=True)
    try:
        rig.engine.bakeoff_capture = True
        vent = {"read": "he is wiped out after a long shift", "lands": "soft, a bit protective",
                "want": "vent", "use": None}

        def reply(model, messages):
            if beats.MARK in messages[-1]["content"]:
                return json.dumps(vent)
            return "rough one, put your feet up"
        rig.llm.set_script(reply)
        rig.whisper_from_him(102, "long shift, i'm wiped")
        rig.run()
        line_calls = [c for c in rig.chat_calls() if c["format"] is None]
        assert len(line_calls) == 1 and "You read it as:" in line_calls[0]["messages"][-1]["content"]
        [row] = rows(store)
        assert row["beat"] == 1 and row["want"] == "vent"
        stored = json.loads(row["messages"])
        assert stored[-1]["content"] + "\n" + beats.lines(beats.Beat(
            vent["read"], vent["lands"], "vent", None), rig.engine.human.called) \
            == line_calls[0]["messages"][-1]["content"]
        text = json.dumps(dict(row))
        for words in ("wiped out after a long shift", "soft, a bit protective", "You read it as",
                      "get something off your chest"):
            assert words not in text
    finally:
        rig.close()
