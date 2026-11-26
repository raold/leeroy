"""[selfies] detailer: the switch, the keys it puts on a render request, and its way through the
profile sets, the situational and request photos and `selfies try`, against the fake worker.
Off, every request is byte for byte what it was. Every person here is invented."""
import json
import logging
import sys
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.config import ConfigError
from leeroy_mind.selfies import cli
from leeroy_mind.selfies.config import SelfieCfg, load_selfie_config
from leeroy_mind.selfies.prompt import PromptSpec
from leeroy_mind.selfies.render import render_candidates
from leeroy_mind.selfies.worker import Detail, Rendered, SelfieWorker, detail_for

from selfies.fakes import fake_command, request, selfie_cfg
from selfies.test_request_render import Room, Worker, plan, render
from selfies.test_request_scene import app, save  # noqa: F401
from selfies.test_selfie_cli import calls, llm, root, snap_llm, snaproot  # noqa: F401


# -- the switch ---------------------------------------------------------------------------------

def test_the_detailer_is_on_by_default_and_a_table_turns_it_off(tmp_path):
    assert (SelfieCfg().detailer, SelfieCfg().detailer_hands) == (True, True)  # since 2026-11-25
    (tmp_path / "mind.toml").write_text("[selfies]\ndetailer = true\n", encoding="utf-8")
    cfg = load_selfie_config(tmp_path / "mind.toml")
    assert (cfg.detailer, cfg.detailer_hands) == (True, True)
    (tmp_path / "mind.toml").write_text("[selfies]\ndetailer_hands = false\n", encoding="utf-8")
    cfg = load_selfie_config(tmp_path / "mind.toml")
    assert (cfg.detailer, cfg.detailer_hands) == (True, False)
    (tmp_path / "mind.toml").write_text("[selfies]\ndetailer = false\n", encoding="utf-8")
    assert detail_for(load_selfie_config(tmp_path / "mind.toml")) is None
    (tmp_path / "mind.toml").write_text("[selfies]\ndetailer = true\ndetailer_hands = true\n",
                                        encoding="utf-8")
    cfg = load_selfie_config(tmp_path / "mind.toml")
    assert (cfg.detailer, cfg.detailer_hands) == (True, True)


@pytest.mark.parametrize("key", ["detailer", "detailer_hands"])
def test_a_bad_value_names_the_key(tmp_path, key):
    (tmp_path / "mind.toml").write_text(f"[selfies]\n{key} = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=f"^selfies\\.{key}:"):
        load_selfie_config(tmp_path / "mind.toml")


def test_a_table_with_an_unknown_key_never_opts_in(tmp_path):
    (tmp_path / "mind.toml").write_text(
        "[selfies]\ndetailer = true\ndetailer_hands = true\ndetailr = true\n", encoding="utf-8")
    cfg = load_selfie_config(tmp_path / "mind.toml")
    assert (cfg.detailer, cfg.detailer_hands) == (False, False)


def test_the_pass_follows_the_switches_and_leaves_a_rear_views_face_alone():
    assert detail_for(selfie_cfg()) is None
    assert detail_for(selfie_cfg(detailer_hands=True)) is None  # hands need the detailer
    assert detail_for(selfie_cfg(detailer=True)) == Detail(True, False)
    assert detail_for(selfie_cfg(detailer=True, detailer_hands=True)) == Detail(True, True)
    assert detail_for(selfie_cfg(detailer=True), rear=True) is None
    assert detail_for(selfie_cfg(detailer=True, detailer_hands=True), rear=True) == Detail(False, True)


# -- the wire -----------------------------------------------------------------------------------

ECHO = '''
import json, sys
sys.stdout.write(json.dumps({"id": "ready", "ok": True, "device": "cuda", "free_gib": 20.0,
                             "versions": {}, "error": ""}) + "\\n")
sys.stdout.flush()
for raw in sys.stdin.buffer:
    req = json.loads(raw)
    if req.get("op") == "quit":
        break
    with open(sys.argv[1], "ab") as f:
        f.write(raw)
    sys.stdout.write(json.dumps({"id": req["id"], "ok": True, "error": "", "flagged": False,
                                 "nsfw": 0.01, "prompt": "p", "tokens": 1, "seconds": 0.01,
                                 "peak_gib": 1.0, "width": 1024, "height": 1024}) + "\\n")
    sys.stdout.flush()
'''


def sent(tmp_path, *requests):
    """The raw lines the worker got for `requests`."""
    script, lines = tmp_path / "echo_worker.py", tmp_path / "lines"
    script.write_text(ECHO, encoding="utf-8")

    async def go():
        w = SelfieWorker([sys.executable, "-I", str(script), str(lines)])
        await w.start()
        try:
            return [await w.render(r, rid=f"r{i}") for i, r in enumerate(requests)]
        finally:
            await w.stop()
    return run(go()), lines.read_bytes().splitlines(keepends=True)


def test_without_the_detailer_the_request_line_is_byte_for_byte_the_old_one(tmp_path):
    req = request(tmp_path)
    got, lines = sent(tmp_path, req, replace(req, detail=Detail(False, False)))
    old = json.dumps({
        "id": "r0", "op": "render", "phrases": [[t, p] for t, p in req.phrases],
        "negative": [[t, p] for t, p in req.negative], "seed": req.seed, "steps": req.steps,
        "cfg": req.cfg, "size": req.size, "out": str(req.out), "thumb": str(req.thumb),
        "thumb_size": req.thumb_size}).encode("utf-8") + b"\n"
    assert lines[0] == old
    assert lines[1] == old.replace(b'"id": "r0"', b'"id": "r1"')  # nothing asked: nothing sent
    assert got[0] == Rendered(False, 0.01, "p", 1, 0.01, 1.0, 1024, 1024)
    assert (got[0].detail_faces, got[0].detail_hands, got[0].detail_ms, got[0].detail_code) == (
        0, 0, 0, "")


def test_the_detailer_adds_only_its_true_keys(tmp_path):
    req = request(tmp_path)
    _, lines = sent(tmp_path, replace(req, detail=Detail(True, False)),
                    replace(req, detail=Detail(False, True)),
                    replace(req, detail=Detail(True, True)))
    plain = set(json.loads(sent(tmp_path, req)[1][-1]))
    bodies = [json.loads(x) for x in lines]
    assert [set(b) - plain for b in bodies] == [{"detail_faces"}, {"detail_hands"},
                                                {"detail_faces", "detail_hands"}]
    assert all(b[k] is True for b in bodies for k in b if k.startswith("detail_"))


def test_the_answers_counts_come_back_and_are_logged_without_a_prompt(tmp_path, caplog):
    cmd, env = fake_command(tmp_path)

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        try:
            return (await w.render(replace(request(tmp_path), detail=Detail(True, True)), rid="a"),
                    await w.render(request(tmp_path), rid="b"))
        finally:
            await w.stop()
    with caplog.at_level(logging.INFO):
        with_pass, without = run(go())
    assert (with_pass.detail_faces, with_pass.detail_hands, with_pass.detail_ms,
            with_pass.detail_code) == (1, 2, 7, "")
    assert (without.detail_faces, without.detail_code) == (0, "")
    lines = [r.getMessage() for r in caplog.records if "selfies_detail" in r.getMessage()]
    assert len(lines) == 1
    assert all(x in lines[0] for x in ("rid=a", "faces=1", "hands=2", "ms=7", "code=ok"))
    assert "woman" not in lines[0]


def test_a_worker_that_answers_no_detail_fields_is_unsupported(tmp_path):
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_NO_DETAIL="1")

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        try:
            return await w.render(replace(request(tmp_path), detail=Detail(True, False)))
        finally:
            await w.stop()
    got = run(go())
    assert got.flagged is False and (got.detail_faces, got.detail_code) == (0, "unsupported")


# -- profile sets -------------------------------------------------------------------------------

SPEC = PromptSpec((("photo of a woman in her thirties", 0),), (("blurry", 1),))


def a_set(tmp_path, cfg, **kw):
    log = tmp_path / "log"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_LOG=str(log))

    async def room():
        pass

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        try:
            await render_candidates(w, SPEC, tmp_path / "set", night="2026-11-17", guid=101,
                                    set_no=1, cfg=cfg, make_room=room, **kw)
        finally:
            await w.stop()
    (tmp_path / "set").mkdir(exist_ok=True)
    run(go())
    return [line.split(" detail ")[1] if " detail " in line else ""
            for line in log.read_text(encoding="utf-8").splitlines()]


def test_a_set_renders_without_the_pass_by_default(tmp_path):
    assert a_set(tmp_path, selfie_cfg()) == ["", "", "", ""]


def test_a_set_asks_for_the_faces_and_with_the_switch_the_hands(tmp_path):
    assert a_set(tmp_path, selfie_cfg(detailer=True)) == ["faces"] * 4
    (tmp_path / "log").unlink()
    assert a_set(tmp_path, selfie_cfg(detailer=True, detailer_hands=True)) == ["faces hands"] * 4


def test_a_slot_seen_from_behind_keeps_its_face_as_rendered(tmp_path):
    rear = (False, True, False, True)
    assert a_set(tmp_path, selfie_cfg(detailer=True), rear=rear) == ["faces", "", "faces", ""]
    (tmp_path / "log").unlink()
    assert a_set(tmp_path, selfie_cfg(detailer=True, detailer_hands=True), rear=rear) == [
        "faces hands", "hands", "faces hands", "hands"]
    with pytest.raises(ValueError):
        a_set(tmp_path, selfie_cfg(detailer=True), rear=(True,))


# -- request photos -----------------------------------------------------------------------------

def test_a_request_photo_asks_for_the_pass_only_with_the_switch(app, tmp_path):  # noqa: F811
    room = Room()
    worker = Worker(room)
    assert render(app, plan(app), room, worker).outcome == "ready"
    assert worker.calls[0].detail is None


def test_a_request_photo_with_the_switch_asks_for_the_faces(app, tmp_path):  # noqa: F811
    app.selfies.cfg = replace(app.selfies.cfg, detailer=True, detailer_hands=True)
    room = Room()
    worker = Worker(room)
    assert render(app, plan(app), room, worker).outcome == "ready"
    assert worker.calls[0].detail == Detail(True, True)


# -- selfies try --------------------------------------------------------------------------------

def test_try_detailer_turns_the_pass_on_whatever_mind_toml_says(root, capsys, tmp_path, monkeypatch):  # noqa: F811
    log = tmp_path / "log"
    env = fake_command(tmp_path, FAKE_SELFIE_LOG=str(log))[1]
    monkeypatch.setattr(cli, "worker_env", lambda cfg: env)
    assert cli.selfies_main(["--data-dir", str(root), "try", "--guid", "101"],
                            make_llm=lambda cfg: llm(root)) == 0
    assert " detail " not in log.read_text(encoding="utf-8")
    log.unlink()
    assert cli.selfies_main(["--data-dir", str(root), "try", "--guid", "101", "--detailer"],
                            make_llm=lambda cfg: llm(root)) == 0
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and all(x.endswith(" detail faces") for x in lines)
    log.unlink()
    assert cli.selfies_main(["--data-dir", str(root), "try", "--guid", "101", "--detailer-hands"],
                            make_llm=lambda cfg: llm(root)) == 0
    assert all(x.endswith(" detail faces hands")
               for x in log.read_text(encoding="utf-8").splitlines())
    capsys.readouterr()


def test_try_snaps_detailer_details_the_photos_and_never_a_guild_tile(snaproot, capsys, tmp_path):  # noqa: F811
    code = cli.selfies_main(["--data-dir", str(snaproot), "try", "--snaps", "--guild", "--detailer"],
                            make_llm=lambda cfg: snap_llm(snaproot))
    assert code == 0 and json.loads(capsys.readouterr().out)["rendered"] == 2
    renders = [x for x in (tmp_path / "log").read_text(encoding="utf-8").splitlines()
               if " grid " not in x]
    photos = [x for x in renders if x.startswith("t")]
    tiles = [x for x in renders if x.startswith("g")]
    assert len(photos) == 2 and all(" detail faces" in x for x in photos)
    assert tiles and not any(" detail " in x for x in tiles)


# -- the night ----------------------------------------------------------------------------------

def _night_lines(h, tmp_path, cfg):
    from selfies.test_varied_sets import make_stage_for
    log = tmp_path / "requests.log"
    run(make_stage_for(cfg, tmp_path, FAKE_SELFIE_LOG=str(log))(h.ctx()))
    return [x for x in log.read_text(encoding="utf-8").splitlines() if x.startswith("101:1:")]


def test_the_nights_varied_set_details_every_face_but_a_back_views(h, tmp_path):
    from leeroy_mind.selfies import kind, varied
    from selfies.test_varied_sets import DAY, ON, lust
    lust(h, 101)
    varied.queue(h.store, human=1, now=h.T, day=DAY, go=True)
    looks = varied.plan_of(h.store.conn, 101, 1)
    assert 0 < sum(x.view in kind.BACK_VIEWS for x in looks) < 4  # some from behind, some not
    lines = _night_lines(h, tmp_path, replace(ON, detailer=True))
    assert len(lines) == 4
    for line, look in zip(lines, looks):
        assert line.endswith(" detail faces") == (look.view not in kind.BACK_VIEWS)
        assert (" detail " in line) == (look.view not in kind.BACK_VIEWS)


def test_the_nights_sets_carry_no_detail_key_with_the_switch_off(h, tmp_path):
    from leeroy_mind.selfies import varied
    from selfies.test_varied_sets import DAY, ON, lust
    lust(h, 101)
    varied.queue(h.store, human=1, now=h.T, day=DAY, go=True)
    lines = _night_lines(h, tmp_path, ON)
    assert len(lines) == 4 and not any(" detail" in x for x in lines)
