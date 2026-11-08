import json
import logging
import sqlite3
from dataclasses import replace

import pytest

from conftest import make_mem
from leeroy_mind.__main__ import main
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.nightly import critic
from leeroy_mind.nightly.forget import _node
from leeroy_mind.storage import provenance as P, repair_provenance as R


def memory(store, *, evidence=("owner:test",), key=None):
    return store.insert_memory(make_mem(owner=101, text="Synthetic memory text.",
                                        evidence=evidence, key=key))


def chat(store, memories, *, origin="mind", dropped=None, to=0):
    text = "Synthetic line about the raid."
    with store.transaction():
        ident = store.conn.execute("INSERT INTO chat_line(ts,channel,from_guid,from_name,"
            "to_guid,text,origin) VALUES (1000,'guild',101,'Arkon',?,?,?)",
            (to, text, origin)).lastrowid
    if origin == "mind":
        store.log_utterance(1000, 101, "guild", "open", text, memories, dropped, None,
                            to_guid=to)
    return f"chat:{ident}"


def chain(store, ref):
    """story -> fact -> retained joke source -> chat -> recalled memory."""
    with store.transaction():
        joke = store.conn.execute("INSERT INTO lexicon(kind,term,origin_ref,originator,"
            "born,last_used) VALUES ('joke','synthetic term','owner:origin',101,1000,1000)"
        ).lastrowid
        store.set_meta(f"nightly:lexicon-sources:{joke}", json.dumps([ref]))
        fact = store.conn.execute("INSERT INTO fact(kind,root,created) VALUES "
            "('noticed',?,1000)", (f"joke:{joke}",)).lastrowid
        story = store.conn.execute("INSERT INTO story(title,summary,evidence,t_created) "
            "VALUES ('Synthetic title','Synthetic summary',?,1000)",
            (json.dumps([f"rule:fact:{fact}"]),)).lastrowid
    return f"story:{story}"


def evidence(store, ident):
    return store.get_memories([ident])[0].evidence


@pytest.mark.parametrize("long", [False, True])
@pytest.mark.parametrize("writer", ["merge", "keyed"])
def test_writers_drop_full_graph_cycles_and_keep_clean_references(store, long, writer):
    ident = memory(store, key="synthetic")
    back = chat(store, [ident])
    if long:
        back = chain(store, back)
    refs = (back, "owner:other", f"memory:{ident}")
    if writer == "merge":
        store.merge_memory(ident, refs, 7)
        assert evidence(store, ident) == ("owner:test", "owner:other")
    else:
        row = store.get_memories([ident])[0]
        assert store.upsert_keyed_memory(replace(row, evidence=refs)) == ident
        assert evidence(store, ident) == ("owner:other",)
    assert R.Graph(store.conn).counts() == R.Counts(0, 0)


def test_guard_follows_rejected_memories_and_dropped_pairs(store):
    ident = memory(store)
    other = memory(store, evidence=(f"memory:{ident}",))
    with store.transaction():
        store.conn.execute("UPDATE memory SET status='rejected' WHERE id=?", (other,))
    ref = chat(store, [other], dropped="synthetic")
    store.merge_memory(ident, [ref], 5)
    assert evidence(store, ident) == ("owner:test",)


def test_clean_critic_graph_keeps_exact_comparable_values(store):
    ident = memory(store)
    ref = chain(store, chat(store, [ident]))
    snapshot = dict(critic._provenance(store, [ref]))
    assert set(snapshot) == {ref, "rule:fact:1", "joke:1", "chat:1",
                             f"memory:{ident}", "owner:origin", "owner:test"}
    for key, result in snapshot.items():
        value, children, holder, scope, parties = _node(store, key)
        extras = ()
        if key == "chat:1":
            line = store.conn.execute("SELECT * FROM chat_line WHERE id=1").fetchone()
            pair = critic._paired(store, line)
            children = (f"memory:{ident}",)
            extras = (tuple(line), tuple(pair))
        elif key == "joke:1":
            children = ("owner:origin", "chat:1")
            extras = (json.dumps(["chat:1"]),)
        assert result == (value, tuple(children), holder, scope, tuple(sorted(parties)), extras)


@pytest.mark.parametrize("kind", ["unpaired", "dropped"])
def test_critic_still_rejects_untraceable_output(store, kind):
    ref = chat(store, [], dropped="synthetic" if kind == "dropped" else None)
    if kind == "unpaired":
        with store.transaction():
            store.conn.execute("DELETE FROM utterance")
    with pytest.raises(critic.ProvenanceError, match="Unpaired"):
        critic._provenance(store, [ref])


def test_budget_exhaustion_is_not_proof_of_safety_and_logs_no_text(store, caplog):
    target = memory(store)
    source = memory(store)
    ref = chat(store, [source])
    caplog.set_level(logging.WARNING)
    assert store._cites_memory(ref, target, limit=1)
    assert "memory_evidence_held" in caplog.text and "code=bound" in caplog.text
    assert "Synthetic" not in caplog.text and "Arkon" not in caplog.text
    assert ref not in caplog.text
    with pytest.raises(P.WalkLimit):
        P.reaches(lambda r: ["owner:end"], "owner:start", "memory:99", limit=1)


def legacy_cycles(store):
    first, second = memory(store), memory(store)
    ref = chain(store, chat(store, [first]))
    with store.transaction():
        store.conn.execute("UPDATE memory SET evidence=? WHERE id=?",
            (json.dumps([f"memory:{first}", ref, "owner:keep", f"memory:{second}"]), first))
        store.conn.execute("UPDATE memory SET evidence=? WHERE id=?",
            (json.dumps([f"memory:{first}", "owner:keep2"]), second))
    return first, second


def rows(store):
    return [tuple(r) for r in store.conn.execute("SELECT * FROM memory ORDER BY id")]


def test_repair_dry_run_changes_nothing_and_prints_counts_only(store, tmp_path, capsys):
    legacy_cycles(store)
    before = rows(store)
    assert main(["db", "repair-provenance", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "rows to change 1, self refs dropped 1, cycle refs dropped 2" in output
    assert "after: self refs 0, other cycle edges 0" in output
    assert "dry run, nothing written" in output
    assert "Synthetic" not in output and "Arkon" not in output
    assert rows(store) == before
    assert not (tmp_path / "snapshots").exists()


def test_repair_go_snapshots_and_preserves_every_noncycle_reference(store, tmp_path, capsys):
    first, second = legacy_cycles(store)
    original = rows(store)
    store.conn.commit()
    assert main(["db", "--data-dir", str(tmp_path), "repair-provenance", "--go"]) == 0
    assert "written; snapshot taken" in capsys.readouterr().out
    store.conn.commit()
    assert evidence(store, first) == ("owner:keep",)
    assert evidence(store, second) == (f"memory:{first}", "owner:keep2")
    assert R.Graph(store.conn).counts() == R.Counts(0, 0)
    current = rows(store)
    index = list(store.conn.execute("SELECT * FROM memory LIMIT 0").description)
    ev = [col[0] for col in index].index("evidence")
    assert [r[:ev] + r[ev + 1:] for r in current] == [r[:ev] + r[ev + 1:] for r in original]
    snapshots = list((tmp_path / "snapshots").glob("mind-pre-repair-provenance-*.db"))
    assert len(snapshots) == 1
    with sqlite3.connect(f"{snapshots[0].as_uri()}?mode=ro", uri=True) as saved:
        assert saved.execute("SELECT * FROM memory ORDER BY id").fetchall() == original


def test_repair_refuses_while_running_but_dry_run_is_allowed(store, tmp_path, capsys):
    legacy_cycles(store)
    before = rows(store)
    with acquire_instance_lock(tmp_path):
        assert main(["db", "repair-provenance", "--data-dir", str(tmp_path), "--go"]) == 1
        assert main(["db", "repair-provenance", "--data-dir", str(tmp_path)]) == 0
    assert "mind service is running" in capsys.readouterr().out
    assert rows(store) == before
    assert not (tmp_path / "snapshots").exists()


def test_repair_never_migrates_old_schema(tmp_path, capsys):
    path = tmp_path / "mind.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE memory(id INTEGER PRIMARY KEY,evidence TEXT);"
                          "INSERT INTO memory VALUES (1,'[\"memory:1\",\"owner:keep\"]');"
                          "PRAGMA user_version=1;")
    original = path.read_bytes()
    assert main(["db", "repair-provenance", "--data-dir", str(tmp_path)]) == 0
    assert "self refs dropped 1" in capsys.readouterr().out
    assert path.read_bytes() == original


def test_verification_failure_rolls_back_all_writes(store, tmp_path, monkeypatch, capsys):
    legacy_cycles(store)
    before = rows(store)
    store.conn.commit()
    real_counts = R.Graph.counts
    calls = 0

    def fail_verification(graph):
        nonlocal calls
        calls += 1
        return R.Counts(1, 0) if calls == 3 else real_counts(graph)

    monkeypatch.setattr(R.Graph, "counts", fail_verification)
    assert main(["db", "repair-provenance", "--data-dir", str(tmp_path), "--go"]) == 1
    store.conn.commit()
    assert rows(store) == before
    assert "nothing written" in capsys.readouterr().out
