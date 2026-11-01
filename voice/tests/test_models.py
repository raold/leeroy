"""The pinned models (the plan's Global Constraints table): fetch_models copies Kokoro from the
listening kit or downloads it from its release, fetches the HuggingFace models at their pinned
revisions, and check() and verify() read what is there. No test reaches the network: every
download here is a fake, and the `no_network` fixture fails the test on a real one."""
import dataclasses
import fnmatch
import hashlib
import http.client
import io
import os
import time
import urllib.error
from pathlib import Path

import pytest

from leeroy_voice import models
from leeroy_voice.models import MODEL_PINS, ModelError, check, fetch_models, model_dir, verify

ONNX, VOICES = b"a small stand-in for the model", b"a small stand-in for the voices"
KOKORO_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob(data: bytes) -> str:
    """git's blob id, the etag huggingface_hub records for a file kept out of LFS."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def quiet(_line: str) -> None:
    pass


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kw):
        raise AssertionError("a test reached the network")
    monkeypatch.setattr(models, "_urlopen", refuse)
    monkeypatch.setattr(models, "_snapshot_download", refuse)
    monkeypatch.setenv("HF_HOME", "")                       # restored after the test
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "")


@pytest.fixture
def small_kokoro(monkeypatch, tmp_path, no_network) -> Path:
    """The kokoro pin over two small files, and a listening kit (in the second of two repos)
    that holds them; returns the kit's folder."""
    pin = MODEL_PINS["kokoro"]
    monkeypatch.setitem(MODEL_PINS, "kokoro", dataclasses.replace(
        pin, sha256={"kokoro-v1.0.onnx": sha(ONNX), "voices-v1.0.bin": sha(VOICES)}))
    repo = tmp_path / "main-checkout"
    lab = repo / pin.lab
    lab.mkdir(parents=True)
    (lab / "kokoro-v1.0.onnx").write_bytes(ONNX)
    (lab / "voices-v1.0.bin").write_bytes(VOICES)
    monkeypatch.setattr(models, "lab_dirs", lambda: [tmp_path / "worktree", repo])
    return lab


def fake_snapshot(calls: list, *, revision: str | None = None):
    """snapshot_download as huggingface_hub does it into a local_dir: each allowed file, and its
    download record (.cache/huggingface/download/<file>.metadata: commit, etag, time). As in
    the real records, an .onnx file (in LFS) has its sha256 as the etag, the small ones their
    git blob id."""
    def snapshot(repo_id, **kw):
        calls.append((repo_id, kw))
        local = Path(kw["local_dir"])
        for name in kw["allow_patterns"]:
            data = f"{repo_id}/{name}".encode()
            (local / name).parent.mkdir(parents=True, exist_ok=True)
            (local / name).write_bytes(data)
            etag = sha(data) if name.endswith(".onnx") else blob(data)
            meta = local / ".cache" / "huggingface" / "download" / f"{name}.metadata"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(f"{revision or kw['revision']}\n{etag}\n{time.time()}\n",
                            encoding="utf-8")
        return str(local)
    return snapshot


def test_pins():                                     # the plan's Global Constraints table
    assert list(MODEL_PINS) == ["kokoro", "parakeet", "silero", "whisper", "ecapa",
                                "chatterbox-turbo"]
    k = MODEL_PINS["kokoro"]
    assert (k.files, k.repo, k.licence) == (KOKORO_FILES, "", "Apache-2.0")
    assert k.sha256 == {
        "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
        "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"}
    assert k.url == "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
    assert k.lab == "private/voice-lab/models/kokoro"
    assert {n: (p.repo, p.revision, p.licence) for n, p in MODEL_PINS.items() if p.repo} == {
        "parakeet": ("istupakov/parakeet-tdt-0.6b-v2-onnx",
                     "0bbb45a3365852604aef28b538a8f066f4ccaa85", "CC-BY-4.0"),
        "silero": ("istupakov/silero-vad-onnx", "b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6", "MIT"),
        "whisper": ("dropbox-dash/faster-whisper-large-v3-turbo",
                    "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf", "MIT"),
        "ecapa": ("speechbrain/spkrec-ecapa-voxceleb", "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286",
                  "Apache-2.0")
        "chatterbox-turbo": ("ResembleAI/chatterbox-turbo",
                             "749d1c1a46eb10492095d68fbcf55691ccf137cd", "MIT"),
    }
    assert MODEL_PINS["parakeet"].files == ("encoder-model.int8.onnx",
                                            "decoder_joint-model.int8.onnx", "nemo128.onnx",
                                            "vocab.txt", "config.json")
    assert MODEL_PINS["silero"].files == ("config.json", "silero_vad.onnx")


def test_onnx_asr_finds_the_pinned_files():
    """I4: the names onnx-asr 0.12.0 looks for in a local dir are among the files fetched."""
    nemo = pytest.importorskip("onnx_asr.models.nemo")
    silero = pytest.importorskip("onnx_asr.models.silero")
    wanted = {"parakeet": nemo.NemoConformerTdt._get_model_files("int8"),
              "silero": silero.SileroVad._get_model_files(None)}
    for name, files in wanted.items():
        for pattern in files.values():
            assert fnmatch.filter(MODEL_PINS[name].files, pattern), (name, pattern)


def test_model_dir(tmp_path):
    assert model_dir(tmp_path, "parakeet") == tmp_path / "models" / "parakeet"
    with pytest.raises(ModelError, match="orpheus"):
        model_dir(tmp_path, "orpheus")


def test_nothing_fetched_is_a_note(tmp_path):
    d = tmp_path / "voice-data"
    assert verify(d) == []
    for name in MODEL_PINS:
        assert check(d, name) == [(None, f"{name}: not fetched (fetch-models --only {name})")]
    assert not d.exists()                            # reading only


def test_kokoro_copied_from_the_kit(tmp_path, small_kokoro):
    d = tmp_path / "voice-data"
    said: list[str] = []
    fetch_models(d, {"kokoro"}, say=said.append)
    k = model_dir(d, "kokoro")
    assert [(k / f).read_bytes() for f in KOKORO_FILES] == [ONNX, VOICES]
    assert sorted(p.name for p in k.iterdir()) == list(KOKORO_FILES)      # no part file left
    assert all((small_kokoro / f).exists() for f in KOKORO_FILES)         # copied, never moved
    assert sum("copied from" in line for line in said) == 2
    assert verify(d) == []
    assert check(d, "kokoro") == [(True, f"kokoro: kokoro-v1.0.onnx sha256 {sha(ONNX)} as pinned"),
                                  (True, f"kokoro: voices-v1.0.bin sha256 {sha(VOICES)} as pinned")]
    said.clear()
    fetch_models(d, {"kokoro"}, say=said.append)                         # already there
    assert not any("copied from" in line for line in said)
    (k / "voices-v1.0.bin").write_bytes(b"damaged")
    said.clear()
    fetch_models(d, {"kokoro"}, say=said.append)                         # a damaged one again
    assert [line for line in said if "copied from" in line] == [
        f"kokoro: voices-v1.0.bin copied from {small_kokoro / 'voices-v1.0.bin'}"]
    assert verify(d) == []


def test_a_bad_copy_refused(tmp_path, small_kokoro):
    (small_kokoro / "voices-v1.0.bin").write_bytes(b"not the pinned bytes")
    with pytest.raises(ModelError, match="voices-v1.0.bin"):
        fetch_models(tmp_path / "voice-data", {"kokoro"}, say=quiet)
    k = model_dir(tmp_path / "voice-data", "kokoro")
    assert not (k / "voices-v1.0.bin").exists() and not list(k.glob("*.part"))


def test_kokoro_downloaded_without_the_kit(tmp_path, small_kokoro, monkeypatch):
    for f in KOKORO_FILES:
        (small_kokoro / f).unlink()
    url = MODEL_PINS["kokoro"].url
    served = {f"{url}/kokoro-v1.0.onnx": ONNX, f"{url}/voices-v1.0.bin": VOICES}
    asked = []

    def urlopen(address, *, timeout):
        asked.append(address)
        return io.BytesIO(served[address])
    monkeypatch.setattr(models, "_urlopen", urlopen)
    fetch_models(tmp_path / "voice-data", {"kokoro"}, say=quiet)
    assert asked == list(served)
    assert verify(tmp_path / "voice-data") == []


class CutOff(io.BytesIO):
    def read(self, *args):
        raise http.client.IncompleteRead(b"half a model")


def test_a_failed_download_is_one_line(tmp_path, small_kokoro, monkeypatch):
    """A network failure is a ModelError of one line naming the file; no part file is left."""
    for f in KOKORO_FILES:
        (small_kokoro / f).unlink()
    d = tmp_path / "voice-data"

    def unreachable(address, *, timeout):
        raise urllib.error.URLError("[Errno 11001] getaddrinfo failed")
    for urlopen, error in ((unreachable, "URLError"),
                           (lambda address, *, timeout: CutOff(), "IncompleteRead")):
        monkeypatch.setattr(models, "_urlopen", urlopen)
        with pytest.raises(ModelError, match=f"kokoro-v1.0.onnx.*{error}") as info:
            fetch_models(d, {"kokoro"}, say=quiet)
        assert "\n" not in str(info.value) and not list(model_dir(d, "kokoro").glob("*.part"))

    def hub_down(repo_id, **kw):
        raise ConnectionError("Network is unreachable\n(request id 42)")
    monkeypatch.setattr(models, "_snapshot_download", hub_down)
    with pytest.raises(ModelError, match="silero.*ConnectionError: Network is unreachable") as info:
        fetch_models(d, {"silero"}, say=quiet)
    assert "\n" not in str(info.value)


def test_hf_models_at_their_pins(tmp_path, monkeypatch, no_network):    # I4
    calls: list = []
    monkeypatch.setattr(models, "_snapshot_download", fake_snapshot(calls))
    d = tmp_path / "voice-data"
    fetch_models(d, {"silero", "parakeet"}, say=quiet)
    assert [repo for repo, _ in calls] == [MODEL_PINS["parakeet"].repo, MODEL_PINS["silero"].repo]
    for (_, kw), name in zip(calls, ("parakeet", "silero")):
        pin = MODEL_PINS[name]
        assert kw == {"revision": pin.revision, "local_dir": model_dir(d, name),
                      "allow_patterns": list(pin.files), "cache_dir": d / "hf-cache",
                      "token": False}
    assert os.environ["HF_HOME"] == str(d / "hf-cache")
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert verify(d) == []
    assert check(d, "silero") == [
        (True, "silero: istupakov/silero-vad-onnx @ b3e3ee3cce4c, 2 files as pinned")]


def test_verify_finds_problems(tmp_path, monkeypatch, small_kokoro):
    d = tmp_path / "voice-data"
    fetch_models(d, {"kokoro"}, say=quiet)
    k = model_dir(d, "kokoro")
    (k / "voices-v1.0.bin").write_bytes(b"changed")
    (k / "kokoro-v1.0.onnx").unlink()
    problems = verify(d)
    assert len(problems) == 2
    assert "kokoro-v1.0.onnx missing" in problems[0]
    assert f"voices-v1.0.bin sha256 {sha(b'changed')}, not the pinned {sha(VOICES)}" in problems[1]

    monkeypatch.setattr(models, "_snapshot_download", fake_snapshot([], revision="f" * 40))
    with pytest.raises(ModelError, match="revision ffffffffffff"):
        fetch_models(d, {"silero"}, say=quiet)
    monkeypatch.setattr(models, "_snapshot_download", fake_snapshot([]))
    fetch_models(d, {"parakeet"}, say=quiet)
    p = model_dir(d, "parakeet")
    (p / "vocab.txt").write_bytes(b"edited by hand")
    (p / "nemo128.onnx").unlink()
    (p / ".cache" / "huggingface" / "download" / "config.json.metadata").unlink()
    found = [text for ok, text in check(d, "parakeet") if ok is False]
    assert [t.split(":")[1].split()[0] for t in found] == ["nemo128.onnx", "vocab.txt",
                                                            "config.json"]
    assert "missing" in found[0] and "does not match" in found[1]
    assert "no download record" in found[2]
    assert set(found) <= set(verify(d))


def test_unknown_model_refused(tmp_path, no_network):
    with pytest.raises(ModelError, match="orpheus"):
        fetch_models(tmp_path / "voice-data", {"kokoro", "orpheus"}, say=quiet)
    assert not (tmp_path / "voice-data").exists()


def test_lab_dirs_find_the_main_checkout(tmp_path, monkeypatch):
    """A worktree's .git file points at <main>/.git/worktrees/<name>, whose commondir leads
    back to <main>/.git: the kit lives only in the main checkout."""
    main, tree = tmp_path / "main", tmp_path / "tree"
    gitdir = main / ".git" / "worktrees" / "tree"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="utf-8")
    monkeypatch.setattr(models, "REPO", tree)
    assert models.lab_dirs() == [tree, main.resolve()]
    monkeypatch.setattr(models, "REPO", main)          # the main checkout: .git is a folder
    assert models.lab_dirs() == [main]
