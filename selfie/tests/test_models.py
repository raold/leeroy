"""The pinned models: check() reads what is there, fetch_models() validates before fetching.
No test reaches the network or loads a model."""
import hashlib
import re

import pytest

from leeroy_selfie import models
from leeroy_selfie.models import MODEL_PINS, ModelError, check, fetch_models, model_dir, verify


def blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def put(data_dir, name, revision=None, body=b"x"):
    """Fake files of one pin with download records naming `revision` (default: the pinned one)."""
    pin = MODEL_PINS[name]
    d = model_dir(data_dir, name)
    for f in pin.files:
        (d / f).parent.mkdir(parents=True, exist_ok=True)
        (d / f).write_bytes(body)
        rec = d / ".cache" / "huggingface" / "download" / f"{f}.metadata"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(f"{revision or pin.revision}\n{blob(body)}\n1.0\n", encoding="utf-8")


def test_each_pin_names_a_full_commit_a_licence_and_its_files():
    for pin in MODEL_PINS.values():
        assert re.fullmatch(r"[0-9a-f]{40}", pin.revision) and pin.licence and pin.files
    assert MODEL_PINS["realvisxl"].revision == "ac93e0dda1f6d448cae19bbfab8c5e720a5e48bc"
    assert all(".fp16." in f for f in MODEL_PINS["realvisxl"].files if f.endswith(".safetensors"))


def test_the_pins_name_the_files_the_loaders_read():
    assert set(MODEL_PINS) == {"realvisxl", "nsfw"}
    assert MODEL_PINS["nsfw"].files == ("config.json", "model.safetensors",
                                        "preprocessor_config.json")
    assert len(MODEL_PINS["realvisxl"].files) == 19


def test_a_model_not_fetched_is_a_note(tmp_path):
    assert check(tmp_path, "nsfw") == [(None, "nsfw: not fetched (fetch-models --only nsfw)")]
    assert verify(tmp_path) == []


def test_a_fetched_model_is_ok_and_a_missing_file_is_a_problem(tmp_path):
    put(tmp_path, "nsfw")
    assert [ok for ok, _ in check(tmp_path, "nsfw")] == [True]
    (model_dir(tmp_path, "nsfw") / "config.json").unlink()
    assert check(tmp_path, "nsfw")[0][0] is False
    assert "config.json missing" in verify(tmp_path)[0]


def test_check_reports_a_file_from_another_revision(tmp_path):
    put(tmp_path, "nsfw", revision="0" * 40)
    lines = check(tmp_path, "nsfw")
    assert lines and all(ok is False and "is from revision" in text for ok, text in lines)


def test_check_reports_a_changed_file(tmp_path):
    put(tmp_path, "nsfw")
    (model_dir(tmp_path, "nsfw") / "model.safetensors").write_bytes(b"changed")
    assert any("does not match its download record" in t for ok, t in check(tmp_path, "nsfw"))


def test_fetch_refuses_an_unknown_name_before_fetching(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("fetched")
    monkeypatch.setattr(models, "_snapshot_download", boom)
    with pytest.raises(ModelError, match="unknown model"):
        fetch_models(tmp_path, {"nsfw", "nope"})


def test_fetch_downloads_the_pinned_files_only_and_checks_them(tmp_path, monkeypatch):
    calls = []

    def fake(repo, **kw):
        calls.append((repo, kw))
        put(tmp_path, "nsfw")
    monkeypatch.setattr(models, "_snapshot_download", fake)
    said = []
    lines = fetch_models(tmp_path, ["nsfw"], say=said.append)
    (repo, kw), = calls
    pin = MODEL_PINS["nsfw"]
    assert repo == pin.repo and kw["revision"] == pin.revision and kw["token"] is False
    assert kw["allow_patterns"] == list(pin.files)
    assert lines[0][0] is True and said


def test_fetch_raises_when_what_arrived_is_wrong(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "_snapshot_download", lambda repo, **kw: None)
    with pytest.raises(ModelError, match="not fetched"):
        fetch_models(tmp_path, ["nsfw"])
    put(tmp_path, "nsfw")
    (model_dir(tmp_path, "nsfw") / "config.json").unlink()
    with pytest.raises(ModelError, match="config.json missing"):
        fetch_models(tmp_path, ["nsfw"])


def test_fetch_turns_a_download_failure_into_one_model_error(tmp_path, monkeypatch):
    def fail(repo, **kw):
        raise OSError("offline")
    monkeypatch.setattr(models, "_snapshot_download", fail)
    with pytest.raises(ModelError, match="OSError: offline"):
        fetch_models(tmp_path, ["nsfw"])


@pytest.mark.engines
def test_the_fetched_models_are_the_pinned_ones(fetched_data_dir):
    lines = check(fetched_data_dir, "realvisxl")
    if any(ok is None for ok, _ in lines):
        pytest.skip(f"RealVisXL is not fetched under {fetched_data_dir}")
    assert all(ok for ok, _ in lines), lines
