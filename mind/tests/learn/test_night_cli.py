import asyncio
import json
import sqlite3

import pytest

from tests.claims.conftest import context
from leeroy_mind.learn import night
from leeroy_mind.learn.recipe import record as record_recipe
from leeroy_mind.config import load_config
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.learn.cli import learn_main, report
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.pipeline import STAGES
from leeroy_mind.nightly.runner import NightRunner
from .test_reward import line, signals, screened


def enable(tmp_path):
    (tmp_path / "mind.toml").write_text("[learn]\nenabled=true\n")


def test_night_selection_cap_survives_resume_and_new_backfill(context, store, clock, tmp_path, monkeypatch):
    enable(tmp_path)
    monkeypatch.setattr(night, "PER_NIGHT", 2)
    async def run():
        async with context(learn_enabled=True) as (ctx, llm):
            ctx.window = (clock.now()-3600, clock.now()+1)
            ids = [line(store, clock, ts=clock.now()-300) for _ in range(4)]
            for lid in ids:
                signals(store, lid, clock.now(), explicit=1)
                screened(store, lid, clock.now())
            await night.stage_rewards(ctx)
            first = [tuple(r) for r in store.conn.execute("SELECT * FROM talk_reward ORDER BY line_id")]
            assert len(first) == 2
            line(store, clock, ts=clock.now()-300)  # an earlier row cannot move a saved selection
            await night.stage_rewards(ctx)
            assert [tuple(r) for r in store.conn.execute("SELECT * FROM talk_reward ORDER BY line_id")] == first
            assert night.count(ctx) == 0
            assert not llm.calls
            assert ctx.done("rewards", str(ids[2])) and not ctx.done("rewards", str(ids[1]))
    asyncio.run(run())


def test_night_off_old_schema_and_unregistered_budget_no_writes(context, store, clock, tmp_path):
    async def run():
        async with context() as (ctx, _):
            ctx.window = (clock.now()-1, clock.now()+1)
            line(store, clock, ts=clock.now()-300)
            before = store.conn.total_changes
            await night.stage_rewards(ctx)
            assert store.conn.total_changes == before
            assert "rewards" not in budget._counts(ctx, set(), frozenset())
            enable(tmp_path)
            from dataclasses import replace
            ctx.settings = replace(ctx.settings, learn_enabled=True)  # restart snapshot
            await night.prepare_count(ctx)
            assert budget._counts(ctx, set(), frozenset({"rewards"}))["rewards"] == 1
            assert budget.estimate({"rewards": 1}, measured={}) == {"rewards": .02}
            with store.transaction():
                store.conn.execute("DROP TABLE talk_reward")
            await night.stage_rewards(ctx)
    asyncio.run(run())


def test_stage_records_discord_separately_and_excludes_owner_noncast(context, store, clock, tmp_path):
    enable(tmp_path)
    async def run():
        async with context(learn_enabled=True) as (ctx, _):
            ctx.window = (clock.now()-1, clock.now()+1)
            ids = [line(store, clock, channel=ch, ts=clock.now()-300) for ch in ("guild", "discord:general", "dm:101")]
            line(store, clock, guid=1)
            line(store, clock, guid=105)
            for lid in ids:
                signals(store, lid, clock.now(), explicit=1)
                screened(store, lid, clock.now())
            await night.stage_rewards(ctx)
            out = report(store.conn, now=clock.now())
            assert out["lines"] == 3
            assert {r["kind"] for r in out["channels"]} == {"guild", "discord", "dm"}
            assert {r["guid"] for r in out["members"]} == {101}
    asyncio.run(run())


def test_cli_reports_recipe_fields_without_content_or_writes(context, store, clock, tmp_path, capsys, monkeypatch):
    enable(tmp_path)
    async def run():
        async with context(learn_enabled=True) as (ctx, _):
            ctx.window = (clock.now()-1, clock.now()+1)
            lid = line(store, clock, text="invented secret message", ts=clock.now()-300)
            turn = Turn(TurnKind.REPLY_HUMAN, 101, "guild")
            with store.transaction():
                record_recipe(store.conn, lid, turn, TurnMeta("synthetic"), ctx.persona(101),
                              load_config(tmp_path / "mind.toml"))
            signals(store, lid, clock.now(), explicit=1)
            screened(store, lid, clock.now())
            await night.stage_rewards(ctx)
    asyncio.run(run())
    monkeypatch.setattr("leeroy_mind.learn.cli.time.time", clock.now)
    before = (tmp_path / "mind.db").read_bytes()
    assert learn_main(["--data-dir", str(tmp_path), "report", "--days", "7"]) == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["lines"] == 1 and data["members"][0]["guid"] == 101
    assert data["members"][0]["mean"] == pytest.approx(1.)
    assert {r["field"] for r in data["recipes"]} == {
        "template_id", "template_version", "dials_hash", "model_id", "guard_verdict"}
    assert "invented secret message" not in output and "Ashlen" not in output
    assert (tmp_path / "mind.db").read_bytes() == before


def test_reward_and_night_receipt_rollback_together(context, store, clock, tmp_path, monkeypatch, caplog):
    enable(tmp_path)
    async def run():
        async with context(learn_enabled=True) as (ctx, _):
            ctx.window = (clock.now()-1, clock.now()+1)
            lid = line(store, clock, ts=clock.now()-300)
            screened(store, lid, clock.now())
            with store.transaction():
                store.conn.execute("CREATE TRIGGER fail_reward BEFORE INSERT ON talk_reward "
                    "BEGIN SELECT RAISE(ABORT,'invented private message'); END")
            await night.stage_rewards(ctx)
            assert not ctx.done("rewards", str(lid))
            assert not store.conn.execute("SELECT * FROM talk_reward").fetchall()
            assert ctx.failures["rewards"] == 1
    asyncio.run(run())
    assert "invented private message" not in caplog.text
    assert "reward_write_failed" in caplog.text


def test_attach_is_off_by_default_and_requires_no_model(tmp_path):
    runner = NightRunner(STAGES, lambda *_: None)
    night.attach(runner, tmp_path)
    assert runner.stages == STAGES
    enable(tmp_path)
    night.attach(runner, tmp_path)
    stages = list(runner.stages)
    i = next(i for i, s in enumerate(stages) if s.name == "rewards")
    assert stages[i-1].name == "threads" and stages[i].model is None
    runner = NightRunner(STAGES, lambda *_: None)
    runner.add_stage("signals", lambda _: None, after="threads", model=None)
    night.attach(runner, tmp_path)
    assert [s.name for s in runner.stages][:4] == ["snapshot", "threads", "signals", "rewards"]


def test_cli_old_schema_is_read_only_and_does_not_initialize(tmp_path, capsys):
    path = tmp_path / "mind.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old (id INTEGER)")
    before = path.read_bytes()
    assert learn_main(["report", "--data-dir", str(tmp_path), "--days", "7"]) == 0
    assert json.loads(capsys.readouterr().out)["available"] is False
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.toml"))


@pytest.mark.parametrize("days", ["0", "-1", "3651", "nope"])
def test_cli_days_bounds(tmp_path, days):
    with pytest.raises(SystemExit):
        learn_main(["report", "--data-dir", str(tmp_path), "--days", days])
