"""`personas clean-contamination` (identity audit, 2026-11-15): what members' echoes of his words,
lines contradicting their persona and gender slips left in their stored state is retired, never
deleted, with a receipt the run can be undone from. Invented members and lines only."""
import json

import pytest

from conftest import make_mem, seed_test_cast, unit
from engine_rig import save_member
from leeroy_mind.__main__ import main
from leeroy_mind.claims.store import add
from leeroy_mind.personas import contamination as C
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import pack_vec

HIS = "single and no kids. five and eight are good ages, youre a lucky woman"


def line(store, who, text, *, channel="whisper", to=1):
    return store.insert_chat_line(Chat(0, channel, who, f"Member{who}", to, text, 0, 0),
                                  session_id=None, origin="human" if who == 1 else "mind")


@pytest.fixture
def stained(store, clock):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", household="Lives with her husband and a terrier")
    store.set_meta("human_guid", "1")
    now = clock.now()
    ids = {"his": line(store, 1, HIS, to=102)}
    ids["echo"] = line(store, 102, "single, no kids. five and eight are good ages, youre a lucky woman")
    ids["kids"] = line(store, 102, "two girls. five and eight")
    ids["ok"] = line(store, 102, "i love the saurfang fight", channel="guild", to=0)
    v = pack_vec(unit(0))
    ids["c_echo"] = add(store, 102, kind="opinion", subject="honesty", polarity=1, embedding=v,
                        text="appreciates honesty about life", source_line=ids["echo"])
    ids["c_kids"] = add(store, 102, kind="fact", subject="children", polarity=0, embedding=v,
                        text="has two daughters aged five and eight", source_line=ids["kids"])
    ids["c_ok"] = add(store, 102, kind="opinion", subject="saurfang", polarity=1, embedding=v,
                      text="loves the saurfang fight", source_line=ids["ok"])
    with store.transaction():
        ids["disc"] = store.conn.execute(
            "INSERT INTO disclosure(guid, other, depth, response, layer, ts, ref) VALUES "
            "(102, 1, 2, 'responsive', NULL, ?, ?)", (now, f"chat:{ids['kids']}")).lastrowid
        for guid, text in ((101, "She keeps it short."), (102, "She keeps it short.")):
            store.conn.execute("INSERT INTO talk_lesson(guid, scope, text, status, reinforced, "
                               "expires, origin_night) VALUES (?, 'guild', ?, 'active', 0, ?, "
                               "'2026-10-28')", (guid, text, now + 400 * 86400))  # the CLI reads
            # the wall clock, the fixture a fake October one
    mem = lambda text, evidence, source="extract": store.insert_memory(make_mem(  # noqa: E731
        owner=102, kind="conversation", text=text, source=source,
        evidence=tuple(f"chat:{x}" for x in evidence)))
    ids["m_kids"] = mem("I told Zan about my two daughters", [ids["kids"]])
    ids["m_derived"] = mem("Told Zan something real.", [ids["kids"], ids["his"]], "reflection")
    ids["m_slip"] = mem("I told Zan I'm not some nice gentleman", [ids["ok"]])
    ids["m_ok"] = mem("The saurfang fight went well", [ids["ok"]])
    # his own memory of her, which reads like a slip: never retired (and it names no family)
    ids["m_owner"] = mem("I told Zan I'm a real gentleman about it", [ids["ok"]], "owner")
    return ids


def _state(store, ids):
    q = store.conn.execute
    return {
        "claims": [q("SELECT superseded_by FROM member_claim WHERE id=?", (ids[k],)).fetchone()[0]
                   for k in ("c_echo", "c_kids", "c_ok")],
        "vecs": q("SELECT count(*) FROM member_claim_vec").fetchone()[0],
        "disc": tuple(q("SELECT ref, response FROM disclosure WHERE id=?", (ids["disc"],)).fetchone()),
        "mems": [q("SELECT status FROM memory WHERE id=?", (ids[k],)).fetchone()[0]
                 for k in ("m_kids", "m_derived", "m_slip", "m_ok", "m_owner")],
        "lessons": [r[0] for r in q("SELECT expires FROM talk_lesson ORDER BY id")],
    }


def test_the_plan_finds_echoes_inventions_slips_and_misgendered_lessons(store, clock, stained):
    plan = C.plan(store.conn, human=1, now=clock.now())
    got = {(i.kind, i.id): i.why for i in plan.items}
    assert got == {("claim", stained["c_echo"]): "echo", ("claim", stained["c_kids"]): "persona:kids",
                   ("disclosure", stained["disc"]): "persona",
                   ("memory", stained["m_kids"]): "persona:kids",
                   ("memory", stained["m_derived"]): "evidence",
                   ("memory", stained["m_slip"]): "slip", ("lesson", 1): "misgendered"}
    assert plan.lines[stained["echo"]] == "echo" and stained["ok"] not in plan.lines


def test_apply_retires_never_deletes_and_undo_puts_it_back(store, clock, stained):
    before = _state(store, stained)
    plan = C.plan(store.conn, human=1, now=clock.now())
    with store.transaction():
        receipt = C.apply(store.conn, plan, now=clock.now(), stamp="t1")
    after = _state(store, stained)
    assert after["claims"] == [stained["c_echo"], stained["c_kids"], None]  # retired by itself
    assert after["vecs"] == before["vecs"] - 2
    assert after["disc"] == (f"retired:chat:{stained['kids']}", "none")
    assert after["mems"] == ["archived", "archived", "archived", "active", "active"]
    assert after["lessons"] == [clock.now(), before["lessons"][1]]
    assert len(receipt["memories"]) == 3 and store.get_meta(C.RECEIPT + "t1")
    assert store.conn.execute("SELECT count(*) FROM member_claim").fetchone()[0] == 3
    assert C.plan(store.conn, human=1, now=clock.now()).items == []
    with store.transaction():
        done = C.undo(store.conn, "t1", now=clock.now())
    assert done == {"claims": 2, "disclosures": 1, "memories": 3, "lessons": 1}
    assert _state(store, stained) == before
    with pytest.raises(ValueError), store.transaction():
        C.undo(store.conn, "t1", now=clock.now())
    with pytest.raises(KeyError), store.transaction():
        C.undo(store.conn, "nope", now=clock.now())
    assert json.loads(store.get_meta(C.RECEIPT + "t1"))["undone"] == clock.now()


def test_the_cli_plans_read_only_and_prints_no_text(store, stained, tmp_path, capsys):
    store.conn.commit()
    before = _state(store, stained)
    assert main(["personas", "clean-contamination", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "would retire claim 2, disclosure 1, memory 3, lesson 1" in out
    assert "nothing was written" in out and "daughters" not in out and "gentleman" not in out
    assert _state(store, stained) == before
    assert main(["personas", "clean-contamination", "--data-dir", str(tmp_path), "--show"]) == 0
    assert "daughters" in capsys.readouterr().out


def test_the_cli_go_snapshots_retires_and_undoes(store, stained, tmp_path, capsys):
    store.conn.commit()
    before = _state(store, stained)
    assert main(["personas", "clean-contamination", "--data-dir", str(tmp_path), "--go"]) == 0
    out = capsys.readouterr().out
    assert "snapshot" in out and "retired: claims 2, disclosures 1, memories 3, lessons 1" in out
    stamp = out.rsplit("--undo ", 1)[1].split()[0]
    assert list((tmp_path / "snapshots").glob("mind-pre-clean-contamination-*.db"))
    store.conn.rollback()  # a fresh read snapshot
    assert _state(store, stained) != before
    assert main(["personas", "clean-contamination", "--data-dir", str(tmp_path), "--undo", stamp]) == 0
    assert "put back: claims 2, disclosures 1, memories 3, lessons 1" in capsys.readouterr().out
    store.conn.rollback()
    assert _state(store, stained) == before
    assert main(["personas", "clean-contamination", "--data-dir", str(tmp_path),
                 "--go", "--undo", stamp]) == 2


def test_the_owners_claims_and_edits_are_never_retired(store, clock, stained):
    """Review 323 P3-2: a claim of source owner is his; a godmode implant that gives her kids
    makes her kids hers."""
    owned = add(store, 102, kind="fact", subject="children", polarity=0, embedding=pack_vec(unit(0)),
                text="has two daughters aged five and eight", source="owner")
    plan = C.plan(store.conn, human=1, now=clock.now())
    assert ("claim", owned) not in {(i.kind, i.id) for i in plan.items}
    store.insert_memory(make_mem(owner=102, kind="event", source="owner",
                                 text="She has two daughters, five and eight",
                                 evidence=("owner:godmode:3",)))
    plan = C.plan(store.conn, human=1, now=clock.now())
    kinds = {(i.kind, i.id) for i in plan.items}
    assert ("claim", stained["c_kids"]) not in kinds and ("memory", stained["m_kids"]) not in kinds
    assert ("claim", stained["c_echo"]) in kinds  # an echo is still his words


def test_undo_leaves_a_lesson_the_lab_changed_since(store, clock, stained):
    """Review 323 P3-5: a lesson vetoed or expired again after the run stays as the Lab left it."""
    plan = C.plan(store.conn, human=1, now=clock.now())
    with store.transaction():
        C.apply(store.conn, plan, now=clock.now(), stamp="t2")
        store.conn.execute("UPDATE talk_lesson SET expires=? WHERE id=1", (clock.now() - 5,))
    with store.transaction():
        done = C.undo(store.conn, "t2", now=clock.now())
    assert done["lessons"] == 0
    assert store.conn.execute("SELECT expires FROM talk_lesson WHERE id=1").fetchone()[0] == clock.now() - 5
