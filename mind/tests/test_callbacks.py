import dataclasses
import json
import sqlite3

import pytest

from leeroy_mind.memory.callbacks import (
    CallbackContext, DAY, load_callbacks_config, pick_callback, record_callback,
)
from leeroy_mind.memory.callback_cli import callbacks_main, replay
from leeroy_mind.storage.rows import pack_vec

NOW = 100 * DAY


@pytest.fixture
def db(store):
    c = store.conn
    c.execute("INSERT INTO session(id,started) VALUES(1,?)", (NOW-7200,))
    c.execute("INSERT INTO meta(key,value) VALUES('human_guid','1')")
    c.execute("INSERT INTO game_event(id,ts,type,payload) VALUES(1,?,'boss','{}')", (NOW-5*DAY,))
    c.commit()
    return c


def memory(db, *, guid=101, age=5, evidence=None, actors=None, **kw):
    values = dict(owner=guid, kind="event", text="Our invented first boss kill.", t_event=NOW-age*DAY,
                  t_created=NOW-age*DAY, t_accessed=NOW-age*DAY, importance=8, valence=.6,
                  source="game", evidence=json.dumps(evidence if evidence is not None else ["event:1"]),
                  actors=json.dumps(actors if actors is not None else [guid, 1]), scope="open", scope_with=0)
    values.update(kw)
    sql = f"INSERT INTO memory({','.join(values)}) VALUES({','.join('?' for _ in values)})"
    return db.execute(sql, tuple(values.values())).lastrowid


def context(**kw):
    return dataclasses.replace(CallbackContext(enabled=True, channel="guild", present=(101, 1)), **kw)


def chat(db, *, channel="whisper", frm=1, to=101, audience=None, origin="human", ts=NOW-5*DAY):
    return db.execute("INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin,audience) "
                      "VALUES(?,?,?,'Invented',?,'A private invented exchange.',?,?)",
                      (ts, channel, frm, to, origin, json.dumps(audience) if audience is not None else None)).lastrowid


def test_read_only_offer_and_committed_use(db):
    mid = memory(db)
    db.commit()
    before = db.total_changes
    result = pick_callback(db, 101, context(), now=NOW)
    assert (result.memory_id, result.seed, result.reason, result.age_days) == (
        mid, "Our invented first boss kill.", "salient_recent_emotional", 5)
    assert db.total_changes == before
    assert pick_callback(db, 101, context(), now=NOW) == result
    assert record_callback(db, 101, context(), result, now=NOW)
    assert pick_callback(db, 101, context(), now=NOW) is None
    db.rollback()
    assert pick_callback(db, 101, context(), now=NOW) == result
    assert record_callback(db, 101, context(), result, now=NOW)
    db.commit()
    assert [tuple(r) for r in db.execute("SELECT memory_id,guid,ts FROM callback_use")] == [(mid, 101, NOW)]


@pytest.mark.parametrize("age,expected", [(1.999, False), (2, True), (60, True), (60.001, False)])
def test_age_bounds(db, age, expected):
    memory(db, age=age)
    assert bool(pick_callback(db, 101, context(), now=NOW)) is expected


@pytest.mark.parametrize("field,value", [("importance", 5), ("confidence", .79), ("status", "archived"),
    ("kind", "plan"), ("kind", "story"), ("source", "hearsay"), ("source", "offscreen"), ("t_created", NOW+1)])
def test_ineligible_memory(db, field, value):
    memory(db, **{field: value})
    assert pick_callback(db, 101, context(), now=NOW) is None


@pytest.mark.parametrize("actors", [[], [101], [102, 103]])
def test_requires_shared_people_present(db, actors):
    memory(db, actors=actors)
    assert pick_callback(db, 101, context(), now=NOW) is None


@pytest.mark.parametrize("scope", ["private", "secret"])
def test_pair_scope_stays_private(db, scope):
    memory(db, scope=scope, scope_with=1)
    assert pick_callback(db, 101, context(), now=NOW) is None
    assert pick_callback(db, 101, context(channel="whisper", to_guid=1), now=NOW)
    assert pick_callback(db, 101, context(channel="whisper", to_guid=102, present=(101, 102)), now=NOW) is None
    assert pick_callback(db, 101, context(channel="whisper", to_guid=1, readers=(101, 1, 102)), now=NOW) is None


def test_mis_scoped_and_mixed_whisper_evidence_never_leaks(db):
    line = chat(db)
    memory(db, evidence=["event:1", f"chat:{line}"])
    assert pick_callback(db, 101, context(), now=NOW) is None
    assert pick_callback(db, 101, context(channel="whisper", to_guid=1), now=NOW)


def test_nested_private_provenance_and_cycles_fail_closed(db):
    inner = memory(db, scope="secret", scope_with=1, importance=5)
    outer = memory(db, evidence=[f"memory:{inner}"])
    assert pick_callback(db, 101, context(), now=NOW) is None
    assert pick_callback(db, 101, context(channel="whisper", to_guid=1), now=NOW).memory_id == outer
    db.execute("UPDATE memory SET evidence=? WHERE id=?", (json.dumps([f"memory:{outer}"]), inner))
    assert pick_callback(db, 101, context(channel="whisper", to_guid=1), now=NOW) is None


@pytest.mark.parametrize("refs", [[], ["chat:99999"], ["memory:99999"], ["unknown:1"], ["encounter:1"], [3]])
def test_incomplete_or_unsupported_provenance_is_not_permission(db, refs):
    memory(db, evidence=refs)
    assert pick_callback(db, 101, context(), now=NOW) is None


def test_officer_requires_role_and_current_officer_room(db):
    line = chat(db, channel="officer", audience=[1, 101])
    memory(db, evidence=[f"chat:{line}"])
    ctx = context(channel="officer", readers=(1, 101))
    assert pick_callback(db, 101, ctx, now=NOW) is None
    # Existing persona schema; avoid inventing a parallel role flag.
    db.execute("INSERT INTO persona_core(guid,json,card,card_tokens,version,updated,source) VALUES(101,?,'',0,1,?,'generated')",
               (json.dumps({"play": {"guild_role": "officer"}}), NOW-10*DAY))
    assert pick_callback(db, 101, ctx, now=NOW)
    assert pick_callback(db, 101, context(), now=NOW) is None


def test_group_evidence_requires_same_room_and_full_current_audience(db):
    line = chat(db, channel="party", audience=[1, 101, 102])
    memory(db, evidence=[f"chat:{line}"])
    assert pick_callback(db, 101, context(), now=NOW) is None
    assert pick_callback(db, 101, context(channel="party", readers=(1, 101)), now=NOW)
    assert pick_callback(db, 101, context(channel="party", readers=(1, 101, 103)), now=NOW) is None
    assert pick_callback(db, 101, context(channel="party"), now=NOW) is None


def test_unpaired_mind_provenance_is_not_public_evidence(db):
    line = chat(db, channel="guild", frm=101, to=0, origin="mind")
    memory(db, evidence=[f"chat:{line}"])
    assert pick_callback(db, 101, context(), now=NOW) is None


def test_paired_mind_line_cannot_launder_recalled_private_memory(db):
    private = memory(db, importance=5, scope="private", scope_with=1)
    line = chat(db, channel="guild", frm=101, to=0, origin="mind")
    # Match the existing utterance schema and pairing rule.
    db.execute("INSERT INTO utterance(ts,guid,channel,to_guid,kind,text,memory_ids) VALUES(?,101,'guild',0,'reply',?,?)",
               (NOW-5*DAY, "A private invented exchange.", json.dumps([private])))
    memory(db, evidence=[f"chat:{line}"])
    assert pick_callback(db, 101, context(), now=NOW) is None


def test_overlapping_sessions_do_not_refill_quota(db):
    first = memory(db)
    memory(db, age=6)
    db.execute("UPDATE session SET started=?,ended=?", (NOW-DAY-1800, NOW-DAY))
    db.execute("INSERT INTO session(started,ended) VALUES(?,?)", (NOW-DAY-1800, NOW-DAY))
    db.execute("INSERT INTO session(started) VALUES(?)", (NOW-1200,))
    db.execute("INSERT INTO callback_use(memory_id,guid,ts) VALUES(?,?,?)", (first, 101, NOW-DAY-600))
    assert pick_callback(db, 101, context(), now=NOW) is None


def test_stale_offer_cannot_exceed_guild_quota(db):
    offers = []
    for guid in range(101, 105):
        memory(db, guid=guid)
        ctx = context(present=(guid, 1))
        offers.append((guid, ctx, pick_callback(db, guid, ctx, now=NOW)))
    assert all(o[2] for o in offers)
    assert [record_callback(db, g, ctx, offer, now=NOW) for g, ctx, offer in offers] == [True, True, True, False]


def test_reuse_fourteen_days(db):
    mid = memory(db)
    db.execute("INSERT INTO callback_use(memory_id,guid,ts) VALUES(?,?,?)", (mid, 101, NOW-14*DAY+1))
    assert pick_callback(db, 101, context(), now=NOW) is None
    db.execute("UPDATE callback_use SET ts=?", (NOW-14*DAY,))
    assert pick_callback(db, 101, context(), now=NOW)


def test_member_limit_counts_play_hours_across_offline_gap(db):
    first = memory(db)
    second = memory(db, age=6)
    db.execute("UPDATE session SET started=?,ended=?", (NOW-DAY-1800, NOW-DAY))
    db.execute("INSERT INTO session(started) VALUES(?)", (NOW-1200,))
    db.execute("INSERT INTO callback_use(memory_id,guid,ts) VALUES(?,?,?)", (first, 101, NOW-DAY-600))
    assert pick_callback(db, 101, context(), now=NOW) is None
    assert pick_callback(db, 101, context(), now=NOW+1801).memory_id == second


def test_first_login_same_timestamp_still_counts_usage(db):
    memory(db)
    memory(db, age=6)
    db.execute("UPDATE session SET started=?", (NOW,))
    selected = pick_callback(db, 101, context(), now=NOW)
    assert record_callback(db, 101, context(), selected, now=NOW)
    assert pick_callback(db, 101, context(), now=NOW) is None


def test_guild_limit(db):
    for guid in range(101, 105):
        memory(db, guid=guid)
        ctx = context(present=(guid, 1))
        chosen = pick_callback(db, guid, ctx, now=NOW)
        if guid < 104:
            assert chosen and record_callback(db, guid, ctx, chosen, now=NOW)
        else:
            assert chosen is None
    assert pick_callback(db, 104, context(present=(104, 1)), now=NOW+3601)


def test_owner_offline_has_no_play_quota(db):
    memory(db)
    db.execute("UPDATE session SET ended=?", (NOW-1,))
    assert pick_callback(db, 101, context(), now=NOW) is None


def test_vectors_are_deterministic_and_never_generated(db):
    a = pack_vec([1.0]+[0.0]*511)
    b = pack_vec([0.0, 1.0]+[0.0]*510)
    memory(db, embedding=b)
    best = memory(db, age=6, embedding=a)
    ctx = context(embedding=a)
    for _ in range(3):
        chosen = pick_callback(db, 101, ctx, now=NOW)
        assert chosen.memory_id == best and chosen.reason == "similarity_emotional"
    assert pick_callback(db, 101, context(embedding=b"invalid"), now=NOW).reason == "salient_recent_emotional"


def test_tie_breaks_by_id(db):
    first = memory(db)
    memory(db)
    assert pick_callback(db, 101, context(), now=NOW).memory_id == first


def test_record_rechecks_evidence_and_does_not_commit(db):
    mid = memory(db)
    chosen = pick_callback(db, 101, context(), now=NOW)
    db.execute("UPDATE memory SET scope='private',scope_with=1 WHERE id=?", (mid,))
    assert not record_callback(db, 101, context(), chosen, now=NOW)
    assert not db.execute("SELECT * FROM callback_use").fetchall()


@pytest.mark.parametrize("content,expected", [("", False), ("[callbacks]\nenabled=true", True),
    ("[callbacks]\nenabled=false", False), ("[callbacks]\nenabled='true'", False),
    ("[callbacks]\nenabled=1", False), ("callbacks=true", False),
    ("[callbacks]\nenabled=true\nunknown=1", False), ("[[[", False)])
def test_config_fail_closed(tmp_path, content, expected):
    path = tmp_path / "mind.toml"
    path.write_text(content)
    assert load_callbacks_config(path) is expected


def test_disabled_means_no_database_access():
    class Unusable:
        def execute(self, *args):
            raise AssertionError("must not touch database")
    assert pick_callback(Unusable(), 101, CallbackContext(), now=NOW) is None


def test_old_database_is_not_migrated():
    conn = sqlite3.connect(":memory:")
    try:
        assert pick_callback(conn, 101, context(), now=NOW) is None
        report, _ = replay(conn, now=NOW)
        assert report["unavailable"] and report["offered"] == 0
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        conn.close()


def test_no_text_in_logs(db, caplog):
    memory(db, text="PRIVATE_MARKER_34")
    chosen = pick_callback(db, 101, context(), now=NOW)
    assert record_callback(db, 101, context(), chosen, now=NOW)
    assert "PRIVATE_MARKER_34" not in caplog.text
    columns = [r[1] for r in db.execute("PRAGMA table_info(callback_use)")]
    assert columns == ["id", "memory_id", "guid", "ts"]


def test_cli_backups_source_and_emits_only_counts(db, store, tmp_path, monkeypatch, capsys):
    memory(db, text="PRIVATE_MARKER_34")
    chat(db, channel="guild", frm=101, to=0, origin="mind", ts=NOW)
    db.commit()
    source = db.execute("PRAGMA database_list").fetchone()[2]
    from pathlib import Path
    root = Path(source).parent
    monkeypatch.setattr("leeroy_mind.memory.callback_cli.time.time", lambda: NOW)
    assert callbacks_main(["eval", "--data-dir", str(root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["turns"] == report["offered"] == 1
    assert report["scopes"] == {"open": 1}
    assert db.execute("SELECT count(*) FROM callback_use").fetchone()[0] == 0
    assert not (root / "scratch").exists()
    assert callbacks_main(["eval", "--data-dir", str(root), "--show-text"]) == 0
    assert "PRIVATE_MARKER_34" not in capsys.readouterr().out
    texts = list((root / "scratch").glob("callbacks-*.json"))
    assert len(texts) == 1 and "PRIVATE_MARKER_34" in texts[0].read_text()


def test_root_cli_routes_callbacks(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    assert main(["callbacks", "eval", "--data-dir", str(tmp_path)]) == 1
    assert "callbacks failed: OperationalError" in capsys.readouterr().err
