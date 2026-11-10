import json
import sqlite3
import pytest

from leeroy_mind.learn.config import load_learn_config
from leeroy_mind.learn.reward import DAY, MEMBER_DAILY_CAP, channel_kind, decayed, reward
from leeroy_mind.learn.weights import DEFAULTS, change, ensure, read
from leeroy_mind.protocol import Chat
from leeroy_mind.signals.store import put


def line(store, clock, *, channel="guild", guid=101, ts=None, origin="mind", text="invented wheel"):
    return store.insert_chat_line(Chat(0, channel, guid, "Invented", 1 if channel == "whisper" else 0,
                                      text, 0, 10), session_id=None, origin=origin, ts=ts)


def signals(store, lid, now, **values):
    with store.transaction():
        guid = store.conn.execute("SELECT from_guid FROM chat_line WHERE id=?", (lid,)).fetchone()[0]
        for kind, value in values.items():
            source = "explicit" if kind == "explicit" else "judge" if kind.startswith("judge_") else "implicit"
            put(store.conn, lid, guid, kind, value, now, source)


def screened(store, lid, now):
    with store.transaction():
        store.conn.execute("INSERT INTO night_step VALUES ('2026-10-29','signals',?,?)", (str(lid), now))


@pytest.mark.parametrize("text,enabled", [
    ("", False), ("[learn]\nenabled=true", True), ("[learn]\nenabled=false", False),
    ('learn="secret"', False), ("[learn]\nenabled=1", False),
    ("[learn]\nenabled=true\nunknown=1", False), ("[learn", False)])
def test_config(tmp_path, text, enabled):
    path = tmp_path / "mind.toml"
    path.write_text(text)
    assert load_learn_config(path).enabled is enabled


@pytest.mark.parametrize("values,quality,expected", [
    ({}, False, 0), ({"explicit": 1}, False, .4),
    ({"explicit": -1, "replied": 1, "emote_friendly": 1}, True, -.4),
    ({"explicit": 1, "replied": 1, "emote_friendly": 1}, True, 1),
    ({"muted": 1, "dial_down": 1, "nudged_against": 1}, False, -1),
    ({"dial_down": 1}, False, -.5), ({"nudged_against": 1}, False, -.5),
    ({"emote_rude": 1}, False, -.15),
    ({"judge_repetition": 1}, False, -.2),
    ({"judge_repetition": 1}, True, 0),
    ({"judge_guard_rewrite": 1}, True, .15)])
def test_formula_and_unmeasured_quality(store, clock, values, quality, expected):
    lid = line(store, clock)
    signals(store, lid, clock.now(), **values)
    if quality:
        screened(store, lid, clock.now())
    result = reward(store.conn, lid, now=clock.now())
    assert result.value == pytest.approx(expected)
    assert result.parts["quality_measured"] == float(quality)
    assert all(type(v) in (int, float) for v in result.parts.values())
    assert not store.conn.execute("SELECT 1 FROM learn_weights").fetchall()  # read-only defaults


def test_weights_append_time_and_veto_cannot_be_learned_away(store, clock):
    with store.transaction():
        a = ensure(store.conn, now=clock.now())
        assert ensure(store.conn, now=clock.now()) == a
        b = change(store.conn, dict(explicit=0, engagement=1, emote=0, quality=0), now=clock.now()+1)
    assert read(store.conn, at=clock.now()).version == a.version
    assert read(store.conn, at=clock.now()+1).version == b.version
    assert store.conn.execute("SELECT count(*) FROM learn_weights").fetchone()[0] == 2
    lid = line(store, clock)
    signals(store, lid, clock.now(), explicit=-1, replied=1)
    assert reward(store.conn, lid, now=clock.now()+1).value == -.4


@pytest.mark.parametrize("values", [dict(DEFAULTS, explicit=-1), dict(DEFAULTS, quality=float("nan")),
    dict(DEFAULTS, explicit=True), dict(DEFAULTS, extra=0), {}, dict.fromkeys(DEFAULTS, 1)])
def test_invalid_weights_never_write(store, clock, values):
    with pytest.raises(ValueError, match="invalid_learn_weights"):
        change(store.conn, values, now=clock.now())
    assert store.conn.execute("SELECT count(*) FROM learn_weights").fetchone()[0] == 0


@pytest.mark.parametrize("channel", ["guild", "whisper", "discord:general", "dm:101"])
def test_baseline_prior_14_days_same_kind_hour_only(store, clock, channel):
    now = clock.now()
    for days, offset, other, value in [(1, 0, channel, .2), (2, 0, channel, .4),
                                      (3, 0, channel, .6), (15, 0, channel, 1),
                                      (1, 3600, channel, 1),
                                      (1, 0, "raid", 1)]:
        ts = now - days*DAY + offset
        prior = line(store, clock, channel=other, ts=ts)
        signals(store, prior, ts+20, reply_len=value, replied=1, reply_latency_s=.5)
    current = line(store, clock, channel=channel)
    signals(store, current, now, reply_len=.4, replied=1, reply_latency_s=.5)
    result = reward(store.conn, current, now=now)
    assert result.parts["baseline_length"] == .4
    assert result.parts["baseline_n_length"] == 3
    assert result.parts["engagement"] == .5


def test_discord_baselines_and_unknown_evidence_do_not_become_game_evidence(store, clock):
    now = clock.now()
    for channel in ("discord:general", "dm:101"):
        prior = line(store, clock, channel=channel, ts=now-DAY)
        signals(store, prior, now-DAY+1, replied=1, reply_len=.8)
        screened(store, prior, now-DAY+30)  # signals skips D1, even if it has a receipt
        assert reward(store.conn, prior, now=now).parts["quality_measured"] == 0
    game = line(store, clock, channel="whisper")
    signals(store, game, now, reply_len=.8)
    result = reward(store.conn, game, now=now)
    assert result.parts["baseline_n_length"] == 0
    assert result.parts["replied"] == 0
    assert result.parts["engagement_measured"] == 1
    assert result.parts["engagement"] == .8
    assert channel_kind("dm:101") == "dm"
    assert channel_kind("discord:general") == "discord"


def test_future_signal_and_line_and_skipped_screening_are_not_evidence(store, clock):
    lid = line(store, clock, text="x"*4001)
    screened(store, lid, clock.now())
    signals(store, lid, clock.now()+10, explicit=1)
    assert reward(store.conn, lid, now=clock.now()).value == 0
    assert reward(store.conn, lid, now=clock.now()).parts["quality_measured"] == 0
    future = line(store, clock, ts=clock.now()+1)
    assert reward(store.conn, future, now=clock.now()).parts == {"available": 0.}


def test_member_positive_cap_resets_by_day_and_does_not_dilute_negative(store, clock):
    total = 0
    for _ in range(150):
        lid = line(store, clock)
        signals(store, lid, clock.now(), explicit=1, replied=1, emote_friendly=1)
        screened(store, lid, clock.now())
        total += reward(store.conn, lid, now=clock.now()).value
    assert 9.99 < total < MEMBER_DAILY_CAP
    signals(store, lid, clock.now(), muted=1, dial_down=1, nudged_against=1)
    assert reward(store.conn, lid, now=clock.now()).value == -1
    another = line(store, clock, guid=102)
    signals(store, another, clock.now(), explicit=1)
    assert reward(store.conn, another, now=clock.now()).value == .4
    tomorrow = line(store, clock, ts=clock.now()+DAY)
    signals(store, tomorrow, clock.now()+DAY, explicit=1)
    assert reward(store.conn, tomorrow, now=clock.now()+DAY).value == .4
    assert decayed(.4, age_seconds=21*DAY) == .2


def test_old_db_and_missing_line_read_only():
    conn = sqlite3.connect(":memory:")
    assert reward(conn, 1).parts == {"available": 0.}
    assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    conn.close()


def test_storage_rejects_text_in_reward_parts(store, clock):
    lid = line(store, clock)
    with store.transaction():
        weights = ensure(store.conn, now=clock.now())
    with pytest.raises(sqlite3.IntegrityError, match="reward_parts_not_numeric"):
        store.conn.execute("INSERT INTO talk_reward VALUES (?,?,?,?,?,?)",
            (lid, 101, 0, json.dumps({"secret": "private words"}), weights.version, clock.now()))


def test_signal_units_and_future_screening_receipt_are_not_misread(store, clock):
    lid = line(store, clock)
    signals(store, lid, clock.now(), reply_latency_s=.25)
    screened(store, lid, clock.now()+30)
    before = store.conn.total_changes
    result = reward(store.conn, lid, now=clock.now())
    assert result.parts["speed"] == .75
    assert result.parts["engagement"] == .75
    assert result.parts["quality_measured"] == 0
    assert store.conn.total_changes == before
