"""`bakeoff run` on invented data with a FakeLLM and a fake Ollama admin: the candidates are asked
the captured turns again with the captured options, the baselines are kept, every family is
scored by the Turing stylometry and the blind judge, mind.db is never written, and it refuses
while the mind runs, WoW is open or the GPU is paused."""
import json
import sqlite3

import pytest

from bakeoff.rig import CONFIG, SYSTEM, digest, ollama, seed, seed_discord
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.bakeoff import variants
from leeroy_mind.bakeoff.cli import bakeoff_main
from leeroy_mind.bakeoff.config import CandidateError, load_candidates


def run(root, tmp_path, *extra, llm=None, admin=None, wow=False, clock=None, cfg_text=CONFIG):
    config = tmp_path / "bakeoff.toml"
    config.write_text(cfg_text)
    return bakeoff_main(["run", "--data-dir", str(root), "--config", str(config), *extra],
                        make_llm=lambda cfg: llm, make_admin=lambda cfg: admin,
                        wow_open=lambda: wow, clock=clock)


@pytest.fixture
def data(store, clock):
    return seed(store, clock.now())


def test_a_run_asks_scores_and_never_writes_mind_db(data, tmp_path, clock, capsys):
    llm, admin, cfg = ollama(data, loaded={"live-model"})
    before = digest(data / "mind.db")
    out = tmp_path / "out"
    assert run(data, tmp_path, "--turns", "40", "--windows", "4", "--out", str(out),
               llm=llm, admin=admin, clock=clock) == 0
    printed = capsys.readouterr()
    assert digest(data / "mind.db") == before
    for word in ("lovely", "Certainly", "Arkon", "Belra"):
        assert word not in printed.out + printed.err
    report = json.loads((out / "report.json").read_text())
    assert report["turns"] == 40 and report["windows"] == 4 and report["missing"] == ["missing:1"]
    names = [c["name"] for c in report["candidates"]]
    assert names == ["sent", "live", "polite", "terse", "terse-think"]
    by = {c["name"]: c for c in report["candidates"]}
    private = {n: by[n]["families"]["private"] for n in names}
    assert all(r["status"] == "ok" for r in private.values())
    assert private["polite"]["auc"] > 0.9 > private["terse"]["auc"]
    assert by["polite"]["judge"]["private"]["balanced"] == 1.0
    assert by["terse"]["judge"]["private"]["balanced"] == 0.5
    assert by["polite"]["lines"]["ok"] == 40 and by["polite"]["lines"]["distinct"] == 1.0
    assert by["polite"]["detectors"]["relevance_measured"] is True
    assert by["sent"]["lines"]["distinct"] is None
    md = (out / "report.md").read_text()
    assert "| polite |" in md and "Not pulled" in md and "lovely" not in md
    assert "judge caught: writing too polished or literary" in md
    assert "too tidy" not in md  # whispers: the judge's phrases are never kept from them


def test_candidates_get_the_captured_request_with_their_model_think_and_variant(data, tmp_path, clock):
    llm, admin, cfg = ollama(data, loaded={cfg_live := "live-model"})
    assert run(data, tmp_path, "--turns", "40", "--windows", "0", "--out", str(tmp_path / "o"),
               llm=llm, admin=admin, clock=clock) == 0
    calls = [c for c in llm.calls if "messages" in c]
    polite = [c for c in calls if c["model"] == "polite:1"]
    terse = [c for c in calls if c["model"] == "terse:1" and c["think"] is False]
    think = [c for c in calls if c["model"] == "terse:1" and c["think"] is True]
    assert len(polite) == len(terse) == len(think) == 40
    assert all(c["options"]["num_predict"] == 80 and c["options"]["stop"] == ["\n\n"]
               and c["options"]["temperature"] == 0.9 and "seed" in c["options"] for c in polite)
    assert {c["options"]["seed"] for c in polite} == {c["options"]["seed"] for c in think}
    assert all(c["options"]["num_predict"] == 180 for c in think)
    assert all(c["keep_alive"] == "5m" for c in polite + terse + think)
    assert polite[0]["messages"][0]["content"] == SYSTEM
    system = terse[0]["messages"][0]["content"]
    assert "Write only your line" in system and "Never mention AI" in system
    assert "other people's lines" in system and "Your slang is 2009 slang" not in system
    assert "You can be moody" not in system and "usually" not in system
    assert "You don't flirt with anyone in the guild." in system
    assert not [c for c in calls if c["model"] == "missing:1"]
    # room made first (the live model), each model unloaded when done, the embedding kept
    assert admin.unloaded[0] == cfg_live and {"polite:1", "terse:1"} <= set(admin.unloaded)
    assert cfg.models.embedding not in admin.unloaded
    order = [c["model"] for c in calls if c["model"] in ("polite:1", "terse:1")]
    assert order == sorted(order, key=lambda m: m != "polite:1")  # one model at a time


def test_a_resumed_run_retries_only_what_failed(data, tmp_path, clock):
    llm, admin, _ = ollama(data, fail_model="terse:1")
    out = tmp_path / "out"
    assert run(data, tmp_path, "--turns", "30", "--windows", "2", "--out", str(out),
               llm=llm, admin=admin, clock=clock) == 0
    report = json.loads((out / "report.json").read_text())
    terse = next(c for c in report["candidates"] if c["name"] == "terse")
    assert terse["lines"]["error"] == 30
    llm2, admin2, _ = ollama(data)
    assert run(data, tmp_path, "--turns", "99", "--windows", "9", "--out", str(out),
               llm=llm2, admin=admin2, clock=clock) == 0
    asked = [c for c in llm2.calls if "messages" in c and c["model"] in ("polite:1", "terse:1")]
    assert len(asked) == 60  # terse and terse-think again; polite was already done
    report = json.loads((out / "report.json").read_text())
    assert report["turns"] == 30  # the stored plan, not the new --turns
    assert next(c for c in report["candidates"] if c["name"] == "terse")["lines"]["ok"] == 30


def test_refuses_while_the_mind_runs_wow_is_open_or_the_gpu_is_paused(data, tmp_path, clock, store, capsys):
    llm, admin, _ = ollama(data)
    with acquire_instance_lock(data):
        assert run(data, tmp_path, llm=llm, admin=admin, clock=clock) == 1
    assert "the mind is running" in capsys.readouterr().err
    assert run(data, tmp_path, llm=llm, admin=admin, wow=True, clock=clock) == 1
    assert "WoW is open" in capsys.readouterr().err
    assert run(data, tmp_path, llm=llm, admin=admin, wow=None, clock=clock) == 1
    assert "cannot tell whether WoW is open" in capsys.readouterr().err
    store.set_meta("gpu:paused", json.dumps({"at": 1.0}))
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert run(data, tmp_path, llm=llm, admin=admin, clock=clock) == 1
    assert "Free GPU" in capsys.readouterr().err
    assert llm.calls == [] and not list((data / "scratch").glob("bakeoff-*"))


def test_the_results_never_go_into_a_git_checkout(data, tmp_path, clock, capsys):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    llm, admin, _ = ollama(data)
    assert run(data, tmp_path, "--out", str(repo / "out"), llm=llm, admin=admin, clock=clock) == 1
    assert "inside a git checkout" in capsys.readouterr().err
    assert not (repo / "out").exists()


def test_the_default_out_is_the_data_dirs_scratch(data, tmp_path, clock):
    llm, admin, _ = ollama(data)
    assert run(data, tmp_path, "--turns", "30", "--windows", "1", llm=llm, admin=admin, clock=clock) == 0
    [out] = list((data / "scratch").glob("bakeoff-*"))
    assert (out / "results.sqlite").is_file() and (out / "report.md").is_file()


def test_a_dry_run_shows_the_plan_and_the_missing_models_without_the_lock(data, tmp_path, clock, capsys):
    llm, admin, _ = ollama(data)
    with acquire_instance_lock(data):  # safe beside a running mind
        assert run(data, tmp_path, "--dry-run", "--turns", "20", llm=llm, admin=admin,
                   wow=True, clock=clock) == 0
    out = capsys.readouterr().out
    assert "captured: 40 turns (whisper 40)" in out and "plan: 20 turns (private 20)" in out
    assert "missing (not pulled): missing:1" in out and "reflection model (the judge): pulled" in out
    assert [c for c in llm.calls if "messages" in c] == []
    assert not (data / "scratch").exists() or not list((data / "scratch").glob("bakeoff-*"))


def test_discord_channels_and_group_dms_are_measured_and_keep_phrases_only_from_public(
        data, store, tmp_path, clock):
    """A group DM counts in the discord family (turing.corpus) with kind gdm; the judge's phrase
    is kept from #general, never from a group DM; a Discord answer is one message."""
    seed_discord(store, clock.now(), channel="general", members=(101, 102, 103, 104, 105), per=8)
    seed_discord(store, clock.now(), channel="gdm:7", members=(101, 102), per=6, start_days=1.0)
    llm, admin, _ = ollama(data)
    out = tmp_path / "out"
    assert run(data, tmp_path, "--kinds", "discord,gdm", "--turns", "52", "--windows", "20",
               "--out", str(out), llm=llm, admin=admin, clock=clock) == 0
    conn = sqlite3.connect(out / "results.sqlite")
    kinds = {k for (k,) in conn.execute("SELECT DISTINCT kind FROM turn")}
    families = {f for (f,) in conn.execute("SELECT DISTINCT family FROM turn")}
    polite = [json.loads(x) for (x,) in conn.execute(
        "SELECT lines FROM result WHERE candidate='polite' AND status='ok'")]
    tells = [(unit, json.loads(t)) for unit, t in conn.execute(
        "SELECT unit, tells FROM judged WHERE candidate='polite' AND status='ok'")]
    windows = {u: json.loads(r) for u, r in conn.execute("SELECT unit, refs FROM window")}
    conn.close()
    assert kinds == {"discord", "gdm"} and families == {"discord"}
    assert len(polite) == 52 and all(len(lines) == 1 for lines in polite)
    gdm = {f"d:{i}" for (i,) in store.conn.execute(
        "SELECT id FROM discord_message WHERE channel='gdm:7'")}
    kept = [t["phrase"] for unit, ts in tells for t in ts if t.get("phrase")]
    from_gdm = [t for unit, ts in tells if set(windows[unit]) & gdm for t in ts]
    assert kept and from_gdm and all(t["phrase"] is None for t in from_gdm)
    report = json.loads((out / "report.json").read_text())
    by = {c["name"]: c for c in report["candidates"]}
    assert by["polite"]["families"]["discord"]["status"] == "ok"
    assert by["polite"]["judge"]["discord"]["balanced"] == 1.0


def test_kinds_filter_the_turns(data, tmp_path, clock, capsys):
    llm, admin, _ = ollama(data)
    assert run(data, tmp_path, "--kinds", "dm,guild", llm=llm, admin=admin, clock=clock) == 1
    assert "nothing to ask" in capsys.readouterr().out


def test_nothing_captured_says_how_to_start(store, clock, tmp_path, capsys):
    root = seed(store, clock.now(), members=(101,), per=1)
    store.conn.execute("DELETE FROM bakeoff_turn")
    store.conn.commit()
    llm, admin, _ = ollama(root)
    assert run(root, tmp_path, llm=llm, admin=admin, clock=clock) == 1
    assert "turn on [bakeoff] capture" in capsys.readouterr().out


@pytest.mark.parametrize("text,why", [
    ("", r"no \[\[candidate\]\] tables"),
    ('[[candidate]]\nname = "sent"\nmodel = "x"\n', "name"),
    ('[[candidate]]\nname = "a"\nmodel = "x"\nprompt_variant = "rules"\n', "prompt_variant"),
    ('[[candidate]]\nname = "a"\nmodel = "x"\nthink = "yes"\n', "think"),
    ('[[candidate]]\nname = "a"\nmodel = "x"\ntemperature = 1\n', "unknown key temperature"),
    ('[[candidate]]\nname = "a"\nmodel = "x"\n[[candidate]]\nname = "A"\nmodel = "y"\n', "share a name"),
])
def test_a_bad_candidate_file_is_refused_by_key(tmp_path, text, why):
    path = tmp_path / "c.toml"
    path.write_text(text)
    with pytest.raises(CandidateError, match=why):
        load_candidates(path)


def test_candidate_defaults(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[[candidate]]\nname = "a"\nmodel = "m"\n[[candidate]]\nname = "b"\n'
                    'model = "m"\nthink = true\n')
    a, b = load_candidates(path)
    assert (a.think, a.think_budget, a.prompt_variant, a.timeout) == (False, 0, "as_is", 120)
    assert (b.think, b.think_budget) == (True, 160)


def test_situation_keeps_the_hard_rules_and_an_unknown_bullet():
    messages = [{"role": "system", "content": SYSTEM + "\n- An owner's tuning line.\n\nAfter."},
                {"role": "user", "content": "- Your slang is 2009 slang. (in the user message)"}]
    out = variants.situation(messages)
    system = out[0]["content"]
    assert "How you talk here:\n- You're not an assistant. Never mention AI" in system
    assert "- Don't narrate actions or write other people's lines." in system
    assert "- Write only your line: no name in front, no quotes." in system
    assert "- An owner's tuning line." in system and system.endswith("\n\nAfter.")
    for gone in ("2009", "React to", "moody", "guilt-trip", "(nothing)", "usually"):
        assert gone not in system
    assert out[1] == messages[1] and messages[0]["content"].startswith("You are Belra")
    assert variants.as_is(messages) == messages
