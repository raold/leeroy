"""A promoted lab arm in live talk: drawn before the prompt is composed, its text in force for its
piece, credited only where the piece was rendered; nothing at all without one."""
import pytest

from engine_rig import make_rig
from leeroy_mind.lab import live as lab_live, store as lab_store
from leeroy_mind.learn import arms, bandit, control
from leeroy_mind.learn.config import LearnCfg
from leeroy_mind.talk import pieces
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

ON = LearnCfg(enabled=True, bandit=True, apply=True)
MARK = "Plainly respond to what"
FAMILY = "prompt.reply_human"


@pytest.fixture
def rig(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    r.engine.learn_enabled = True
    r.engine.learn_settings = ON
    yield r
    r.close()


def lab_arm(rig, *, status="active"):
    text = pieces.builtin("reply_human").replace("Respond to what", MARK, 1)
    with rig.store.transaction():
        conn = rig.store.conn
        arms.ensure(conn)
        rid = lab_store.create_run(conn, night="n", source="night", piece="reply_human", seed="s",
                                   plan={"pairs": []}, now=1.0)
        vid = lab_store.add_variant(conn, rid, "reply_human", text, "d", 1.0)
        aid = f"lab.reply_human.v{vid}"
        conn.execute("INSERT INTO learn_arm VALUES (?,?,?,?,?,?,?)",
                     (arms.control_of(FAMILY), FAMILY, '{"mode": "control"}', 1, "control", "builtin", 1.0))
        conn.execute("INSERT INTO learn_arm VALUES (?,?,?,?,?,?,?)",
                     (aid, FAMILY, f'{{"piece": "reply_human", "variant": {vid}}}', 1, status, "coach", 1.0))
    return aid


def compose(rig, kind=TurnKind.REPLY_HUMAN, channel="guild"):
    turn = Turn(kind, 101, channel, to_guid=1 if channel == "whisper" else 0, addressee=1, max_lines=1)
    meta = TurnMeta(rig.engine._new_id())
    composed = rig.sync(rig.engine._compose(turn, meta, live=True))
    return meta, composed


def force(monkeypatch, which):
    def choose_pieces(conn, guid, context, families, **kwargs):
        context.key  # a valid context, as the real draw needs
        return {f: which(f) for f in families if f == FAMILY}
    monkeypatch.setattr(bandit, "choose_pieces", choose_pieces)


def test_a_drawn_lab_arm_is_in_her_prompt_and_in_the_recipe(rig, monkeypatch, caplog):
    aid = lab_arm(rig)
    force(monkeypatch, lambda f: aid)
    caplog.set_level("INFO", logger="leeroy_mind")
    meta, c = compose(rig)
    assert MARK in c.messages[-1]["content"]
    assert meta.lab.selected == {FAMILY: aid} and "reply_human" in meta.lab.rendered
    assert meta.learning.selected[FAMILY] == aid
    assert aid in meta.recipe_snapshot.arm_ids
    assert any("lab_piece: guid=101 piece=reply_human source=arm" in r.getMessage() for r in caplog.records)


def test_the_control_draw_reads_the_builtin_wording_and_credits_the_control(rig, monkeypatch):
    lab_arm(rig)
    force(monkeypatch, lambda f: arms.control_of(f))
    meta, c = compose(rig)
    assert MARK not in c.messages[-1]["content"]
    assert meta.learning.selected[FAMILY] == arms.control_of(FAMILY)
    assert arms.control_of(FAMILY) in meta.recipe_snapshot.arm_ids


def test_a_turn_that_renders_no_lab_piece_draws_nothing(rig, monkeypatch):
    lab_arm(rig)
    force(monkeypatch, lambda f: pytest.fail("no draw for a turn without the piece"))
    meta, c = compose(rig, TurnKind.CONTINUE)
    assert meta.lab is None
    assert not any(arms.is_prompt(f) for f in meta.learning.selected)


def test_without_a_lab_arm_nothing_is_drawn_or_written(rig, monkeypatch):
    monkeypatch.setattr(bandit, "choose_pieces", lambda *a, **k: pytest.fail("no draw"))
    meta, c = compose(rig)
    assert meta.lab is None and MARK not in c.messages[-1]["content"]
    assert set(meta.learning.selected) == set(arms.FAMILIES)


def test_a_piece_drawn_but_never_rendered_leaves_the_recipe(rig):
    meta = TurnMeta("t")
    pick = lab_live.Pick(selected={"prompt.beat_ask": "lab.beat_ask.v1", FAMILY: "x"},
                         rendered={"reply_human"})

    class Applied:
        selected = {**arms.controls(), "prompt.beat_ask": "lab.beat_ask.v1", FAMILY: "x"}
    meta.learning = Applied()
    lab_live.settle(meta, pick)
    assert "prompt.beat_ask" not in meta.learning.selected and meta.learning.selected[FAMILY] == "x"


def test_the_real_draw_explores_a_new_arm_within_the_budget_and_freezes_hold(rig):
    aid = lab_arm(rig)
    persona = rig.engine.personas.get(101)
    turn = Turn(TurnKind.REPLY_HUMAN, 101, "guild", addressee=1, max_lines=1)
    from leeroy_mind.learn import apply
    context = apply.context_for(rig.engine, turn, persona, ON)
    seen = set()
    with rig.store.transaction():
        conn = rig.store.conn
        conn.execute("INSERT INTO bandit_turn VALUES (101, 50, 0)")
        for _ in range(40):
            got = bandit.choose_pieces(conn, 101, context, [FAMILY, "prompt.continue"],
                                       rng=rig.engine.rng)
            assert set(got) == {FAMILY}  # no active arm for continue: not drawn
            seen.add(got[FAMILY])
        shares = dict(conn.execute("SELECT arm_id, turns FROM bandit_share WHERE context=?",
                                   (context.key,)))
        explored = conn.execute("SELECT exploratory FROM bandit_turn WHERE guid=101").fetchone()[0]
    assert seen == {aid, arms.control_of(FAMILY)}
    assert sum(shares.values()) == 40 and explored == shares[aid] <= 10
    with rig.store.transaction():
        rig.store.set_meta(control.PREFIX + "freeze", "true")
        assert bandit.choose_pieces(rig.store.conn, 101, context, [FAMILY]) == {}


def test_families_for_follow_the_turn():
    def fams(kind, channel, to=0, addressee=0):
        return lab_live.families_for(Turn(kind, 101, channel, to_guid=to, addressee=addressee), 1)
    assert fams(TurnKind.REPLY_HUMAN, "guild", addressee=1) == [
        "prompt.reply_human", "prompt.situation_reply_human", "prompt.romance_hook"]
    assert "prompt.beat_ask" in fams(TurnKind.REPLY_HUMAN, "whisper", to=1, addressee=1)
    assert "prompt.discord_dm" in fams(TurnKind.REPLY_HUMAN, "dm:101", addressee=1)
    assert fams(TurnKind.CONTINUE, "guild") == ["prompt.continue"]
