"""Invented cast and FakeLLM only: bounded automatic persona generation."""
import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import run
from test_generate import _persona_json
from leeroy_mind.app import MindApp
from leeroy_mind.config import CastCfg, load_config
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.pipeline import STAGES
from leeroy_mind.nightly.runner import NightRunner, Stage
from leeroy_mind.personas import newcomers as N
from leeroy_mind.personas.attach import attach_personas
from leeroy_mind.personas.io import load_persona
from leeroy_mind.testing.fake_llm import FakeLLM, script_from

CFG = CastCfg(0.7, 0.7)
NAMES = ("Nolan Alder", "Petra Flint", "Silas Wren", "Tamsin Vale", "Ulric Moss")


def missing(h, *guids):
    with h.store.transaction():
        h.store.conn.executemany("DELETE FROM persona_core WHERE guid=?", [(g,) for g in guids])


def generator(*names):
    return FakeLLM(script_from([reply for name in names for reply in ("synthetic profile", _persona_json(name))]))


def context(h, *names, night="2026-10-29", cap=6):
    ctx = h.ctx(night=night)
    ctx.settings = replace(ctx.settings, newcomers_per_night=cap)
    ctx.reasoner.llm = generator(*(names or NAMES))
    return ctx


def generate(ctx):
    return N.make_stage(lambda: CFG)(ctx)


def test_newcomer_uses_generator_source_screens_budget_and_refreshes_night(h, caplog):
    missing(h, 101)
    ctx = context(h, NAMES[0])
    assert 101 not in ctx.members()  # budgeting/earlier readers can cache this
    ctx.women()
    with caplog.at_level(logging.INFO):
        run(generate(ctx))
    p = load_persona(h.store, 101)
    assert p is not None and p.identity.age >= 25
    assert ctx.done(N.STAGE, "101") and 101 in ctx.members()
    assert ctx.persona(101) == p and p.guid == 101 and p.character_name == "Arkon"
    assert h.store.conn.execute("SELECT source FROM persona_core WHERE guid=101").fetchone()[0] == "generated"
    assert ctx.reasoner.calls == 2 and len(ctx.reasoner.llm.calls) == 2
    assert all(call["model"] == ctx.settings.reflection_model for call in ctx.reasoner.llm.calls)
    assert budget.UNIT_CALLS[N.STAGE] == {"think": 1, "json": 1}
    assert N.STAGE in budget.EXTENSION_STAGES
    assert any(c.setting == "newcomers_per_night" for d in budget.DEGRADE for c in d.cuts)
    assert "newcomer_created" in caplog.text
    assert not any(text in caplog.text for text in (*NAMES, "Arkon", "synthetic profile"))


def test_cap_survives_resume_and_later_eligible_members(h):
    missing(h, 101, 102, 103, 104, 105)
    ctx = context(h, *NAMES, cap=2)
    assert N.count(ctx) == 2 and h.settings.newcomers_per_night == 6
    run(generate(ctx))
    assert set(h.store.persona_versions()) == {101, 102}
    fresh = context(h, *NAMES[2:], cap=2)
    run(generate(fresh))
    assert set(h.store.persona_versions()) == {101, 102}
    assert fresh.reasoner.calls == 0
    tomorrow = context(h, *NAMES[2:], night="2026-10-30", cap=2)
    run(generate(tomorrow))
    assert set(h.store.persona_versions()) == {101, 102, 103, 104}


def test_owner_low_level_and_departed_members_are_never_generated(h):
    missing(h, 101, 104, 105)
    with h.store.transaction():
        h.store.conn.execute("UPDATE cast_member SET level=79 WHERE guid=104")
        h.store.conn.execute("DELETE FROM cast_member WHERE guid=105")
    ctx = context(h, NAMES[0])
    run(generate(ctx))
    assert set(h.store.persona_versions()) == {101, 102, 103}
    assert json.loads(h.store.get_meta(N.PLAN + ctx.night)) == [101]
    assert not ctx.done(N.STAGE, "1")


def test_unknown_owner_and_zero_cap_make_no_calls(h):
    missing(h, 101)
    for ctx in (context(h, cap=0), replace(context(h, night="2026-10-30"), ident=replace(h.ident, guid=0))):
        run(generate(ctx))
        assert ctx.reasoner.calls == 0
    assert load_persona(h.store, 101) is None


def test_refusal_counts_distinct_failed_nights_then_gives_up(h, caplog):
    missing(h, 101)
    with caplog.at_level(logging.INFO):
        for day in (29, 30, 31):
            ctx = context(h, night=f"2026-10-{day}")
            ctx.reasoner.llm = FakeLLM(lambda *_: "PRIVATE_REPLY")
            run(generate(ctx))
            attempts = ctx.reasoner.calls
            run(generate(context(h, night=ctx.night)))
            assert ctx.reasoner.calls == attempts
            assert ctx.done(N.STAGE, "101") and load_persona(h.store, 101) is None
            assert json.loads(h.store.get_meta(N.STATE + "101"))["failed_nights"] == day - 28
        final = context(h, night="2026-11-01")
        assert N.count(final) == 0
        run(generate(final))
        assert final.reasoner.calls == 0
    assert caplog.text.count("newcomer_gave_up") == 1
    assert "PRIVATE_REPLY" not in caplog.text and "Arkon" not in caplog.text


def test_cancellation_preserves_selection_but_does_not_charge_a_failed_night(h):
    missing(h, 101, 102)

    async def scenario():
        ctx = context(h, cap=1)
        gate = asyncio.Event()
        ctx.reasoner.llm = FakeLLM(lambda *_: "never", gate=gate)
        task = asyncio.create_task(generate(ctx))
        for _ in range(100):
            if ctx.reasoner.llm.inflight:
                break
            await asyncio.sleep(0)
        assert ctx.reasoner.llm.inflight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert h.store.get_meta(N.STATE + "101") is None
        assert not ctx.done(N.STAGE, "101")
        retry = context(h, NAMES[0], cap=1)
        await generate(retry)
        assert load_persona(h.store, 101) is not None and load_persona(h.store, 102) is None
    run(scenario())


def test_manual_writer_during_generation_wins(h):
    missing(h, 101)
    ctx = context(h, NAMES[0])
    script = ctx.reasoner.llm._script

    def write_by_hand(model, messages):
        h.persona(101)
        return script(model, messages)
    ctx.reasoner.llm._script = write_by_hand
    run(generate(ctx))
    assert load_persona(h.store, 101).identity.real_name != NAMES[0]
    assert h.store.conn.execute("SELECT source FROM persona_core WHERE guid=101").fetchone()[0] == "seed"
    assert ctx.done(N.STAGE, "101")


def test_corrupt_retry_state_is_not_an_unlimited_retry(h):
    missing(h, 101)
    h.store.set_meta(N.STATE + "101", "not json")
    ctx = context(h)
    assert N.count(ctx) == 0
    run(generate(ctx))
    assert ctx.reasoner.calls == 0


def test_shared_real_name_screen_refuses_private_content_without_logging_it(h, caplog):
    missing(h, 101)
    ctx = context(h)
    bad = _persona_json(NAMES[0], secret="Quillon PRIVATE_REPLY")
    ctx.reasoner.llm = FakeLLM(lambda _, messages: bad if "matching this schema" in messages[-1]["content"] else "profile")
    with caplog.at_level(logging.INFO):
        run(generate(ctx))
    assert load_persona(h.store, 101) is None and ctx.done(N.STAGE, "101")
    assert ctx.failures[N.STAGE] == 1
    assert not any(text in caplog.text for text in ("Quillon", "PRIVATE_REPLY", NAMES[0], "Arkon"))


def test_shared_name_collision_retry_logs_only_guid(h, caplog):
    missing(h, 101)
    clash = load_persona(h.store, 102).identity.real_name
    ctx = context(h)
    ctx.reasoner.llm = FakeLLM(script_from(["profile", _persona_json(clash), _persona_json(clash),
                                          json.dumps({"names": [NAMES[0]]})]))
    with caplog.at_level(logging.INFO):
        run(generate(ctx))
    assert load_persona(h.store, 101).identity.real_name == NAMES[0]
    assert "persona_renamed" in caplog.text and "guid=101" in caplog.text
    assert not any(text in caplog.text for text in (clash, NAMES[0], "Arkon"))


def test_persona_relationship_and_unit_writes_are_atomic(h, monkeypatch, caplog):
    missing(h, 101)
    ctx = context(h)

    def failed(*_, **__):
        raise ValueError("PRIVATE_REPLY")
    monkeypatch.setattr(N, "seed_relationships", failed)
    with caplog.at_level(logging.INFO):
        run(generate(ctx))
    assert load_persona(h.store, 101) is None and not ctx.done(N.STAGE, "101")
    assert "persist_failed" in caplog.text and "PRIVATE_REPLY" not in caplog.text


@pytest.mark.parametrize("text,enabled", [
    ("", False), ("[personas]\nauto_newcomers=false\n", False),
    ("[personas]\nauto_newcomers=true\n", True),
    ('[personas]\nauto_newcomers="true"\n', False),
    ("personas=1\n", False), ("[personas]\nauto_newcomers=true\nunknown=1\n", False),
])
def test_config_is_opt_in_and_stage_precedes_persona_readers(tmp_path, text, enabled):
    (tmp_path / "mind.toml").write_text(text, encoding="utf-8")
    cfg = load_config(tmp_path / "mind.toml")
    app = SimpleNamespace(data_dir=tmp_path, cfg=cfg, night_runner=NightRunner(STAGES, None))
    assert attach_personas(app).auto_newcomers is enabled
    names = [s.name for s in app.night_runner.stages]
    if enabled:
        assert names[:4] == ["snapshot", "newcomers", "threads", "confide"]
    else:
        assert tuple(app.night_runner.stages) == STAGES


def test_empty_newcomer_stage_does_not_swap_the_live_model(h):
    ctx = context(h)
    ctx.reasoner.llm.set_models({h.REFL, h.LIVE, h.EMB})

    async def no_op(_):
        pass
    runner = NightRunner([Stage(N.STAGE, N.make_stage(lambda: CFG), "reflection", N.count),
                          Stage("threads", no_op, "live")], lambda *_: ctx)
    run(runner.run(ctx.night, ctx.window))
    assert not any(call[:2] == ("preload", h.REFL) for call in ctx.arbiter.admin.calls)
    assert ctx.reasoner.calls == 0


def test_next_login_uses_new_persona_without_restart(h, monkeypatch):
    missing(h, 101)
    (h.settings.data_dir / "mind.toml").write_text("[personas]\nauto_newcomers=true\n", encoding="utf-8")
    app = MindApp(load_config(h.settings.data_dir / "mind.toml"), h.settings.data_dir,
                  llm=generator(NAMES[0]), clock=h.clock)

    async def scenario():
        try:
            assert app.personas.get(101) is None
            ctx = app.night_runner.make_ctx("2026-10-29", (1, 2))
            stage = next(s for s in app.night_runner.stages if s.name == N.STAGE)
            await stage.fn(ctx)
            assert load_persona(app.store, 101) is not None
            seen = []

            async def started(_):
                seen.append(app.engine.personas.get(101))
            monkeypatch.setattr(app.engine, "start_session", started)
            app._model_known = True
            await app._start_session(h.T + 86400)
            assert seen and seen[0].guid == 101
        finally:
            await app.stop()
    run(scenario())
