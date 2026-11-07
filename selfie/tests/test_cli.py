import sys

from leeroy_selfie import models, paths
from leeroy_selfie.__main__ import main


def test_where_prints_the_data_dir_from_the_environment(selfie_data, capsys):
    assert main(["where"]) == 0
    assert capsys.readouterr().out.strip() == f"data_dir={selfie_data}"
    assert paths.data_dir() == selfie_data


def test_the_default_data_dir_is_selfie_data_in_the_checkout(monkeypatch):
    monkeypatch.delenv("LEEROY_SELFIE_DATA")
    assert paths.data_dir().name == "selfie-data"
    assert (paths.data_dir().parent / "selfie" / "leeroy_selfie").is_dir()


def test_quick_doctor_never_imports_torch(capsys):
    assert main(["doctor", "--quick"]) == 0
    assert "torch" not in sys.modules
    out = capsys.readouterr().out
    assert "note realvisxl: not fetched" in out and "note nsfw: not fetched" in out


def test_quick_doctor_notes_an_identity_adapter_not_fetched(capsys):
    assert main(["doctor", "--quick"]) == 0
    assert "note ipadapter: not fetched" in capsys.readouterr().out


def test_doctor_exits_1_on_a_failed_check(selfie_data, capsys):
    d = models.model_dir(selfie_data, "nsfw")
    d.mkdir(parents=True)
    (d / "config.json").write_text("{}")
    assert main(["doctor", "--quick"]) == 1
    assert "FAIL nsfw:" in capsys.readouterr().out


def test_doctor_takes_a_data_dir(tmp_path, capsys):
    assert main(["doctor", "--quick", "--data", str(tmp_path / "other")]) == 0
    assert "other" in capsys.readouterr().out


def test_fetch_models_prints_one_line_on_failure(monkeypatch, capsys):
    monkeypatch.setattr(models, "_snapshot_download", lambda repo, **kw: None)
    assert main(["fetch-models", "--only", "nsfw"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("fetch-models: ") and err.count("\n") == 1
    assert main(["fetch-models", "--only", "bogus"]) == 1
