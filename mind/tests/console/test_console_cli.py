"""`python -m leeroy_mind console`: its arguments, the data dir it refuses, and what it builds.
Nothing is launched: the scratch checkout has no voice venv and control is off or fake."""
import pytest

from conftest import seed_test_cast
from leeroy_mind import __main__ as mind_main
from leeroy_mind.console import DEFAULT_PORT, cli


def args(*argv):
    return cli.parser().parse_args(list(argv))


def test_defaults():
    a = args()
    assert (a.port, a.no_control, a.voice_ui_port, a.open, a.root) == (
        DEFAULT_PORT, False, None, False, None)
    assert DEFAULT_PORT == 7881


def test_an_empty_data_dir_is_refused(tmp_path, capsys):
    assert cli.main(["--data-dir", str(tmp_path / "nothing"), "--no-control"]) == 1
    err = capsys.readouterr().err
    assert "no mind.toml" in err and "--data-dir" in err


def test_the_mind_cli_hands_console_over(tmp_path, capsys):
    assert mind_main.main(["console", "--data-dir", str(tmp_path / "nothing")]) == 1
    assert "console: no mind.toml" in capsys.readouterr().err


@pytest.fixture
def data(store, tmp_path):
    seed_test_cast(store)
    (tmp_path / "mind.toml").write_text('[bridge]\nport = 17878\n[voice]\nenabled = true\n',
                                        encoding="utf-8")
    return tmp_path


def test_build_reads_mind_toml_and_turns_control_off(data, tmp_path):
    app, problem = cli.build(args("--data-dir", str(data), "--no-control", "--root",
                                  str(tmp_path / "checkout"), "--voice-ui-port", "17880"),
                             platform="linux")
    assert problem is None
    assert (app.s.bridge_port, app.s.voice_enabled, app.s.voice_ui_port) == (17878, True, 17880)
    assert app.s.port == DEFAULT_PORT and len(app.s.key) >= 32
    control = app.controls.describe()
    assert control["enabled"] is False and "--no-control" in control["reason"]


def test_build_says_why_control_is_unavailable(data, tmp_path):
    app, _ = cli.build(args("--data-dir", str(data), "--root", str(tmp_path / "checkout")),
                       platform="linux")
    services = app.controls.describe()["services"]
    assert services == {"mind": "process control is supported only on Windows",
                        "voice": "process control is supported only on Windows"}


def test_each_console_gets_its_own_key(data, tmp_path):
    a = args("--data-dir", str(data), "--no-control", "--root", str(tmp_path / "c"))
    one, _ = cli.build(a, platform="linux")
    two, _ = cli.build(a, platform="linux")
    assert one.s.key != two.s.key


def test_a_bad_mind_toml_leaves_defaults_and_says_so(data, tmp_path):
    (data / "mind.toml").write_text("[bridge]\nport = 'x'\n", encoding="utf-8")
    app, _ = cli.build(args("--data-dir", str(data), "--no-control", "--root",
                            str(tmp_path / "c")), platform="linux")
    assert app.s.config_problem.startswith("mind.toml:") and app.s.bridge_port == 7878


def test_bad_ports_are_refused(data):
    app, problem = cli.build(args("--data-dir", str(data), "--port", "70000"), platform="linux")
    assert app is None and "1-65535" in problem
