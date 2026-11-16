"""lexicon clean-junk: stored everyday-word slang and its copies are retired, reversibly."""
import json
import sqlite3

import pytest

from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.nightly import junk_slang, lexicon, vocab
from leeroy_mind.nightly.lexicon_cli import lexicon_main


def stored(h, word, adopters=(102, 103), *, originator=101, kind="slang", target=0):
    """A lexicon term as the old slang stage left it: adopters, and each adopter's copy."""
    with h.store.transaction():
        ident = h.store.conn.execute(
            "INSERT INTO lexicon(kind,term,target,origin_ref,originator,born,last_used) VALUES (?,?,?,?,?,?,?)",
            (kind, word, target, "chat:1", originator, h.T, h.T)).lastrowid
        for guid in (originator, *adopters):
            h.store.conn.execute("INSERT INTO lexicon_adopter(term,guid,first_use,via) VALUES (?,?,?,?)",
                                 (ident, guid, h.T, originator))
    key = lexicon.memory_key({"kind": kind, "term": word, "target": target, "id": ident})
    for guid in adopters:
        h.mem(guid, f"Picked up '{word}' from Arkon.", kind="reflection", source="chat", key=key,
              evidence=(f"joke:{ident}", "chat:1"))
    return ident


def state(h):
    h.store.conn.commit()  # end this connection's read snapshot: the command writes through its own
    c = h.store.conn
    return (c.execute("SELECT id,status,retired FROM lexicon ORDER BY id").fetchall(),
            c.execute("SELECT term,guid,active FROM lexicon_adopter ORDER BY term,guid").fetchall(),
            c.execute("SELECT id,status FROM memory ORDER BY id").fetchall())


def meta(h, key):
    h.store.conn.commit()
    return h.store.get_meta(key)


def run(tmp_path, *args):
    return lexicon_main(["clean-junk", "--data-dir", str(tmp_path), *args])


@pytest.fixture
def lex(h):
    ids = {word: stored(h, word) for word in ("you", "the", "zugzug")}
    ids["nick"] = stored(h, "Big A", kind="nickname", target=101, adopters=(102,))
    return ids


def snapshots(tmp_path):
    return sorted(p.name for p in (tmp_path / "snapshots").glob("mind-pre-clean-junk*.db"))


def test_dry_run_counts_and_samples_and_writes_nothing(h, tmp_path, lex, capsys):
    before = state(h)
    assert run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "retire 2, keep 1" in out and "common 2" in out
    assert "adopters to set inactive: 6 (3 members)" in out
    assert "'Picked up' memories to archive: 4 (2 members)" in out
    assert "the (3), you (3)" in out and "keep (adopters): zugzug (3)" in out
    assert "dry run, nothing written" in out and "Picked up" not in out.split("memories")[1]
    assert state(h) == before and snapshots(tmp_path) == []


def test_go_retires_only_junk_with_a_snapshot_and_a_receipt(h, tmp_path, lex):
    assert run(tmp_path, "--go") == 0
    terms, adopters, _ = state(h)
    status = {row["id"]: (row["status"], row["retired"]) for row in terms}
    for word in ("you", "the"):
        assert status[lex[word]][0] == "dormant" and status[lex[word]][1] is not None
    assert status[lex["zugzug"]] == ("active", None) and status[lex["nick"]] == ("active", None)
    active = {(row["term"], row["guid"]) for row in adopters if row["active"]}
    assert active == {(lex[w], g) for w in ("zugzug",) for g in (101, 102, 103)} | {(lex["nick"], 101),
                                                                                (lex["nick"], 102)}
    copies = {m.key: m.status for g in (102, 103) for m in h.rows(g)}
    assert copies == {"slang:you": "archived", "slang:the": "archived", "slang:zugzug": "active",
                      "nick:101": "active"}
    receipt = json.loads(meta(h, f"{junk_slang.RECEIPT}1"))
    assert [t["term"] for t in receipt["terms"]] == ["you", "the"] and receipt["undone_at"] is None
    assert receipt["snapshot"] in snapshots(tmp_path) and len(snapshots(tmp_path)) == 1
    assert h.count("lexicon") == 4 and h.count("memory") == 7  # status changes only


def test_go_twice_retires_nothing_more(h, tmp_path, lex, capsys):
    assert run(tmp_path, "--go") == 0
    after = state(h)
    assert run(tmp_path, "--go") == 0
    assert "nothing to retire; nothing written" in capsys.readouterr().out
    assert state(h) == after and len(snapshots(tmp_path)) == 1
    assert meta(h, f"{junk_slang.RECEIPT}2") is None


def test_undo_puts_back_exactly_what_it_changed(h, tmp_path, lex, capsys):
    before = state(h)
    assert run(tmp_path, "--go") == 0
    retired = state(h)
    assert run(tmp_path, "--undo", "1") == 0  # a dry run of the undo
    assert "restore 2 terms (0 changed since, left alone), 6 adopters, 4 memories" in capsys.readouterr().out
    assert state(h) == retired
    assert run(tmp_path, "--undo", "1", "--go") == 0
    assert state(h) == before
    assert json.loads(meta(h, f"{junk_slang.RECEIPT}1"))["undone_at"] is not None
    assert run(tmp_path, "--undo", "1", "--go") == 0
    assert "already undone" in capsys.readouterr().out and state(h) == before
    assert run(tmp_path, "--undo", "9", "--go") == 1


def test_undo_leaves_alone_what_changed_since(h, tmp_path, lex, capsys):
    assert run(tmp_path, "--go") == 0
    with h.store.transaction():
        h.store.conn.execute("UPDATE memory SET status='rejected' WHERE key='slang:you' AND owner=102")
        h.store.conn.execute("UPDATE lexicon SET status='vetoed' WHERE id=?", (lex["the"],))
    assert run(tmp_path, "--undo", "1", "--go") == 0
    assert "restore 1 terms (1 changed since, left alone), 6 adopters, 3 memories (1 changed since"         in capsys.readouterr().out
    status = {row["id"]: row["status"] for row in state(h)[0]}
    assert status[lex["you"]] == "active" and status[lex["the"]] == "vetoed"
    assert {m.key: m.status for m in h.rows(102)}["slang:you"] == "rejected"


def test_go_and_undo_refuse_while_the_mind_holds_its_lock(h, tmp_path, lex, capsys):
    before = state(h)
    with acquire_instance_lock(tmp_path):
        assert run(tmp_path) == 0  # the dry run only reads
        assert run(tmp_path, "--go") == 1
        assert "refused: mind service is running" in capsys.readouterr().out
        assert run(tmp_path, "--undo", "1", "--go") == 1
    assert state(h) == before and snapshots(tmp_path) == []
    assert run(tmp_path, "--go") == 0
    with acquire_instance_lock(tmp_path):
        assert run(tmp_path, "--undo", "1", "--go") == 1
    assert json.loads(meta(h, f"{junk_slang.RECEIPT}1"))["undone_at"] is None


def test_an_untagged_early_copy_is_archived_too(h, tmp_path):
    ident = stored(h, "want", adopters=())
    early = h.mem(102, "Picked up 'want' from Arkon.", kind="reflection", source="chat", key="slang:want",
                  evidence=("chat:1",))
    assert run(tmp_path, "--go") == 0
    assert h.store.get_memories([early])[0].status == "archived"
    assert json.loads(meta(h, f"{junk_slang.RECEIPT}1"))["terms"][0]["id"] == ident


def test_an_older_schema_is_refused(tmp_path, capsys):
    conn = sqlite3.connect(tmp_path / "mind.db")
    conn.execute("CREATE TABLE lexicon(id INTEGER PRIMARY KEY, kind TEXT, term TEXT, status TEXT, originator INT)")
    conn.close()
    assert run(tmp_path) == 1
    assert "older than the code" in capsys.readouterr().out


def test_spread_never_revives_or_spreads_everyday_slang(h, monkeypatch):
    for n, guid in enumerate((101, 102, 103)):
        h.say(h.T + n, guid, "you zug")
    ctx = h.ctx()
    with monkeypatch.context() as m:  # the old stage: every word with three speakers promoted
        m.setattr(vocab, "junk_reason", lambda *a, **k: None)
        vocab.vocab(ctx)
        vocab.slang(ctx)
    ids = {r["term"]: r["id"] for r in h.store.conn.execute("SELECT id,term FROM lexicon WHERE kind='slang'")}
    assert set(ids) == {"you", "zug"}
    h.say(h.T + 10, 104, "you zug")
    lexicon.spread(ctx)
    uses = dict(h.store.conn.execute("SELECT term,uses FROM lexicon WHERE kind='slang'").fetchall())
    assert uses["zug"] > 0 and uses["you"] == 0
    assert h.store.conn.execute("SELECT 1 FROM lexicon_adopter WHERE term=? AND guid=104",
                                (ids["you"],)).fetchone() is None
