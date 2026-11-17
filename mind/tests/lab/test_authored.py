"""Hand-written variants (lab/authored.py): the folder, the screen, the night's propose unit
(the waiting files first, the model for the slots left, one second ask after refusals) and the
CLI that queues one. No model but a scripted FakeLLM."""
import asyncio
import json
import logging
from dataclasses import replace

from lab.rig import LIVE_TOML, digest, make_live, night_ctx, title_of, variant_text
from leeroy_mind.lab import authored, propose
from leeroy_mind.lab import night as lab_night
from leeroy_mind.lab import store as lab_store
from leeroy_mind.lab.cli import lab_main
from leeroy_mind.lab.config import load
from leeroy_mind.talk import pieces

SCRIPTS = {"flirt-3", "smalltalk-3", "meet-3", "pushback-3"}
HAND = ("{addressee} already knows what {they} wrote. Skip past it and start with your own "
        "answer, in one short line. {extra}")
LOST = ("Answer {addressee}'s last line. React to what {they} actually said, staying true to "
        "yourself. Keep it brief. Use a memory or joke only if it fits. {extra}")


def put(live, piece, name, text):
    folder = authored.folder(live, piece)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.txt").write_bytes(text.encode())


def propose_only(live, reflection, *, cfg=None, piece=None):
    cfg = cfg or load(live / "mind.toml")

    async def go():
        ctx, llm = night_ctx(live, reflection)
        try:
            await lab_night.make_propose(cfg, scripts=SCRIPTS, members={102, 103}, piece=piece)(ctx)
            with ctx.store.transaction():
                run = lab_store.night_run(ctx.store.conn, ctx.night)
                variants = lab_store.variants(ctx.store.conn, run["id"]) if run else []
                written = authored.names(ctx.store.conn, [v["id"] for v in variants])
            return run, variants, written, dict(ctx.failures)
        finally:
            ctx.store.close()
    return asyncio.run(go())


def never(model, messages):
    raise AssertionError(f"the model was asked: {title_of(messages)}")


def test_the_folder_reads_one_paragraph_a_file_in_name_order(live):
    assert authored.read(live, "reply_human") == [] and authored.read(live, "nope") == []
    put(live, "reply_human", "b-second", "  " + HAND.replace(". Skip", ".\r\n\r\nSkip") + "\n")
    put(live, "reply_human", "a-first", variant_text("Plainly"))
    put(live, "reply_human", "empty", " \n")
    put(live, "reply_human", "huge", "x" * (authored.MAX_BYTES + 1))
    (authored.folder(live, "reply_human") / "notes.md").write_bytes(b"not a variant")
    (authored.folder(live, "reply_human") / "bad.txt").write_bytes(b"\xff\xfe\xfa")
    assert authored.read(live, "reply_human") == [("a-first", variant_text("Plainly")),
                                                 ("b-second", HAND)]


def test_waiting_screens_as_the_proposer_does_but_for_the_length(live):
    put(live, "reply_human", "a", HAND)                      # 116 characters: short is fine
    put(live, "reply_human", "b", LOST)                      # the kept phrase reworded
    put(live, "reply_human", "c", HAND.replace("{they}", "he"))
    put(live, "reply_human", "d", HAND + " ")                # the same text as a
    put(live, "reply_human", "e", variant_text("Truly"))
    put(live, "reply_human", "f", pieces.builtin("reply_human"))
    put(live, "reply_human", "g", variant_text("As a bot, truly"))
    ready, refused = authored.waiting(live, "reply_human", earlier=[variant_text("Truly")])
    assert [v["name"] for v in ready] == ["a"] and ready[0]["text"] == HAND
    assert "[-" in ready[0]["diff"] and "{+" in ready[0]["diff"]
    assert refused == ["kept", "placeholders", "meta"]      # tried or repeated ones are not faults
    assert authored.pieces_waiting(live, lambda n: []) == {"reply_human"}
    assert authored.pieces_waiting(live, lambda n: [HAND, variant_text("Truly")]) == set()


def test_the_night_tries_the_waiting_files_first_and_asks_the_model_for_the_rest(live, caplog):
    put(live, "reply_human", "a-no-echo", HAND)
    put(live, "reply_human", "b-broken", LOST)
    asked = []

    def reflection(model, messages):
        assert title_of(messages) == "lab_variants"
        asked.append(messages[-1]["content"])
        return json.dumps({"variants": [{"text": variant_text("Plainly"), "idea": "plainer"},
                                        {"text": HAND, "idea": "the file again"},
                                        {"text": variant_text("Truly"), "idea": "truer"}]})
    before = digest(live / "lab")
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run, variants, written, failures = propose_only(live, reflection)
    assert not failures and run["piece"] == "reply_human"
    assert [v["text"] for v in variants] == [HAND, variant_text("Plainly"), variant_text("Truly")]
    assert written == {variants[0]["id"]: "a-no-echo"}
    assert len(asked) == 1 and "Write 4 different variants." in asked[0]   # two slots were left
    assert digest(live / "lab") == before                                  # the files stay
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("lab_proposed"))
    assert "kept=3 authored=1 asks=1 refused=2 codes=kept,same" in line
    plan = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("lab_plan"))
    assert "authored=1" in plan
    # the next night: the file was tried, so it is done and no longer preferred
    assert authored.waiting(live, "reply_human", earlier=[v["text"] for v in variants])[0] == []


def test_enough_waiting_files_need_no_model(live):
    for i, word in enumerate(("Plainly", "Truly", "Simply", "Honestly")):
        put(live, "reply_human", f"v{i}", variant_text(word))
    run, variants, written, failures = propose_only(live, never)
    assert not failures
    assert [v["text"] for v in variants] == [variant_text(w) for w in ("Plainly", "Truly", "Simply")]
    assert sorted(written.values()) == ["v0", "v1", "v2"]      # the fourth waits for another night


def test_refused_proposals_get_one_second_ask_with_what_was_wrong(live, caplog):
    asked = []

    def reflection(model, messages):
        text = messages[-1]["content"]
        asked.append(text)
        if "Your earlier variants were refused by the code:" not in text:
            return json.dumps({"variants": [{"text": LOST, "idea": "react"},
                                            {"text": LOST.replace("React", "Reply"), "idea": "reply"},
                                            {"text": variant_text("Plainly"), "idea": "plainer"}]})
        return json.dumps({"variants": [{"text": variant_text("Truly"), "idea": "truer"},
                                        {"text": variant_text("Simply"), "idea": "simpler"},
                                        {"text": variant_text("Honestly"), "idea": "one too many"}]})
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run, variants, written, failures = propose_only(live, reflection)
    assert not failures and not written
    assert [v["text"] for v in variants] == [variant_text(w) for w in ("Plainly", "Truly", "Simply")]
    assert len(asked) == 2
    assert 'it lost the locked phrase "in one short line": copy it letter for letter' in asked[1]
    assert "Write 4 new variants without those faults." in asked[1]
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("lab_proposed"))
    assert "kept=3 authored=0 asks=2 refused=3 codes=extra,kept" in line


def test_a_night_that_keeps_every_proposal_asks_once(live):
    asked = []

    def reflection(model, messages):
        asked.append(1)
        return json.dumps({"variants": [{"text": variant_text(w), "idea": w}
                                        for w in ("Plainly", "Truly", "Simply", "Honestly")]})
    run, variants, written, failures = propose_only(live, reflection)
    assert len(variants) == 3 and len(asked) == 1 and not failures


def test_a_failed_model_call_keeps_the_waiting_files(live):
    put(live, "reply_human", "a", HAND)

    def reflection(model, messages):
        return "not json"
    run, variants, written, failures = propose_only(live, reflection)
    assert [v["text"] for v in variants] == [HAND] and not failures


def test_a_failed_model_call_with_nothing_waiting_fails_the_unit(live):
    run, variants, written, failures = propose_only(live, lambda model, messages: "not json")
    assert not variants and failures == {"lab_propose": 1}


def test_the_plan_prefers_a_waiting_piece_and_passes_over_unrendered_ones(tmp_path):
    live = make_live(tmp_path)
    put(live, "goodnight", "warm", "{addressee} is off to bed. Say goodnight like you mean it, "
                                   "in a few words. {extra}")
    one = replace(load(live / "mind.toml"), variants=1)
    run, variants, written, _ = propose_only(live, never, cfg=one)
    assert run["piece"] == "goodnight" and len(variants) == 1 and list(written.values()) == ["warm"]
    # [talk] situation on: a reply reads the situation template, so reply_human and continue
    # could only end untested; the round-robin begins at the first piece that is rendered
    (tmp_path / "s").mkdir()
    live = make_live(tmp_path / "s", LIVE_TOML.replace("chattiness = 4", "chattiness = 4\nsituation = true")
                     + "[romance]\nrealism = true\n")
    assert lab_night.skipped_pieces(live) == {"reply_human", "continue", "romance_hook"}

    def reflection(model, messages):
        assert "{addressee} is logging off." in messages[-1]["content"]
        return json.dumps({"variants": [{"text": "x", "idea": "y"}]})
    run, variants, written, _ = propose_only(live, reflection, cfg=load(live / "mind.toml"))
    assert run["piece"] == "goodnight"
    # an unrendered piece is not preferred for its files either, but `lab run --piece` names it
    put(live, "reply_human", "a", HAND)
    (tmp_path / "f").mkdir()
    forced = make_live(tmp_path / "f")
    put(forced, "discord_dm", "a", propose.EXAMPLES["discord_dm"].replace("Match", "Meet"))
    run, variants, written, _ = propose_only(forced, never, cfg=one, piece="discord_dm")
    assert run["piece"] == "discord_dm" and len(variants) == 1


def test_add_variant_screens_and_queues_a_file_and_variants_lists_them(tmp_path, capsys):
    live = make_live(tmp_path)
    src = tmp_path / "mine"
    src.mkdir()
    (src / "a-no-echo.txt").write_bytes((HAND.replace(". Skip", ".\n  Skip") + "\n").encode())
    (src / "lost.txt").write_bytes(LOST.encode())
    (src / "named.txt").write_bytes(HAND.replace("your own", "Testmage's").encode())
    before = digest(live)
    base = ["--data-dir", str(live)]
    assert lab_main(base + ["add-variant", "--piece", "reply_human", "--file", str(src / "lost.txt")]) == 1
    assert 'refused (kept): it lost the locked phrase "in one short line"' in capsys.readouterr().err
    assert lab_main(base + ["add-variant", "--piece", "reply_human", "--file", str(src / "named.txt")]) == 1
    assert "refused (names)" in capsys.readouterr().err
    assert lab_main(base + ["add-variant", "--piece", "reply_human", "--file", str(src / "nope.txt")]) == 2
    assert lab_main(base + ["add-variant", "--piece", "reply_human", "--file",
                            str(src / "a-no-echo.txt"), "--name", "bad name"]) == 1
    capsys.readouterr()
    assert digest(live) == before                      # a refusal writes nothing
    assert lab_main(base + ["add-variant", "--piece", "reply_human", "--file", str(src / "a-no-echo.txt")]) == 0
    assert "is waiting for the next lab night of reply_human" in capsys.readouterr().out
    stored = authored.folder(live, "reply_human") / "a-no-echo.txt"
    assert stored.read_bytes() == (HAND + "\n").encode()
    changed = digest(live)
    assert {k for k in changed if changed[k] != before.get(k)} == {str(stored.relative_to(live))}
    # the same text again is fine under its name, refused under another; another text under it too
    assert lab_main(base + ["add-variant", "--piece", "reply_human", "--file", str(src / "a-no-echo.txt")]) == 0
    assert lab_main(base + ["add-variant", "--piece", "reply_human", "--file",
                            str(src / "a-no-echo.txt"), "--name", "twin"]) == 1
    (src / "other.txt").write_bytes(variant_text("Plainly").encode())
    assert lab_main(base + ["add-variant", "--piece", "reply_human", "--file", str(src / "other.txt"),
                            "--name", "a-no-echo"]) == 1
    assert "already there with another text" in capsys.readouterr().err
    put(live, "reply_human", "z-lost", LOST)
    assert lab_main(base + ["variants"]) == 0
    out = capsys.readouterr().out
    assert "reply_human:" in out and "a-no-echo: waiting, 116 characters" in out
    assert "z-lost: refused (kept)" in out and HAND not in out
    assert lab_main(base + ["run", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "hand-written variants are waiting for reply_human" in out and "piece reply_human" in out
    assert lab_main(base + ["run", "--dry-run", "--piece", "beat_ask"]) == 0
    assert "piece beat_ask" in capsys.readouterr().out


def test_the_report_names_a_hand_written_variant(live, capsys):
    put(live, "reply_human", "a-no-echo", HAND)
    propose_only(live, never, cfg=replace(load(live / "mind.toml"), variants=1))
    assert lab_main(["--data-dir", str(live), "report"]) == 0
    assert "(hand-written: a-no-echo)" in capsys.readouterr().out
