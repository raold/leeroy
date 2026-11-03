"""CLI boundaries over synthetic databases and injected clients; never a live model."""
import asyncio
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.app import acquire_instance_lock, instance_lock_held
from leeroy_mind.config import load_config
from leeroy_mind.nightly import cli
from leeroy_mind.nightly.runner import Stage
from leeroy_mind.nightly.store import ledger_add, log_grounding, run_begin, run_finish
from leeroy_mind.testing.fake_llm import FakeLLM
from nightly.conftest import scripted
from nightly.test_night_enrich import APPEARANCE, ENRICHED

NIGHT = "2026-10-29"
WEEK = "2026-W44"


@pytest.fixture
def data(h, monkeypatch):
    root = h.settings.data_dir
    (root / "mind.toml").write_text('[human]\nnickname="Testy"\nreal_name="Quillon"\npronouns="he"\n',
                                    encoding="utf-8")
    h.clock.set(datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(cli, "SystemClock", lambda: h.clock)
    monkeypatch.setenv("LEEROY_MIND_DATA", str(root))
    return root


def client(h, *, script=None):
    return FakeLLM(script=script, models=frozenset({h.LIVE, h.REFL, h.EMB}))


@pytest.fixture
def tiny_pipeline(monkeypatch):
    seen = []

    async def step(ctx):
        seen.append(ctx)
        assert ctx.writer._store is ctx.store
        assert ctx.writer._human() == ctx.ident
        assert type(ctx.arbiter.admin).__name__ == "NullAdmin"
        if not ctx.done("tiny", "all"):
            ctx.commit("tiny", "all", lambda store: store.set_meta("trial-proof", "written"))

    async def plan(ctx):
        ctx.plan = {}
        return {}

    monkeypatch.setitem(sys.modules, "leeroy_mind.nightly.pipeline",
                        SimpleNamespace(STAGES=(Stage("tiny", step, None),)))
    monkeypatch.setattr("leeroy_mind.nightly.runner.plan_night", plan)
    return seen


def test_nightly_run_uses_a_copy_and_resumes_that_same_trial(h, data, tiny_pipeline):
    source = data / "mind.db"
    before = source.read_bytes(), source.stat().st_mtime_ns
    fake = client(h)
    assert cli.nightly_main(["run", "--night", NIGHT], make_llm=lambda: fake) == 0
    trial = data / "scratch" / f"mind-{NIGHT}-trial.db"
    assert trial.exists()
    assert before == (source.read_bytes(), source.stat().st_mtime_ns)
    assert h.store.get_meta("trial-proof") is None
    with sqlite3.connect(trial) as copied:
        assert copied.execute("SELECT value FROM meta WHERE key='trial-proof'").fetchone()[0] == "written"
        assert copied.execute("SELECT status FROM night_run").fetchone()[0] == "done"
    assert cli.nightly_main(["run", "--night", NIGHT], make_llm=lambda cfg: fake) == 0
    with sqlite3.connect(trial) as copied:
        assert copied.execute("SELECT count(*) FROM night_step WHERE stage='tiny'").fetchone()[0] == 1
    assert len(tiny_pipeline) == 2 and fake.calls == []


def test_live_nightly_run_holds_exclusive_lock_until_done(h, data, tiny_pipeline):
    original = tiny_pipeline
    assert cli.nightly_main(["run", "--night", NIGHT, "--live-db"], make_llm=lambda cfg: client(h)) == 0
    assert h.store.get_meta("trial-proof") == "written"
    assert not instance_lock_held(data)
    assert original[0].store is not h.store


def test_resumed_trial_keeps_original_window_and_attempt_count(h, data, tiny_pipeline):
    window = (h.T - 10, h.T + 20)
    run_begin(h.store, NIGHT, *window, now=h.T)
    run_finish(h.store, NIGHT, now=h.T + 1, status="interrupted")
    assert cli.nightly_main(["run", "--night", NIGHT], make_llm=lambda: client(h)) == 0
    assert tiny_pipeline[0].window == window
    with sqlite3.connect(data / "scratch" / f"mind-{NIGHT}-trial.db") as copied:
        row = copied.execute("SELECT window_start,window_end,attempts FROM night_run").fetchone()
        assert row == (*window, 1)


@pytest.mark.parametrize("args", [["run", "--night", NIGHT, "--live-db"], ["undo", "1"],
                                   ["veto", "1"], ["enrich", "--limit", "1"]])
def test_all_live_mutators_refuse_before_opening_or_constructing_client(h, data, monkeypatch, capsys, args):
    monkeypatch.setattr(cli, "instance_lock_held", lambda root: True)
    monkeypatch.setattr(cli, "open_db", lambda path: pytest.fail("opened live database while locked"))
    assert cli.nightly_main(args, make_llm=lambda: pytest.fail("constructed client")) == 1
    assert "mind service is running" in capsys.readouterr().out


def test_real_instance_lock_closes_the_check_acquire_race(h, data, monkeypatch, capsys):
    monkeypatch.setattr(cli, "instance_lock_held", lambda root: False)
    with acquire_instance_lock(data):
        assert cli.nightly_main(["veto", "1"]) == 1
    assert "mind service is running" in capsys.readouterr().out


def test_real_ollama_trial_refuses_shared_gpu_while_service_runs(h, data, capsys):
    with acquire_instance_lock(data):
        assert cli.nightly_main(["run", "--night", NIGHT]) == 1
    text = capsys.readouterr().out
    assert "mind service is running" in text and "shares Ollama" in text
    assert h.store.get_meta("trial-proof") is None


def test_fake_trial_still_runs_while_service_lock_is_held(h, data, tiny_pipeline):
    with acquire_instance_lock(data):
        assert cli.nightly_main(["run", "--night", NIGHT], make_llm=lambda: client(h)) == 0


def test_readonly_status_and_ledger_do_not_migrate_lock_or_construct_models(h, data, monkeypatch, capsys):
    run_begin(h.store, NIGHT, h.T, h.T + 100, now=h.T)
    run_finish(h.store, NIGHT, now=h.T + 20, status="done")
    with h.store.transaction():
        h.store.conn.execute("UPDATE night_run SET plan=?", (json.dumps({"estimate_s": 8, "budget_s": 9,
                                                                       "steps": ["fewer"]}),))
        h.store.conn.execute("INSERT INTO night_stage(night,stage,started,finished,calls,call_s) "
                            "VALUES (?,'reflect',100,123,4,18)", (NIGHT,))
        h.store.conn.execute("INSERT INTO night_step VALUES (?,'reflect','101',123)", (NIGHT,))
    log_grounding(h.store, night=NIGHT, stage="reflect", owner=101, kind="belief", text="x",
                  verdict="accepted", check="judge", reason="", ts=h.T)
    ledger_add(h.store, guid=101, kind="note_added", text="Older note", evidence=[], now=h.T - 20)
    ledger_add(h.store, guid=101, kind="note_added", text="Quillon newer note", evidence=[], now=h.T)
    before = (data / "mind.db").stat().st_mtime_ns
    monkeypatch.setattr(cli, "open_db", lambda path: pytest.fail("read-only command migrated"))
    monkeypatch.setattr(cli, "acquire_instance_lock", lambda root: pytest.fail("read-only command locked"))
    assert cli.nightly_main(["status", "--night", NIGHT], make_llm=lambda: pytest.fail("client")) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["run"]["plan"]["steps"] == ["fewer"]
    assert status["stages"][0]["duration_s"] == 23
    assert status["stages"][0]["calls"] == 4
    assert status["units"] == {"reflect": 1}
    assert status["grounding"]["reflect"] == {"accepted": 1, "rejected": 0}
    assert cli.nightly_main(["ledger", "--guid", "101", "--since", str(h.T - 30)]) == 0
    text = capsys.readouterr().out
    assert text.index("newer note") < text.index("Older note")
    assert "Quillon" not in text
    assert (data / "mind.db").stat().st_mtime_ns == before


def test_status_without_runs_is_readable(h, data, capsys):
    assert cli.nightly_main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_run"


def test_ledger_undo_and_veto_are_real_atomic_mutations(h, data, capsys):
    text = "I can ask for help."
    h.store.put_state(replace(h.store.get_state(101), self_notes=({"text": text, "evidence": [], "added": h.T},)))
    lid = ledger_add(h.store, guid=101, kind="note_added", text=text, evidence=[], now=h.T)
    with h.store.transaction():
        tid = h.store.conn.execute("INSERT INTO lexicon(kind,term,target,origin_ref,originator,born,last_used) "
            "VALUES ('nickname','Old Pal',1,'chat:1',101,?,?)", (h.T, h.T)).lastrowid
    assert cli.nightly_main(["undo", str(lid)]) == 0
    assert h.store.get_state(101).self_notes == ()
    assert "reversed note_added" in capsys.readouterr().out
    assert cli.nightly_main(["undo", str(lid)]) == 1
    assert cli.nightly_main(["veto", str(tid)]) == 0
    h.store.conn.commit()
    assert h.store.conn.execute("SELECT status FROM lexicon WHERE id=?", (tid,)).fetchone()[0] == "vetoed"
    assert not instance_lock_held(data)


def test_enrichment_dry_run_keeps_live_personas_and_approval_unchanged(h, data, capsys):
    versions = h.store.persona_versions()
    fake = client(h, script=scripted({"enrich": [ENRICHED, ENRICHED]}))
    assert cli.nightly_main(["enrich", "--dry-run", "--limit", "2"], make_llm=lambda cfg: fake) == 0
    path = data / "scratch" / "enrich-2026-11-01.json"
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 2
    assert h.store.persona_versions() == versions
    assert h.store.get_meta("enrich_approved") is None
    assert h.count("night_step") == 0
    assert '"done": 2' in capsys.readouterr().out


def test_dry_run_previews_previously_refused_members_without_deleting_live_checkpoint(h, data):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO night_step VALUES ('2026-11-01','enrich','101',?)", (h.T,))
    fake = client(h, script=scripted({"enrich": [ENRICHED, ENRICHED]}))
    assert cli.nightly_main(["enrich", "--dry-run", "--limit", "2"], make_llm=lambda: fake) == 0
    entries = json.loads((data / "scratch" / "enrich-2026-11-01.json").read_text(encoding="utf-8"))
    assert [entry["guid"] for entry in entries] == [101, 102]
    assert h.count("night_step") == 1
    assert h.store.get_meta("enrich_approved") is None


def test_enrichment_limit_counts_only_unenriched_members_and_live_approval(h, data):
    h.persona(101, appearance=APPEARANCE)
    fake = client(h, script=scripted({"enrich": [ENRICHED]}))
    assert cli.nightly_main(["enrich", "--limit", "1"], make_llm=lambda: fake) == 0
    assert h.store.get_meta("enrich_approved") == "1"
    assert h.store.persona_versions()[102] == 2
    assert h.store.persona_versions()[103] == 1


def test_enrichment_rejects_human_guid_without_model_call(h, data):
    fake = client(h)
    assert cli.nightly_main(["enrich", "--dry-run", "--guid", "1"], make_llm=lambda: fake) == 1
    assert fake.calls == []


def test_no_llm_evaluation_uses_copy_and_never_constructs_client(h, data, capsys):
    before = h.count("eval_answer"), h.count("probe_run")
    with acquire_instance_lock(data):
        assert cli.eval_main(["--week", WEEK, "--no-llm"],
                             make_llm=lambda: pytest.fail("no-llm constructed a client")) == 0
    assert (data / "scratch" / f"mind-eval-{WEEK}-trial.db").exists()
    report = json.loads((data / "reports" / f"{WEEK}.json").read_text(encoding="utf-8"))
    assert report["secret_probe"]["skipped"] and report["memory_quiz"]["skipped"]
    assert before == (h.count("eval_answer"), h.count("probe_run"))
    assert "report " in capsys.readouterr().out


def test_probe_records_only_complete_counts_in_live_database(h, data, capsys):
    fake = client(h, script=lambda model, messages: "(nothing)")
    assert cli.eval_main(["--probe-secrets"], make_llm=lambda cfg: fake) == 0
    h.store.conn.commit()
    row = h.store.conn.execute("SELECT * FROM probe_run").fetchone()
    assert (row["n"], row["leaks"], row["rate"]) == (36, 0, 0.0)
    assert len(fake.calls) == 36
    text = capsys.readouterr().out
    assert text.strip() == "n 36 leaks 0 rate 0"
    assert "(nothing)" not in text
    assert not instance_lock_held(data)


def test_probe_refuses_held_lock_and_partial_failure_never_records(h, data, capsys):
    fake = client(h)
    with acquire_instance_lock(data):
        assert cli.eval_main(["--probe-secrets"], make_llm=lambda: fake) == 1
    assert fake.calls == []
    fake.fail_next(1)
    assert cli.eval_main(["--probe-secrets"], make_llm=lambda: fake) == 1
    assert h.count("probe_run") == 0
    assert not instance_lock_held(data)


def test_direct_eval_closes_owned_asker_after_report_failure(h, data, monkeypatch):
    import leeroy_mind.eval as evaluation
    from leeroy_mind.eval.asker import ScratchAsker
    closed = []

    class Asker:
        async def aclose(self):
            closed.append(True)

    async def opened(**kwargs):
        return Asker()

    async def fail(ctx, week, *, use_llm):
        assert isinstance(ctx.asker, Asker)
        raise RuntimeError("Quillon private model payload")

    monkeypatch.setattr(ScratchAsker, "open", opened)
    monkeypatch.setattr(evaluation, "run_weekly", fail)
    assert cli.eval_main(["--week", WEEK], make_llm=lambda: client(h)) == 1
    assert closed == [True]


def test_real_context_keeps_gpu_lock_during_exception_cleanup(h, data):
    from leeroy_mind.llm.client import OllamaClient
    observations = []

    class OwnedClient(OllamaClient):
        async def aclose(self):
            observations.append(instance_lock_held(data))

    async def go():
        with pytest.raises(RuntimeError, match="synthetic"):
            async with cli._context(data, h.store, load_config(data / "mind.toml"), h.clock,
                                    NIGHT, make_llm=lambda: OwnedClient("http://unused.invalid")):
                assert instance_lock_held(data)
                raise RuntimeError("synthetic")

    run(go())
    assert observations == [True]
    assert not instance_lock_held(data)


def test_failed_trial_stage_releases_resources_and_marks_failed(h, data, tiny_pipeline, monkeypatch, capsys):
    async def fail(ctx):
        raise RuntimeError("Quillon private model payload")

    monkeypatch.setitem(sys.modules, "leeroy_mind.nightly.pipeline",
                        SimpleNamespace(STAGES=(Stage("failure", fail, None),)))
    assert cli.nightly_main(["run", "--night", NIGHT], make_llm=lambda: client(h)) == 1
    with sqlite3.connect(data / "scratch" / f"mind-{NIGHT}-trial.db") as trial:
        row = trial.execute("SELECT status,error FROM night_run").fetchone()
        assert row == ("failed", "RuntimeError")
    output = capsys.readouterr()
    assert "Quillon" not in output.out + output.err
    assert not instance_lock_held(data / "scratch" / "locks" / f"{NIGHT}-trial")


@pytest.mark.parametrize("argv", [["run", "--night", "../2026-10-29"], ["run", "--night", "2026-02-30"],
                                  ["undo", "0"], ["enrich", "--limit", "-1"]])
def test_invalid_cli_arguments_fail_before_opening_any_database(data, monkeypatch, argv):
    monkeypatch.setattr(cli, "open_db", lambda path: pytest.fail("opened before validation"))
    with pytest.raises(SystemExit) as exc:
        cli.nightly_main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [["--week", "../2026-W44"], ["--week", "2025-W53"],
                                  ["--probe-secrets", "--no-llm"]])
def test_invalid_eval_arguments_fail_before_models(data, argv):
    with pytest.raises(SystemExit) as exc:
        cli.eval_main(argv, make_llm=lambda: pytest.fail("constructed client"))
    assert exc.value.code == 2


def test_future_night_refused_and_bad_data_dir_not_created(h, data, tmp_path):
    assert cli.nightly_main(["run", "--night", "2026-11-10"], make_llm=lambda: pytest.fail("client")) == 1
    missing = tmp_path / "missing"
    assert cli.nightly_main(["status", "--data-dir", str(missing)]) == 1
    assert not missing.exists()


def test_data_dir_before_or_after_action_and_default_week_boundary(h, data, capsys):
    for argv in (["--data-dir", str(data), "status"], ["status", "--data-dir", str(data)]):
        assert cli.nightly_main(argv) == 0
        capsys.readouterr()
    assert cli._default_week(h.clock) == WEEK
    h.clock.set(datetime(2026, 11, 2, 5, tzinfo=timezone.utc).timestamp())
    assert cli._default_week(h.clock) == "2026-W43"
    assert cli._default_night(h.clock) == "2026-10-31"


def test_trial_symlink_cannot_redirect_writes_outside_data_dir(h, data, tmp_path):
    scratch = data / "scratch"
    scratch.mkdir()
    external = tmp_path / "outside.db"
    external.write_bytes(b"unchanged")
    try:
        (scratch / f"mind-{NIGHT}-trial.db").symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert cli.nightly_main(["run", "--night", NIGHT], make_llm=lambda: pytest.fail("client")) == 1
    assert external.read_bytes() == b"unchanged"
