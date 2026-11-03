from conftest import run
from leeroy_mind.__main__ import main
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.realm import cli, protocol
from leeroy_mind.testing.fake_llm import FakeLLM


def test_main_routes_realm_before_application_start(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "realm_main", lambda argv: seen.append(argv) or 7)
    assert main(["realm", "status", "--data-dir", "invented"]) == 7
    assert seen == [["status", "--data-dir", "invented"]]


def test_main_help_lists_realm(capsys):
    import pytest
    with pytest.raises(SystemExit) as exited:
        main(["--help"])
    assert exited.value.code == 0 and "realm" in capsys.readouterr().out


def test_root_loader_accepts_realm_extension_without_warning(tmp_path, caplog):
    path = tmp_path / "mind.toml"
    path.write_text("[realm]\nenabled = false\n")
    load_config(path)
    assert not any("unknown" in row.message and "realm" in row.message for row in caplog.records)


def test_real_mindapp_always_attaches_realm_last_and_default_off(tmp_path, clock):
    app = MindApp(load_config(tmp_path / "absent.toml"), tmp_path, llm=FakeLLM(), clock=clock)
    try:
        assert app.realm is not None and not app.realm.enabled
        assert app._background[-1] == app.realm.run
        assert app._shutdown_drains[-1] == app.realm.close
        assert app.skip_event_rows >= protocol.NOISY
        assert not hasattr(app, "bodies")
        assert app._before_handlers["hello"][-1] == app.realm.on_presence
        assert app._before_handlers["login"][-1] == app.realm.on_presence
        assert app._shutdown_receipts["realm_job"][-1] == app.realm.on_job
    finally:
        run(app.realm.close())
        app.store.close()
