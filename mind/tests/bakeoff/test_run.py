"""`bakeoff run` on invented data with a FakeLLM and a fake Ollama admin: the candidates are asked
the captured turns again (the generation's first request by default, before any retry hint), the
three baselines are kept, every family is scored by the Turing stylometry and the blind judge, the
retried turns are reported apart, mind.db is never written, and it refuses while the mind runs,
WoW is open or the GPU is paused."""
import json
import sqlite3

import pytest

from bakeoff.rig import CONFIG, HINT, RETRY_EVERY, SYSTEM, digest, ollama, seed, seed_discord
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.bakeoff import variants
from leeroy_mind.bakeoff.cli import bakeoff_main
from leeroy_mind.bakeoff.config import Candidate, CandidateError, load_candidates

NAMES = ["sent", "first", "live", "polite", "terse", "terse-think", "deep"]


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
    for word in ("lovely", "Certainly", "Arkon", "Belra", "smiles"):
        assert word not in printed.out + printed.err
    assert "10 asked more than once live" in printed.out
    report = json.loads((out / "report.json").read_text())
    assert report["turns"] == 40 and report["windows"] == 4 and report["missing"] == ["missing:1"]
    assert report["retried"] == 40 // RETRY_EVERY and report["source"] == "first"
    names = [c["name"] for c in report["candidates"]]
    assert names == NAMES
    by = {c["name"]: c for c in report["candidates"]}
    private = {n: by[n]["families"]["private"] for n in names}
    assert all(r["status"] == "ok" for n, r in private.items() if n != "first")
    assert private["polite"]["auc"] > 0.9 > private["terse"]["auc"]
    assert by["polite"]["judge"]["private"]["balanced"] == 1.0
    assert by["terse"]["judge"]["private"]["balanced"] == 0.5
    assert by["polite"]["lines"]["ok"] == 40 and by["polite"]["lines"]["distinct"] == 1.0
    assert by["polite"]["detectors"]["relevance_measured"] is True
    assert by["sent"]["lines"]["distinct"] is None
    # the first draft was a silence on every retried turn; the reply whose words were sent was not
    assert by["first"]["lines"]["silent"] == 10 and by["live"]["lines"]["silent"] == 0
    retried, first_try = by["first"]["subsets"]["retried"], by["first"]["subsets"]["first_try"]
    assert retried["lines"]["turns"] == 10 and retried["lines"]["silent"] == 10
    assert first_try["lines"]["turns"] == 30 and first_try["lines"]["silent"] == 0
    assert by["polite"]["subsets"]["retried"]["lines"]["ok"] == 10
    md = (out / "report.md").read_text()
    assert "| polite |" in md and "Not pulled" in md and "lovely" not in md
    assert "judge caught: writing too polished or literary" in md
    assert "## Turns the live pipeline asked more than once" in md and "the first request" in md
    assert "too tidy" not in md  # whispers: the judge's phrases are never kept from them


def test_candidates_get_the_first_request_with_their_model_think_and_variant(data, tmp_path, clock):
    llm, admin, cfg = ollama(data, loaded={cfg_live := "live-model"})
    assert run(data, tmp_path, "--turns", "40", "--windows", "0", "--out", str(tmp_path / "o"),
               llm=llm, admin=admin, clock=clock) == 0
    calls = [c for c in llm.calls if "messages" in c]
    polite = [c for c in calls if c["model"] == "polite:1"]
    terse = [c for c in calls if c["model"] == "terse:1" and c["think"] is False]
    think = [c for c in calls if c["model"] == "terse:1" and c["think"] is True]
    deep = [c for c in calls if c["model"] == "deep:1"]
    assert len(polite) == len(terse) == len(think) == len(deep) == 40
    assert all(c["options"]["num_predict"] == 80 and c["options"]["stop"] == ["\n\n"]
               and c["options"]["num_ctx"] == 4096 and c["options"]["temperature"] == 0.9
               and "seed" in c["options"] for c in polite)
    assert {c["options"]["seed"] for c in polite} == {c["options"]["seed"] for c in think}
    # thinking: no stop, the budget on num_predict, num_ctx raised to fit
    assert all("stop" not in c["options"] and c["options"]["num_predict"] == 180
               and c["options"]["num_ctx"] == 5120 for c in think)
    assert all(c["think"] is None and "stop" not in c["options"]
               and c["options"]["num_predict"] == 80 + 1400 and c["options"]["num_ctx"] == 6144
               for c in deep)
    assert all(c["keep_alive"] == "5m" for c in polite + terse + think + deep)
    assert polite[0]["messages"][0]["content"] == SYSTEM
    assert not any(HINT in c["messages"][-1]["content"] for c in polite)  # the first request
    system = terse[0]["messages"][0]["content"]
    assert "Write only your line" in system and "Never mention AI" in system
    assert "other people's lines" in system and "Your slang is 2009 slang" not in system
    assert "You can be moody" not in system and "usually" not in system
    assert "React only to the chat shown" in system and "(nothing)" in system
    assert "You don't flirt with anyone in the guild." in system
    assert not [c for c in calls if c["model"] == "missing:1"]
    # room made first (the live model), each model unloaded when done, the embedding kept
    assert admin.unloaded[0] == cfg_live and {"polite:1", "terse:1", "deep:1"} <= set(admin.unloaded)
    assert cfg.models.embedding not in admin.unloaded
    order = [c["model"] for c in calls if c["model"] in ("polite:1", "terse:1", "deep:1")]
    assert order == sorted(order, key=["polite:1", "terse:1", "deep:1"].index)  # one at a time
    conn = sqlite3.connect(tmp_path / "o" / "results.sqlite")
    deep_lines = [json.loads(x) for (x,) in conn.execute(
        "SELECT lines FROM result WHERE candidate='deep' AND status='ok'")]
    conn.close()
    assert deep_lines and all(len(x) == 1 and "smiles" not in x[0] for x in deep_lines)


def test_from_sent_asks_the_request_whose_words_were_sent(data, tmp_path, clock):
    llm, admin, _ = ollama(data)
    assert run(data, tmp_path, "--turns", "40", "--windows", "0", "--from", "sent",
               "--out", str(tmp_path / "o"), llm=llm, admin=admin, clock=clock) == 0
    polite = [c for c in llm.calls if c.get("model") == "polite:1"]
    assert sum(HINT in c["messages"][-1]["content"] for c in polite) == 40 // RETRY_EVERY
    report = json.loads((tmp_path / "o" / "report.json").read_text())
    assert report["source"] == "sent"


def test_a_resumed_run_retries_only_what_failed(data, tmp_path, clock):
    llm, admin, _ = ollama(data, fail_model="terse:1", fail_text="/api/chat: HTTP 400 bad think")
    out = tmp_path / "out"
    assert run(data, tmp_path, "--turns", "30", "--windows", "2", "--out", str(out),
               llm=llm, admin=admin, clock=clock) == 0
    report = json.loads((out / "report.json").read_text())
    terse = next(c for c in report["candidates"] if c["name"] == "terse")
    assert terse["lines"]["error"] == 30 and terse["lines"]["codes"] == {"http_400": 30}
    assert "errors: http_400 30" in (out / "report.md").read_text()
    llm2, admin2, _ = ollama(data)
    assert run(data, tmp_path, "--turns", "99", "--windows", "9", "--from", "sent",
               "--out", str(out), llm=llm2, admin=admin2, clock=clock) == 0
    asked = [c for c in llm2.calls if "messages" in c
             and c["model"] in ("polite:1", "terse:1", "deep:1")]
    assert len(asked) == 60  # terse and terse-think again; polite and deep were already done
    report = json.loads((out / "report.json").read_text())
    assert report["turns"] == 30 and report["source"] == "first"  # the stored plan and source
    assert next(c for c in report["candidates"] if c["name"] == "terse")["lines"]["ok"] == 30


def test_resume_rejudges_a_window_skipped_for_a_failed_candidate(data, tmp_path, clock):
    """review-328 P2-2: a window skipped (or judged on partial lines) while a candidate's turns
    had failed is judged again once they answer."""
    llm, admin, _ = ollama(data, fail_model="terse:1")
    out = tmp_path / "out"
    assert run(data, tmp_path, "--turns", "30", "--windows", "2", "--out", str(out),
               llm=llm, admin=admin, clock=clock) == 0
    conn = sqlite3.connect(out / "results.sqlite")
    first = dict(conn.execute("SELECT status, count(*) FROM judged WHERE candidate='terse' "
                              "GROUP BY status").fetchall())
    conn.close()
    assert first == {"skipped": 2}
    llm2, admin2, _ = ollama(data)
    assert run(data, tmp_path, "--out", str(out), llm=llm2, admin=admin2, clock=clock) == 0
    report = json.loads((out / "report.json").read_text())
    terse = next(c for c in report["candidates"] if c["name"] == "terse")
    assert terse["judge"]["private"]["windows"] == 2 and terse["lines"]["ok"] == 30
    judged_again = [c for c in llm2.calls if c.get("messages")
                    and c["messages"][0]["content"].startswith("You are the judge")]
    # only the windows whose lines changed: terse's and terse-think's, two each
    assert len(judged_again) == 4


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


def test_an_interrupted_run_still_unloads_the_model_it_was_using(data, tmp_path, clock):
    llm, admin, _ = ollama(data)

    def stop(model, messages):
        raise KeyboardInterrupt
    llm._script = stop
    assert run(data, tmp_path, "--turns", "30", "--out", str(tmp_path / "o"), llm=llm,
               admin=admin, clock=clock) == 130
    assert "polite:1" in admin.unloaded and "polite:1" not in admin.resident


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
    assert "candidate polite: think false" in out
    assert ("candidate deep: think \"default\" (no flag: the model's own default): no stop, "
            "num_predict + 1400") in out
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


def test_live_and_first_are_cut_like_a_candidate_when_num_predict_cut_a_discord_reply(
        store, clock, tmp_path):
    """review-328 P3-1: a Discord reply cut off by num_predict is trimmed to a sentence end for
    the baselines too, as _ask trims it live and generate.ask trims a candidate's."""
    from bakeoff.rig import capture
    from leeroy_mind.discord import store as discord
    from leeroy_mind.protocol import Chat
    root = seed(store, clock.now(), members=(101,), per=1)
    t = clock.now() - 3600
    mid = discord.post(store, channel="general", author=101, origin="mind",
                       text="One whole sentence. And a cut", now=t)
    lid = store.insert_chat_line(Chat(0, "discord:general", 101, "Arkon", 0, "x", 0, 0),
                                 session_id=None, origin="mind", ts=t)
    capture(store, line_id=lid, ts=t, guid=101, text="One whole sentence. And a cut",
            channel="discord:general", discord_id=mid, words=90)
    store.conn.execute("UPDATE bakeoff_turn SET done_reason='length' WHERE line_id=?", (lid,))
    store.conn.commit()
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    llm, admin, _ = ollama(root)
    out = tmp_path / "o"
    assert run(root, tmp_path, "--kinds", "discord", "--windows", "0", "--out", str(out),
               llm=llm, admin=admin, clock=clock) == 0
    conn = sqlite3.connect(out / "results.sqlite")
    live = conn.execute("SELECT text FROM result WHERE candidate='live'").fetchone()[0]
    conn.close()
    assert live == "One whole sentence."


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
    ('[[candidate]]\nname = "first"\nmodel = "x"\n', "name"),
    ('[[candidate]]\nname = "a"\nmodel = "x"\nprompt_variant = "rules"\n', "prompt_variant"),
    ('[[candidate]]\nname = "a"\nmodel = "x"\nthink = "yes"\n', "think"),
    ('[[candidate]]\nname = "a"\nmodel = "x"\nnum_ctx = 100\n', "num_ctx"),
    ('[[candidate]]\nname = "a"\nmodel = "x"\ntemperature = 1\n', "unknown key temperature"),
    ('[[candidate]]\nname = "a"\nmodel = "x"\n[[candidate]]\nname = "A"\nmodel = "y"\n', "share a name"),
])
def test_a_bad_candidate_file_is_refused_by_key(tmp_path, text, why):
    path = tmp_path / "c.toml"
    path.write_text(text)
    with pytest.raises(CandidateError, match=why):
        load_candidates(path)


def test_candidate_think_is_three_way_and_false_when_left_out(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[[candidate]]\nname = "plain"\nmodel = "m"\n'
                    '[[candidate]]\nname = "off"\nmodel = "m"\nthink = false\n'
                    '[[candidate]]\nname = "on"\nmodel = "m"\nthink = true\n'
                    '[[candidate]]\nname = "own"\nmodel = "m"\nthink = "default"\n'
                    '[[candidate]]\nname = "big"\nmodel = "m"\nthink = "default"\n'
                    'think_budget = 2000\nnum_ctx = 8192\n')
    plain, off, on, own, big = load_candidates(path)
    assert (plain.think, plain.thinks, plain.think_budget, plain.context(4096)) == (False, False, 0, 4096)
    assert off == Candidate("off", "m")  # left out is false, as the live pipeline sends it
    assert (on.think, on.thinks, on.think_budget, on.context(4096)) == (True, True, 1400, 6144)
    assert (own.think, own.thinks, own.context(4096)) == (None, True, 6144)
    assert (big.think_budget, big.context(4096)) == (2000, 8192)
    assert (plain.prompt_variant, plain.timeout) == ("as_is", 120)
    for bad in ('"yes"', '"Default"', "1"):
        path.write_text(f'[[candidate]]\nname = "x"\nmodel = "m"\nthink = {bad}\n')
        with pytest.raises(CandidateError, match='think must be true, false .* or "default"'):
            load_candidates(path)


def test_a_candidate_left_without_think_sends_false(data, tmp_path, clock):
    llm, admin, _ = ollama(data)
    cfg = '[[candidate]]\nname = "plain"\nmodel = "polite:1"\n'
    assert run(data, tmp_path, "--turns", "10", "--windows", "0", "--out", str(tmp_path / "o"),
               llm=llm, admin=admin, clock=clock, cfg_text=cfg) == 0
    calls = [c for c in llm.calls if c.get("model") == "polite:1"]
    assert len(calls) == 10 and all(c["think"] is False and c["options"]["stop"] == ["\n\n"]
                                    and c["options"]["num_predict"] == 80 for c in calls)


def test_situation_keeps_the_hard_rules_bake_off_1_s_two_and_an_unknown_bullet():
    messages = [{"role": "system", "content": SYSTEM + "\n- An owner's tuning line.\n\nAfter."},
                {"role": "user", "content": "- Your slang is 2009 slang. (in the user message)"}]
    out = variants.situation(messages)
    system = out[0]["content"]
    assert "How you talk here:\n- You're not an assistant. Never mention AI" in system
    assert "- Don't narrate actions or write other people's lines." in system
    assert "- Write only your line: no name in front, no quotes." in system
    assert "- React only to the chat shown. Shared history comes only from your memories" in system
    assert "- If you wouldn't say anything, answer exactly (nothing)" in system
    assert "- An owner's tuning line." in system and system.endswith("\n\nAfter.")
    for gone in ("2009", "React to what", "moody", "guilt-trip", "usually"):
        assert gone not in system
    assert out[1] == messages[1] and messages[0]["content"].startswith("You are Belra")
    assert variants.as_is(messages) == messages


def test_a_beat_candidate_asks_its_own_beat_first(data, tmp_path, clock):
    """review-329: a beat candidate (beat = true) asks a beat through talk/beat.py on the request
    (which never holds the live beat), then its line with the beat's lines after the
    instruction; the result keeps the want code, never the words."""
    llm, admin, _ = ollama(data)
    cfg = ('[[candidate]]\nname = "polite-beat"\nmodel = "polite:1"\nthink = false\n'
           'beat = true\nprompt_variant = "situation"\n')
    out = tmp_path / "o"
    assert run(data, tmp_path, "--turns", "20", "--windows", "0", "--out", str(out), llm=llm,
               admin=admin, clock=clock, cfg_text=cfg) == 0
    calls = [c for c in llm.calls if c.get("model") == "polite:1"]
    beat_calls = [c for c in calls if c["format"] is not None]
    line_calls = [c for c in calls if c["format"] is None]
    assert len(beat_calls) == len(line_calls) == 20
    assert all(c["think"] is False and c["options"]["num_predict"] == 240 for c in beat_calls)
    assert all(c["messages"][-1]["content"].endswith("You want to: just react") for c in line_calls)
    assert all("Your slang is 2009" not in c["messages"][0]["content"] for c in beat_calls)
    conn = sqlite3.connect(out / "results.sqlite")
    stored = conn.execute("SELECT beat, text FROM result WHERE candidate='polite-beat'").fetchall()
    conn.close()
    assert {b for b, _ in stored} == {"react"}
    assert not any("he asks if she is around" in (t or "") for _, t in stored)
    report = json.loads((out / "report.json").read_text())
    beat = next(c for c in report["candidates"] if c["name"] == "polite-beat")
    assert beat["beat"] is True and beat["lines"]["beat"] == {
        "used": 20, "asked": 20, "wants": {"react": 20}, "skipped": {}}
    md = (out / "report.md").read_text()
    assert "situation + beat" in md and "beat used on 20 of 20 turns; wants: react 20" in md
    assert "he asks if she is around" not in md


def test_beat_items_are_read_back_from_the_request():
    from leeroy_mind.bakeoff import beat
    user = ("You remember:\n- 2d ago: we cleared ICC together\n\nChat:\n"
            "[3m ago] Belra (whisper to Zan): her own earlier line\n"
            "[2m ago] Zan (whisper to Belra): his earlier line\n"
            "[just now] Zan (whisper to Belra): the line she answers\n\nAnswer Zan.")
    assert beat.items([{"role": "system", "content": "card"}, {"role": "user", "content": user}],
                      "Belra") == ["we cleared ICC together", "his earlier line"]
