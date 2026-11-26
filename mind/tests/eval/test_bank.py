"""`eval bank` on invented data (tests/bakeoff/rig.py: his whispers with the test cast, every
member line a captured turn, every fourth one asked twice live after a silent first draft) with
a FakeLLM and a fake Ollama admin. The bank freezes the captured turns that answer his line,
split by conversation with the salt it stores; `stats` and `run` print numbers only; the test
half needs --confirm-final and is logged before it is read; the 95% CI resamples
conversations. Nothing here is real guild data."""
import json
import re
import statistics
from pathlib import Path

import pytest

from bakeoff.rig import HIS, MEMBER, OPTIONS, FakeAdmin, RETRY_EVERY, capture, digest, seed
from conftest import TEST_CAST
from leeroy_mind.protocol import Chat
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.config import load_config
from leeroy_mind.eval import bank
from leeroy_mind.lab import judge
from leeroy_mind.nightly import cli as nightly_cli
from leeroy_mind.talk import beat as beats
from leeroy_mind.testing.fake_llm import FakeLLM

MEMBERS = (101, 102, 103, 104, 105)
CONVS = tuple(f"private:{g}" for g in MEMBERS)
PER = 8
SHORT = "ok CANDIDATE_TOKEN"     # shorter than every stored member line: the judge prefers it
LONG = ("well CANDIDATE_TOKEN let me tell you everything I think about this in many words, "
        "because there is so much to say")
# words of the invented lines, names and replies that must never reach a terminal or a log
PRIVATE = ("lovely", "glad you asked", "absolutely", "Count me in", "what's up", "wanna run icc",
           "Arkon", "Belra", "Coros", "Dunna", "Ebbo", "Testmage", "Zan", "invented test member",
           "CANDIDATE_TOKEN", "Answer Zan")
_REPLY = re.compile(r"^Her reply \(\d+ words?\): (.*)$", re.MULTILINE)


class Ollama(FakeLLM):
    """Candidates answer by model; the pairwise judge prefers the shorter reply; what is loaded
    is tracked for the fake admin."""

    def __init__(self, resident: set, models):
        super().__init__(self.answer, models=models)
        self.resident = resident

    @staticmethod
    def answer(model, messages):
        if messages[0]["content"] == judge.PAIR_SYSTEM:
            a, b = _REPLY.findall(messages[-1]["content"])
            better = "A" if len(a) < len(b) else "B" if len(b) < len(a) else "same"
            return json.dumps({"better": better})
        if beats.MARK in messages[-1]["content"]:
            return json.dumps({"read": "he asks if she is around", "lands": "fine",
                               "want": "react", "use": None})
        return {"short:1": SHORT, "long:1": LONG}[model]

    async def _chat(self, model, messages, **kw):
        self.resident.add(model)
        return await super()._chat(model, messages, **kw)


def ollama(root: Path, *, loaded=()):
    cfg = load_config(root / "mind.toml")
    resident = set(loaded) | {cfg.models.embedding}
    llm = Ollama(resident, {"short:1", "long:1", cfg.models.reflection, cfg.models.embedding})
    return llm, FakeAdmin(resident), cfg


@pytest.fixture
def data(store, clock, monkeypatch):
    monkeypatch.setattr(nightly_cli, "SystemClock", lambda: clock)
    return seed(store, clock.now(), members=MEMBERS, per=PER)


def seed_dms(store, now, *, members=MEMBERS, per=4) -> None:
    """His Discord DMs with each member, each answer a captured turn of one message."""
    from leeroy_mind.discord import store as discord
    t = now - 2 * 86400
    for m in members:
        channel = f"dm:{m}"
        for i in range(per):
            t += 40
            discord.post(store, channel=channel, author=1, origin="human", text=HIS[i % len(HIS)],
                         now=t)
            t += 20
            text = MEMBER[(i + m) % len(MEMBER)]
            mid = discord.post(store, channel=channel, author=m, origin="mind", text=text, now=t)
            lid = store.insert_chat_line(Chat(0, channel, m, TEST_CAST[m][0], 0, text, 0, 0),
                                         session_id=None, origin="mind", ts=t)
            capture(store, line_id=lid, ts=t, guid=m, text=text, channel=channel, discord_id=mid,
                    words=90, options=dict(OPTIONS, num_predict=168))
        t += 3600
    store.conn.commit()
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def both_ways(convs=CONVS) -> str:
    """The first of a fixed list of salts that puts some conversations in each split."""
    return next(s for s in (f"salt-{i}" for i in range(200))
                if {bank.split_of(s, c) for c in convs} == {"dev", "test"})


def eval_bank(root, *args, llm=None, admin=None, wow=False):
    return nightly_cli.eval_main(["bank", *args, "--data-dir", str(root)],
                                 make_llm=(lambda cfg: llm) if llm is not None else None,
                                 make_admin=(lambda cfg: admin) if admin is not None else None,
                                 wow_open=lambda: wow)


def cases(root, version, split):
    path = root / "eval" / "bank" / version / f"{split}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def manifest(root, version):
    return json.loads((root / "eval" / "bank" / version / "manifest.json").read_text(encoding="utf-8"))


def no_text(*outputs):
    joined = "\n".join(outputs)
    found = [w for w in PRIVATE if w in joined]
    assert not found, found


# -- the split ------------------------------------------------------------------------------------

def test_split_is_a_salted_hash_of_the_conversation_about_seventy_thirty():
    ids = [f"conv-{i}" for i in range(4000)]
    first = [bank.split_of("salt-a", c) for c in ids]
    assert first == [bank.split_of("salt-a", c) for c in ids]  # deterministic
    assert set(first) == {"dev", "test"}
    assert 0.67 < first.count("dev") / len(ids) < 0.73
    other = [bank.split_of("salt-b", c) for c in ids]
    assert other != first  # the salt decides


def test_build_freezes_answers_to_him_split_by_conversation_with_the_stored_salt(
        data, monkeypatch, capsys):
    salt = both_ways()
    monkeypatch.setattr(bank, "new_salt", lambda: salt)
    before = digest(data / "mind.db")
    assert eval_bank(data, "build", "--version", "v1") == 0
    printed = capsys.readouterr()
    assert digest(data / "mind.db") == before
    no_text(printed.out, printed.err)
    m = manifest(data, "v1")
    assert m["salt"] == salt and m["version"] == "v1" and m["format"] == bank.FORMAT
    dev, test = cases(data, "v1", "dev"), cases(data, "v1", "test")
    assert len(dev) + len(test) == len(MEMBERS) * PER
    dev_convs = {c["conversation"] for c in dev}
    test_convs = {c["conversation"] for c in test}
    assert dev_convs and test_convs and not dev_convs & test_convs
    assert dev_convs | test_convs == set(CONVS)
    for split, rows in (("dev", dev), ("test", test)):
        assert all(bank.split_of(m["salt"], c["conversation"]) == split for c in rows)
        assert m["counts"][split]["cases"] == len(rows)
        assert m["counts"][split]["conversations"] == len({c["conversation"] for c in rows})
        assert m["counts"][split]["kinds"] == {"whisper": len(rows)}
    case = dev[0]
    assert case["source"] == "turn" and case["kind"] == "whisper" and case["family"] == "private"
    assert case["his"] and case["request"]["messages"] and case["reference"]["model"] == "live-model"
    # the request is the generation's first: a retried turn keeps its silent first draft
    retried = [c for c in dev + test if c["reference"]["text"] == "(nothing)"]
    assert len(retried) == len(MEMBERS) * PER // RETRY_EVERY
    assert all("Answer Zan's whisper in a line" not in c["request"]["messages"][-1]["content"]
               for c in retried)
    written = {p.relative_to(data).as_posix() for p in data.rglob("*") if p.is_file()}
    assert {p for p in written if not p.startswith("mind.")} == {
        "eval/bank/v1/manifest.json", "eval/bank/v1/dev.jsonl", "eval/bank/v1/test.jsonl"}


def test_the_same_salt_freezes_the_same_split_and_every_salt_keeps_conversations_whole(
        data, monkeypatch):
    salt, real = both_ways(), bank.new_salt
    monkeypatch.setattr(bank, "new_salt", lambda: salt)
    assert eval_bank(data, "build", "--version", "a") == 0
    assert eval_bank(data, "build", "--version", "b") == 0
    for split in bank.SPLITS:
        assert cases(data, "a", split) == cases(data, "b", split)
    monkeypatch.setattr(bank, "new_salt", real)
    for n in range(4):  # fresh random salts
        version = f"r{n}"
        assert eval_bank(data, "build", "--version", version) == 0
        seen = {}
        for split in bank.SPLITS:
            for c in cases(data, version, split):
                assert seen.setdefault(c["conversation"], split) == split


def test_a_members_whispers_and_dms_are_one_conversation_in_one_split(data, store, clock):
    seed_dms(store, clock.now())
    for n in range(4):  # fresh random salts
        version = f"r{n}"
        assert eval_bank(data, "build", "--version", version) == 0
        rows = {split: cases(data, version, split) for split in bank.SPLITS}
        assert sum(len(r) for r in rows.values()) == len(MEMBERS) * (PER + 4)
        kinds = {}
        for split, found in rows.items():
            for c in found:
                assert c["conversation"] == f"private:{c['guid']}"
                assert kinds.setdefault(c["conversation"], (split, set()))[0] == split
                kinds[c["conversation"]][1].add(c["kind"])
        assert set(kinds) == set(CONVS)
        assert all(found == {"whisper", "dm"} for _, found in kinds.values())


def test_rebuilding_a_version_needs_force_and_keeps_the_test_log(data, monkeypatch, capsys):
    monkeypatch.setattr(bank, "new_salt", lambda: "salt-old")
    assert eval_bank(data, "build", "--version", "v1") == 0
    log = data / "eval" / "bank" / "v1" / bank.TEST_LOG
    log.write_text('{"id": "x", "model": "m", "time": "t"}\n', encoding="utf-8")
    capsys.readouterr()
    assert eval_bank(data, "build", "--version", "v1") == 1
    assert "--force" in capsys.readouterr().err
    assert manifest(data, "v1")["salt"] == "salt-old"
    monkeypatch.setattr(bank, "new_salt", lambda: "salt-new")
    assert eval_bank(data, "build", "--version", "v1", "--force") == 0
    assert manifest(data, "v1")["salt"] == "salt-new"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["id"] == "x"
    assert json.loads(lines[1])["event"] == "rebuilt" and len(lines) == 2
    assert not [p for p in (data / "eval" / "bank").iterdir() if p.name != "v1"]


def test_a_bad_version_name_or_nothing_to_freeze_is_refused(store, clock, monkeypatch, tmp_path,
                                                            capsys):
    monkeypatch.setattr(nightly_cli, "SystemClock", lambda: clock)
    root = seed(store, clock.now(), members=MEMBERS, per=PER)
    with pytest.raises(SystemExit):
        eval_bank(root, "build", "--version", "../out")
    store.conn.execute("DELETE FROM bakeoff_turn")
    store.conn.commit()
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    capsys.readouterr()
    assert eval_bank(root, "build", "--version", "empty") == 1
    assert "nothing to freeze" in capsys.readouterr().err
    assert not (root / "eval" / "bank" / "empty").exists()


# -- stats ----------------------------------------------------------------------------------------

def test_stats_prints_counts_per_split_and_kind_and_no_text(data, monkeypatch, capsys):
    monkeypatch.setattr(bank, "new_salt", both_ways)
    assert eval_bank(data, "build", "--version", "v1") == 0
    capsys.readouterr()
    assert eval_bank(data, "stats") == 0  # the newest bank
    out = capsys.readouterr()
    no_text(out.out, out.err)
    m = manifest(data, "v1")
    for split in bank.SPLITS:
        n = m["counts"][split]
        assert (f"{split}: {n['cases']} cases, {n['conversations']} conversations; "
                f"kinds: whisper {n['cases']}") in out.out
    assert "files: intact" in out.out and "test runs: 0" in out.out
    (data / "eval" / "bank" / "v1" / "dev.jsonl").write_text("{}\n", encoding="utf-8")
    assert eval_bank(data, "stats", "--version", "v1") == 0
    assert "files: changed since the build (dev)" in capsys.readouterr().out


# -- run ------------------------------------------------------------------------------------------

def test_a_dev_run_regenerates_judges_pairwise_and_prints_a_ci(data, monkeypatch, capsys):
    monkeypatch.setattr(bank, "new_salt", both_ways)
    assert eval_bank(data, "build", "--version", "v1") == 0
    dev = cases(data, "v1", "dev")
    read = []
    real = bank.load_cases
    monkeypatch.setattr(bank, "load_cases", lambda d, m, s: read.append(s) or real(d, m, s))
    llm, admin, cfg = ollama(data, loaded={"live-model"})
    before = digest(data / "mind.db")
    capsys.readouterr()
    assert eval_bank(data, "run", "--model", "long:1", "--split", "dev", llm=llm, admin=admin) == 0
    out = capsys.readouterr()
    no_text(out.out, out.err)
    assert read == ["dev"] and digest(data / "mind.db") == before
    # the long reply loses every comparison and wins only where the live first draft was silent:
    # a quarter of every conversation, so every resample of conversations reads 0.25
    assert (f"bank run: version=v1 split=dev model=long:1 cases={len(dev)} "
            f"conversations={len({c['conversation'] for c in dev})} samples=1") in out.out
    given = [len(c["reference"]["text"].split()) for c in dev if c["reference"]["text"] != "(nothing)"]
    assert (f"answers: ok={len(dev)} silent=0 errors=0 median_words={len(LONG.split())} "
            f"reference_median_words={statistics.median(given):g}") in out.out
    assert "win_rate=0.2500 ci95_low=0.2500 ci95_high=0.2500" in out.out
    assert "kind=whisper win_rate=0.2500" in out.out
    calls = [c for c in llm.calls if c["model"] == "long:1"]
    assert len(calls) == len(dev)
    assert {c["messages"][-1]["content"] for c in calls} == {
        c["request"]["messages"][-1]["content"] for c in dev}
    assert all(c["think"] is False and "seed" in c["options"] for c in calls)
    pair_calls = [c for c in llm.calls if c["model"] == cfg.models.reflection]
    assert len(pair_calls) == 2 * sum(c["reference"]["text"] != "(nothing)" for c in dev)
    assert "live-model" in admin.unloaded and "long:1" in admin.unloaded
    assert cfg.models.reflection in admin.unloaded
    assert admin.resident == {cfg.models.embedding}
    assert not (data / "eval" / "bank" / "v1" / bank.TEST_LOG).exists()


def test_errors_and_silences_count_as_losses_and_samples_differ_by_seed(data, monkeypatch, capsys):
    monkeypatch.setattr(bank, "new_salt", both_ways)
    assert eval_bank(data, "build", "--version", "v1") == 0
    dev = cases(data, "v1", "dev")
    llm, admin, _ = ollama(data)
    llm.fail_next(1)
    capsys.readouterr()
    assert eval_bank(data, "run", "--model", "short:1", "--split", "dev", "--samples", "2",
                     llm=llm, admin=admin) == 0
    out = capsys.readouterr().out
    n = 2 * len(dev)
    assert f"answers: ok={n - 1} silent=0 errors=1" in out
    assert f"win_rate={(n - 1) / n:.4f}" in out and f"decided={n}" in out
    seeds = [c["options"]["seed"] for c in llm.calls if c["model"] == "short:1"]
    assert len(seeds) == len(set(seeds)) == n  # a seed per case and sample


def test_the_test_split_is_locked_without_confirm_final_and_logged_with_it(data, monkeypatch, capsys):
    monkeypatch.setattr(bank, "new_salt", both_ways)
    assert eval_bank(data, "build", "--version", "v1") == 0
    test = cases(data, "v1", "test")
    log = data / "eval" / "bank" / "v1" / bank.TEST_LOG
    llm, admin, _ = ollama(data)
    capsys.readouterr()
    assert eval_bank(data, "run", "--model", "short:1", "--split", "test", llm=llm, admin=admin) == 1
    assert "--confirm-final" in capsys.readouterr().err
    assert llm.calls == [] and not log.exists()
    assert eval_bank(data, "run", "--model", "short:1", "--split", "test", "--confirm-final",
                     llm=llm, admin=admin) == 0
    out = capsys.readouterr()
    no_text(out.out, out.err)
    assert f"split=test model=short:1 cases={len(test)}" in out.out
    assert "win_rate=1.0000" in out.out
    entries = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1 and set(entries[0]) == {"id", "model", "time"}
    assert entries[0]["model"] == "short:1" and re.fullmatch(r"[0-9a-f]{12}", entries[0]["id"])
    assert entries[0]["time"].endswith("Z")
    no_text(log.read_text(encoding="utf-8"))
    capsys.readouterr()
    assert eval_bank(data, "stats") == 0
    assert "test runs: 1" in capsys.readouterr().out


def test_a_bank_changed_after_its_build_is_refused(data, monkeypatch, capsys):
    monkeypatch.setattr(bank, "new_salt", both_ways)
    assert eval_bank(data, "build", "--version", "v1") == 0
    path = data / "eval" / "bank" / "v1" / "dev.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    llm, admin, _ = ollama(data)
    capsys.readouterr()
    assert eval_bank(data, "run", "--model", "short:1", "--split", "dev", llm=llm, admin=admin) == 1
    assert "changed since it was built" in capsys.readouterr().err
    assert [c for c in llm.calls if "messages" in c] == []


def test_a_run_refuses_while_the_mind_runs_wow_is_open_a_model_is_missing(data, monkeypatch, capsys):
    monkeypatch.setattr(bank, "new_salt", both_ways)
    assert eval_bank(data, "build", "--version", "v1") == 0
    llm, admin, _ = ollama(data)
    capsys.readouterr()
    with acquire_instance_lock(data):
        assert eval_bank(data, "run", "--model", "short:1", "--split", "dev", llm=llm, admin=admin) == 1
    assert "the mind is running" in capsys.readouterr().err
    assert eval_bank(data, "run", "--model", "short:1", "--split", "dev", llm=llm, admin=admin,
                     wow=True) == 1
    assert "WoW is open" in capsys.readouterr().err
    assert eval_bank(data, "run", "--model", "absent:1", "--split", "dev", llm=llm, admin=admin) == 1
    assert "not pulled" in capsys.readouterr().err
    assert llm.calls == []


def test_a_turn_that_had_a_beat_live_asks_the_candidate_its_own_beat_first(
        data, store, monkeypatch, capsys):
    store.conn.execute("UPDATE bakeoff_turn SET beat = 1")
    store.conn.commit()
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    monkeypatch.setattr(bank, "new_salt", both_ways)
    assert eval_bank(data, "build", "--version", "v1") == 0
    dev = cases(data, "v1", "dev")
    assert all(c["beat"] and c["who"]["speaker"] for c in dev)
    llm, admin, _ = ollama(data)
    assert eval_bank(data, "run", "--model", "short:1", "--split", "dev", llm=llm, admin=admin) == 0
    calls = [c for c in llm.calls if c["model"] == "short:1"]
    beat_calls = [c for c in calls if c["format"] is not None]
    assert len(beat_calls) == len(calls) - len(beat_calls) == len(dev)
    assert all(c["options"]["num_predict"] == manifest(data, "v1")["beat_predict"] for c in beat_calls)


# -- the confidence interval ----------------------------------------------------------------------

def test_the_ci_resamples_conversations_not_turns():
    # one conversation: every resample is that conversation, so its turns' spread is no CI width
    assert bank.bootstrap_ci({"a": [1.0, 0.0, 1.0, 0.0]}, rounds=500, seed=1) == (0.5, 0.5)
    assert bank.bootstrap_ci({}, rounds=500, seed=1) is None
    assert bank.bootstrap_ci({"a": [1.0], "b": [1.0, 1.0]}, rounds=500, seed=1) == (1.0, 1.0)


def test_the_ci_on_a_fixed_sample():
    groups = {"c1": [1.0, 1.0, 0.0], "c2": [0.0], "c3": [0.5, 1.0], "c4": [1.0], "c5": [0.0, 0.0],
              "c6": [1.0, 0.5, 0.5, 1.0]}
    assert bank.win_rate(groups) == pytest.approx(7.5 / 13)  # pooled over comparisons
    ci = bank.bootstrap_ci(groups, rounds=2000, seed=7)
    assert ci == bank.bootstrap_ci(groups, rounds=2000, seed=7)  # the seed fixes it
    # pinned; a plain reimplementation (draw 6 conversations, pool, 2.5th/97.5th) agrees
    assert ci == (pytest.approx(0.25), pytest.approx(10 / 13))
    low, high = ci
    assert low < bank.win_rate(groups) < high
    # a fair coin in 400 one-turn conversations: about 0.5 +- 1.96 * sqrt(0.25 / 400)
    coin = {f"c{i}": [float(i % 2)] for i in range(400)}
    low, high = bank.bootstrap_ci(coin, rounds=2000, seed=3)
    assert 0.44 < low < 0.48 and 0.52 < high < 0.56
