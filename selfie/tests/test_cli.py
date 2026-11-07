import json
import sys

import pytest

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


class SmokeRenderer:
    def __init__(self, flagged=False):
        self.flagged, self.seen = flagged, []

    def render(self, req):
        self.seen.append(req)
        return {"flagged": self.flagged, "nsfw": 0.01, "prompt": "p", "dropped": [],
                "tokens": 9, "seconds": 1.5, "peak_gib": 12.2, "width": 832, "height": 1216}


def test_smoke_identity_renders_the_invented_person_as_a_portrait_with_the_reference(
        tmp_path, monkeypatch, capsys):
    from leeroy_selfie import render
    from leeroy_selfie.__main__ import SMOKE_PHRASES
    loads, renderer = [], SmokeRenderer()
    monkeypatch.setattr(render, "load", lambda data, *, min_free_gib, identity=False:
                        loads.append(identity) or renderer)
    ref = tmp_path / "ref.jpg"
    ref.write_bytes(b"jpg")
    assert main(["smoke", "--out", str(tmp_path / "o"), "--identity", "--ref", str(ref)]) == 0
    (req,) = renderer.seen
    assert loads == [True] and req.shape == "portrait" and req.size == 1024
    assert req.identity.ref == str(ref.resolve()) and req.identity.crop == (0.0, 0.0, 1.0, 1.0)
    assert req.identity.scale == 0.6 and [list(p) for p in req.phrases] == SMOKE_PHRASES
    assert set(json.loads(capsys.readouterr().out)) == {"load_s", "seconds", "peak_gib", "nsfw",
                                                        "tokens"}


def test_plain_smoke_stays_square_without_a_reference(tmp_path, monkeypatch, capsys):
    from leeroy_selfie import render
    loads, renderer = [], SmokeRenderer()
    monkeypatch.setattr(render, "load", lambda data, *, min_free_gib, identity=False:
                        loads.append(identity) or renderer)
    assert main(["smoke", "--out", str(tmp_path / "o")]) == 0
    (req,) = renderer.seen
    assert loads == [False] and req.shape == "square" and req.identity is None


def test_smoke_identity_and_ref_go_together(tmp_path, capsys):
    for argv in (["--identity"], ["--ref", str(tmp_path / "ref.jpg")]):
        with pytest.raises(SystemExit) as e:
            main(["smoke", "--out", str(tmp_path / "o"), *argv])
        assert e.value.code == 2
    assert "--identity and --ref go together" in capsys.readouterr().err


def test_smoke_says_a_render_failure_in_one_line(tmp_path, monkeypatch, capsys):
    from leeroy_selfie import render
    from leeroy_selfie.protocol import RenderFailed

    class Missing:
        def render(self, req):
            raise RenderFailed("missing_ref")
    monkeypatch.setattr(render, "load", lambda data, *, min_free_gib, identity=False: Missing())
    assert main(["smoke", "--out", str(tmp_path / "o"), "--identity", "--ref",
                 str(tmp_path / "gone.jpg")]) == 1
    assert capsys.readouterr().err == "smoke: missing_ref\n"


def test_fetch_models_prints_one_line_on_failure(monkeypatch, capsys):
    monkeypatch.setattr(models, "_snapshot_download", lambda repo, **kw: None)
    assert main(["fetch-models", "--only", "nsfw"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("fetch-models: ") and err.count("\n") == 1
    assert main(["fetch-models", "--only", "bogus"]) == 1
