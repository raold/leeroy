"""The night stage `turing` on the nightly harness, with a scripted fake judge. Invented lines
only; "Quillon" is the fixture's real name, never the owner's."""
import asyncio
import json
import re
from dataclasses import replace

import pytest

from leeroy_mind.llm.client import GpuPaused
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.pipeline import STAGES
from leeroy_mind.nightly.runner import NightRunner
from leeroy_mind.turing import judge, night, store as tstore
from leeroy_mind.turing.config import TuringCfg, load

CFG = TuringCfg(enabled=True, windows=4, days=3)
SWITCH = b"[turing]\nenabled = true\nwindows = 4\ndays = 3\n"
MEMBERS = (101, 102, 103)


def conversations(h, *, his_style="ok sounds good", member_style="Sure, that's lovely."):
    """A night of whispers (his lines and the members') and guild talk, some with him."""
    t = h.T + 3600
    for m in MEMBERS:  # a whisper conversation with each: twelve exchanges, either may open
        t += 3600
        for i in range(12):
            first, second = ((1, m), (m, 1)) if i % 2 else ((m, 1), (1, m))
            for k, (sender, to) in enumerate((first, second)):
                text = his_style if sender == 1 else member_style
                h.say(t + 60 * i + 20 * k, sender, text, channel="whisper", to=to)
    t += 3600
    for i in range(60):  # guild: he speaks every fourth line in the first half
        speaker = 1 if i < 30 and i % 4 == 0 else MEMBERS[i % 3]
        text = ("anyone up for voa" if speaker == 1
                else f"Arkon, Quillon, Testy, jo and Sams said {i}.")  # Jo, Sam: persona names
        h.say(t + 12 * i, speaker, text)
    return t + 800


def judge_reply(prompt):
    """Calls A human and everyone else an AI, with one tell each."""
    letters = re.search(r"Speakers: ([A-Z, ]+)\.", prompt).group(1).split(", ")
    return {"speakers": [{"speaker": s, "human": s == "A",
                          "tells": [{"code": "natural" if s == "A" else "polish",
                                     "phrase": "reads like Arkon typing" if s != "A" else "short"}]}
                         for s in letters]}


def ctx_for(h, *, switch=True, **kw):
    if switch:
        (h.settings.data_dir / "mind.toml").write_bytes(SWITCH)
    ctx = h.ctx({}, default=judge_reply, **kw)
    ctx.settings = replace(ctx.settings, turing_windows=CFG.windows)
    return ctx


def run(coro):
    return asyncio.run(coro)


def rows(h, table):
    return h.store.conn.execute(f"SELECT * FROM {table}").fetchall()


def test_the_stage_scores_style_and_judges_its_windows(h):
    end = conversations(h)
    h.clock.set(end + 7200)
    ctx = ctx_for(h)
    run(night.make_stage(CFG)(ctx))
    fams = {r["family"]: r for r in h.store.conn.execute("SELECT * FROM turing_night")}
    assert set(fams) == {"game", "private", "discord"}
    assert fams["private"]["status"] == "ok" and fams["private"]["auc"] > 0.9
    assert fams["discord"]["status"] == "too_few"
    feats = json.loads(fams["private"]["features"])
    assert {"key", "human", "member", "separation", "higher"} <= set(feats[0])
    judged = h.store.conn.execute("SELECT * FROM turing_judgement ORDER BY unit").fetchall()
    assert [r["unit"] for r in judged] == ["w1", "w2", "w3", "w4"]
    assert all(r["status"] == "ok" for r in judged)
    totals = {f: (r["judge_windows"], r["judge_speakers"], r["judge_correct"]) for f, r in fams.items()}
    assert sum(t[0] for t in totals.values()) == 4
    assert sum(t[1] for t in totals.values()) == sum(r["speakers"] for r in judged)
    for r in judged:
        refs = json.loads(r["refs"])
        assert judge.WINDOW_MIN <= len(refs) <= judge.WINDOW_MAX
        tells = json.loads(r["tells"])
        # whisper windows are private: codes only; guild windows may keep the judge's phrase
        private = r["family"] == "private"
        assert all((t["phrase"] is None) == private for t in tells)
        assert all("Arkon" not in (t["phrase"] or "") for t in tells)
    # the judge read no name: speakers are letters, everyone else [name]
    prompts = [c["messages"][-1]["content"] for c in ctx.reasoner.llm.calls]
    assert len(prompts) == 4
    for p in prompts:
        body = p.split("matching this schema:")[0]
        for name in ("Quillon", "Testy", "Testmage", "Arkon", "Belra", "Coros"):
            assert name not in body
        assert not re.search(r"\b(jo|sams?|mercer|carter)\b", body, re.IGNORECASE)
    assert all(h.store.conn.execute("SELECT count(*) FROM night_step WHERE night=? AND stage='turing' "
                                    "AND unit=?", (h.night, u)).fetchone()[0] == 1
               for u in ("stylo", "plan", "w1", "w4"))


def test_a_resumed_night_judges_the_same_windows_and_no_more(h):
    end = conversations(h)
    h.clock.set(end + 7200)
    ctx = ctx_for(h)
    ctx.reasoner.llm.fail_next(1)  # the first judge call fails: that unit only
    run(night.make_stage(CFG)(ctx))
    assert ctx.failures["turing"] == 1
    plan = json.loads(h.store.get_meta(tstore.PLAN_KEY + h.night))
    assert len(rows(h, "turing_judgement")) == 3
    again = ctx_for(h)
    assert night.count(again) == 1
    run(night.make_stage(CFG)(again))
    assert len(again.reasoner.llm.calls) == 1 and not again.failures
    assert json.loads(h.store.get_meta(tstore.PLAN_KEY + h.night)) == plan
    assert len(rows(h, "turing_judgement")) == 4
    third = ctx_for(h)
    run(night.make_stage(CFG)(third))
    assert third.reasoner.llm.calls == [] and night.count(third) == 0


def test_the_owner_pausing_the_gpu_stops_the_night_without_a_unit_failure(h):
    end = conversations(h)
    h.clock.set(end + 7200)
    ctx = ctx_for(h)

    async def paused(*a, **kw):
        raise GpuPaused("paused")
    ctx.reasoner.json = paused
    with pytest.raises(GpuPaused):
        run(night.make_stage(CFG)(ctx))
    assert not ctx.failures
    assert h.store.conn.execute("SELECT count(*) FROM turing_judgement").fetchone()[0] == 0


def test_lines_already_judged_are_not_shown_again_the_next_night(h):
    end = conversations(h)
    h.clock.set(end + 7200)
    run(night.make_stage(CFG)(ctx_for(h)))
    first = {r for (v,) in h.store.conn.execute("SELECT refs FROM turing_judgement") for r in json.loads(v)}
    h.clock.set(end + 86400)
    run(night.make_stage(CFG)(ctx_for(h, night="2026-10-30")))
    later = [json.loads(v) for (v,) in h.store.conn.execute(
        "SELECT refs FROM turing_judgement WHERE night='2026-10-30'")]
    assert later and not first & {r for refs in later for r in refs}


def test_group_dm_windows_keep_codes_only(h):
    t = h.T + 3600
    with h.store.transaction():
        for i in range(24):
            author = 1 if i % 3 == 0 else MEMBERS[i % 3]
            h.store.conn.execute(
                "INSERT INTO discord_message(channel, author, origin, text, ts) VALUES (?,?,?,?,?)",
                ("gdm:7", author, "human" if author == 1 else "mind", f"group line {i}", t + 30 * i))
    h.clock.set(t + 7200)
    ctx = ctx_for(h)
    run(night.make_stage(CFG)(ctx))
    judged = h.store.conn.execute("SELECT family, tells FROM turing_judgement").fetchall()
    assert judged and all(r["family"] == "discord" for r in judged)
    assert all(t["phrase"] is None for r in judged for t in json.loads(r["tells"]))
    assert "small group chat" in ctx.reasoner.llm.calls[0]["messages"][-1]["content"]


def test_no_windows_writes_style_only(h):
    h.clock.set(h.T + 7200)
    ctx = ctx_for(h)
    run(night.make_stage(CFG)(ctx))
    assert ctx.reasoner.llm.calls == []
    assert json.loads(h.store.get_meta(tstore.PLAN_KEY + h.night)) == []
    assert {r["status"] for r in h.store.conn.execute("SELECT status FROM turing_night")} == {"too_few"}


def test_attached_only_when_switched_on_after_the_last_reflection_stage(tmp_path):
    runner = NightRunner(STAGES, lambda n, w: None)
    assert night.attach(runner, tmp_path) is False
    assert "turing" not in [s.name for s in runner.stages]
    (tmp_path / "mind.toml").write_bytes(b"[turing]\nenabled = true\nwindows = 6\n")
    assert night.attach(runner, tmp_path) is True
    names = [s.name for s in runner.stages]
    assert names[names.index("forget") + 1] == "turing"
    assert next(s for s in runner.stages if s.name == "turing").model == "reflection"


@pytest.mark.parametrize("text,expected", [
    (b"", TuringCfg()),
    (b"[turing]\nenabled = true\n", TuringCfg(True, 8, 14)),
    (b"[turing]\nenabled = true\nwindows = 0\ndays = 90\n", TuringCfg(True, 0, 90)),
    (b"[turing]\nenabled = 1\n", TuringCfg()),
    (b"[turing]\nenabled = true\nwindows = 61\n", TuringCfg()),
    (b"[turing]\nenabled = true\nshout = true\n", TuringCfg()),
    (b"[turing\n", TuringCfg()),
])
def test_config_fails_off(tmp_path, text, expected):
    (tmp_path / "mind.toml").write_bytes(text)
    assert load(tmp_path / "mind.toml") == expected


def test_the_budget_counts_judge_calls_and_can_cut_them(h, tmp_path):
    assert "turing" in budget.EXTENSION_STAGES and budget.unit_calls("turing") == 1
    assert any(step.name == "turing" for step in budget.DEGRADE)
    assert h.settings.turing_windows == 8  # the default, under health's 10 failed units
    ctx = ctx_for(h, switch=False)
    assert night.count(ctx) == 0  # the switch is off in this data dir
    ctx = ctx_for(h)
    assert night.count(ctx) == CFG.windows
    out = budget._counts(ctx, set(), frozenset({"turing"}))
    assert out["turing"] == CFG.windows
    assert "turing" not in budget._counts(ctx, set(), frozenset())
