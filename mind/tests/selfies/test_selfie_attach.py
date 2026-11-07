"""P2 Task 12: the attach that wires profile photos into the mind. [selfies] is read once at the
start; both night stages only with enabled = true ("photo" after "enrich" on the reflection
model, "selfies" after "regress" as an image stage); the pick service always; and the photo
files repaired once, in MindApp.__init__ before run() starts any loop or night, and from nowhere
else. Every person here is invented."""
import logging
from pathlib import Path

import pytest

import leeroy_mind
from conftest import run
from leeroy_mind.selfies.config import SelfieCfg
from leeroy_mind.selfies.files import candidate, chosen, history, staging_dir
from leeroy_mind.selfies.service import SelfieService
from leeroy_mind.selfies.stage import wants_gpu as stage_wants_gpu
from leeroy_mind.selfies.store import pick

from .fakes import JPEG, mind_app, ready, tagged_jpeg

ON = "[selfies]\nenabled = true\n"


def _events(caplog, event):
    return [r.leeroy_fields for r in caplog.records if getattr(r, "leeroy_event", None) == event]


@pytest.mark.parametrize("enabled", [True, False])
def test_attach_adds_both_stages_only_when_enabled(tmp_path, enabled):
    app = mind_app(tmp_path, toml=f"[selfies]\nenabled = {str(enabled).lower()}\n")   # FakeLLM, no bridge
    stages = {s.name: s for s in app.night_runner.stages}
    names = [s.name for s in app.night_runner.stages]
    if enabled:
        assert names[names.index("enrich") + 1] == "photo" and stages["photo"].model == "reflection"
        assert names[names.index("regress") + 1] == "selfies" and stages["selfies"].model == "image"
        assert stages["selfies"].wants_gpu is stage_wants_gpu   # asked before make_room
        assert stages["photo"].wants_gpu is None
    else:
        assert "photo" not in stages and "selfies" not in stages
    assert app.selfies is not None
    app.store.close()


@pytest.mark.parametrize("toml,cfg,said", [(ON, SelfieCfg(enabled=True), "selfies_on"),
                                            ("", SelfieCfg(), "selfies_off")], ids=["on", "off"])
def test_the_attach_repairs_then_logs_whether_photos_render(tmp_path, caplog, toml, cfg, said):
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        app = mind_app(tmp_path, toml=toml)
    try:
        assert app.selfies.cfg == cfg
        events = [r.leeroy_event for r in caplog.records
                  if getattr(r, "leeroy_event", "").startswith("selfies_")]
        assert events == ["selfies_synced", said, "selfies_snaps_off"]
    finally:
        app.store.close()


def test_an_invalid_selfies_table_is_logged_and_treated_as_off(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        app = mind_app(tmp_path, toml='[selfies]\nenabled = "yes"\n')
    try:
        assert app.selfies is not None and app.selfies.cfg == SelfieCfg()
        names = [s.name for s in app.night_runner.stages]
        assert "photo" not in names and "selfies" not in names
        invalid = [r for r in caplog.records
                   if getattr(r, "leeroy_event", None) == "selfies_config_invalid"]
        assert len(invalid) == 1 and invalid[0].levelno == logging.WARNING
        assert "selfies.enabled" in invalid[0].leeroy_fields["reason"]
        assert _events(caplog, "selfies_off") == [{}] and not _events(caplog, "selfies_on")
        st = run(app.selfies.handle({"action": "status"}))
        assert st["ok"] is True and st["enabled"] is False
    finally:
        app.store.close()


def test_attach_repairs_the_files_at_start(tmp_path, caplog):
    made = {}

    def crashed(store):  # what a crash left: a committed pick without files, a stray chosen.jpg,
        ready(store, tmp_path, 101, 1)  # a render cut short and a half-written copy
        made["pick"] = pick(store, 101, 1, 2, source="console", now=2.0)
        ready(store, tmp_path, 102, 1)
        chosen(tmp_path, 102).write_bytes(tagged_jpeg("stray"))
        ready(store, tmp_path, 103, 1)
        part = staging_dir(tmp_path, 103, 2)
        part.mkdir(parents=True)
        (part / "c0.jpg").write_bytes(JPEG)
        (chosen(tmp_path, 103).parent / ".chosen.jpg.4242.tmp").write_bytes(b"half")

    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        app = mind_app(tmp_path, toml=ON, prepare=crashed)
    try:
        n = made["pick"]
        photo = candidate(tmp_path, 101, 1, 2).read_bytes()
        assert chosen(tmp_path, 101).read_bytes() == photo == history(tmp_path, 101, n).read_bytes()
        assert not chosen(tmp_path, 102).exists()
        assert not staging_dir(tmp_path, 103, 2).exists()
        assert not (chosen(tmp_path, 103).parent / ".chosen.jpg.4242.tmp").exists()
        assert all(candidate(tmp_path, 103, 1, i).exists() for i in range(4))
        [synced] = _events(caplog, "selfies_synced")
        assert {k: synced[k] for k in ("members", "changed", "failed", "swept")} == {
            "members": 2, "changed": 2, "failed": 0, "swept": 2}
    finally:
        app.store.close()


def test_a_repair_that_fails_never_stops_the_mind_starting(tmp_path, monkeypatch, caplog):
    def broken(self):
        raise RuntimeError("no such table, say")
    monkeypatch.setattr(SelfieService, "sync_all", broken)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        app = mind_app(tmp_path, toml=ON)
    try:
        assert app.selfies is not None and "selfies" in [s.name for s in app.night_runner.stages]
        failed = [r for r in caplog.records
                  if getattr(r, "leeroy_event", None) == "selfies_repair_failed"]
        assert [(r.levelno, r.leeroy_fields) for r in failed] == [
            (logging.WARNING, {"error": "RuntimeError"})]
        assert "no such table" not in caplog.text
        assert _events(caplog, "selfies_on") == [{}]
    finally:
        app.store.close()


def test_sync_all_runs_once_at_attach_before_the_mind_runs_and_from_nowhere_else(tmp_path,
                                                                                monkeypatch):
    seen = []
    real = SelfieService.sync_all

    def spy(self):
        app = self.app
        seen.append((app.running, dict(app._loops), list(app._bg_tasks)))
        return real(self)
    monkeypatch.setattr(SelfieService, "sync_all", spy)

    async def go():
        app = mind_app(tmp_path, toml=ON)
        try:
            assert seen == [(False, {}, [])]  # in __init__: no loop, no background, no night
            ready(app.store, tmp_path, 101, 1)
            n = (await app.selfies.handle({"action": "pick", "guid": 101, "set": 1,
                                           "index": 0}))["pick"]
            await app.selfies.handle({"action": "undo", "pick": n})
            await app.selfies.handle({"action": "more", "guid": 101})
            await app.selfies.handle({"action": "status"})
        finally:
            app.store.close()
    run(go())
    assert len(seen) == 1  # a change syncs its own member only, never everyone's files

    # and no other code calls it: not a hook, the endpoint, a stage or a command
    root = Path(leeroy_mind.__file__).parent
    calls = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8").count("sync_all")
             for p in root.rglob("*.py") if "sync_all" in p.read_text(encoding="utf-8")}
    assert set(calls) == {"selfies/service.py", "selfies/attach.py"}
    attach_src = (root / "selfies" / "attach.py").read_text(encoding="utf-8")
    assert attach_src.count(".sync_all()") == 1
    service_src = (root / "selfies" / "service.py").read_text(encoding="utf-8")
    assert ".sync_all(" not in service_src  # defined there, called only by the attach


SNAPS = "[selfies]\nenabled = true\nsnaps = true\n"


def test_attach_adds_scenes_and_snaps_only_with_both_on(tmp_path, caplog):
    from leeroy_mind.selfies.snapstage import wants_gpu as snaps_wants_gpu
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        app = mind_app(tmp_path, toml=SNAPS)
    try:
        stages = {s.name: s for s in app.night_runner.stages}
        names = [s.name for s in app.night_runner.stages]
        assert names[names.index("life") + 1] == "scenes" and stages["scenes"].model == "reflection"
        assert names[names.index("selfies") + 1] == "snaps" and stages["snaps"].model == "image"
        assert stages["snaps"].wants_gpu is snaps_wants_gpu
        assert _events(caplog, "selfies_snaps_on") == [{}]
    finally:
        app.store.close()


@pytest.mark.parametrize("toml,said", [(ON, "selfies_snaps_off"),
                                        ("[selfies]\nsnaps = true\n", "selfies_snaps_need_enabled")],
                         ids=["snaps_off", "need_enabled"])
def test_attach_adds_neither_scenes_nor_snaps_otherwise(tmp_path, caplog, toml, said):
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        app = mind_app(tmp_path, toml=toml)
    try:
        names = [s.name for s in app.night_runner.stages]
        assert "scenes" not in names and "snaps" not in names
        assert _events(caplog, said) == [{}] and not _events(caplog, "selfies_snaps_on")
    finally:
        app.store.close()
