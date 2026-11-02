"""The pinned models (the plan's models table), fetched by fetch-models into
<data dir>/models/<name>.

- Kokoro's two files, from the kokoro-onnx release `model-files-v1.0`: copied from the listening
  kit (private/voice-lab/models/kokoro, in this repo or, from a git worktree, in its main
  checkout) when it holds them, else downloaded; either way checked against the pinned sha256.
- Every HuggingFace model: snapshot_download at its pinned revision into its own folder, only
  the files its loader reads. onnx-asr 0.12.0's loaders take no revision (I4), so the service
  loads them offline (HF_HUB_OFFLINE=1) from that folder. huggingface_hub keeps a download
  record of each file there (.cache/huggingface/download/<file>.metadata: the commit, the etag,
  the time), which check() reads for the revision and the content.

check() and verify() only read. A model not fetched is a note; a file missing, changed or
from another revision is a problem."""
import hashlib
import http.client
import os
import shutil
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HF_CACHE = "hf-cache"  # in the data dir: HF_HOME while fetching
DOWNLOAD_TIMEOUT_S = 60
_CHUNK = 1 << 20

_urlopen = urllib.request.urlopen


class ModelError(Exception):
    """A fetch or check that failed; its message is one line."""


def _reason(e: BaseException) -> str:
    """An error as one line: its type and message, whitespace folded."""
    text = " ".join(str(e).split())
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


@dataclass(frozen=True)
class ModelPin:
    name: str
    title: str
    licence: str
    files: tuple[str, ...]  # what is fetched: a release's files, or a repo's allow_patterns
    repo: str = ""  # a HuggingFace model repo; "" for Kokoro's release
    revision: str = ""  # the repo's pinned commit
    url: str = ""  # where the release's files are downloaded from
    sha256: dict[str, str] = field(default_factory=dict)  # each release file's
    lab: str = ""  # the listening kit's copy of the release, relative to the repo


MODEL_PINS: dict[str, ModelPin] = {
    "kokoro": ModelPin(
        "kokoro", "Kokoro-82M v1.0 ONNX fp32", "Apache-2.0",
        ("kokoro-v1.0.onnx", "voices-v1.0.bin"),
        url="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0",
        sha256={
            "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
            "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"},
        lab="private/voice-lab/models/kokoro"),
    "parakeet": ModelPin(
        "parakeet", "Parakeet TDT 0.6B v2, int8 ONNX", "CC-BY-4.0",
        ("encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "nemo128.onnx", "vocab.txt",
         "config.json"),
        repo="istupakov/parakeet-tdt-0.6b-v2-onnx",
        revision="0bbb45a3365852604aef28b538a8f066f4ccaa85"),
    # silero_vad.onnx: the file SileroVad._get_model_files() names in onnx-asr 0.12.0
    "silero": ModelPin(
        "silero", "Silero VAD ONNX", "MIT", ("config.json", "silero_vad.onnx"),
        repo="istupakov/silero-vad-onnx", revision="b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6"),
    # what faster-whisper 1.2.1's own download takes (utils.download_model)
    "whisper": ModelPin(
        "whisper", "Whisper large-v3-turbo, CTranslate2", "MIT",
        ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json",
         "vocabulary.json"),
        repo="dropbox-dash/faster-whisper-large-v3-turbo",
        revision="0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"),
    # hyperparams.yaml and the four files its pretrainer loads
    "ecapa": ModelPin(
        "ecapa", "ECAPA speaker embedding", "Apache-2.0",
        ("hyperparams.yaml", "embedding_model.ckpt", "mean_var_norm_emb.ckpt", "classifier.ckpt",
         "label_encoder.txt"),
        repo="speechbrain/spkrec-ecapa-voxceleb",
        revision="0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"),
    # what ChatterboxTurboTTS.from_local reads, not the repo's unused s3gen.safetensors
    "chatterbox-turbo": ModelPin(
        "chatterbox-turbo", "Chatterbox Turbo", "MIT",
        ("ve.safetensors", "t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors", "conds.pt",
         "added_tokens.json", "merges.txt", "special_tokens_map.json", "tokenizer_config.json",
         "vocab.json"),
        repo="ResembleAI/chatterbox-turbo",
        revision="749d1c1a46eb10492095d68fbcf55691ccf137cd"),
}


def model_dir(data_dir: Path, name: str) -> Path:
    if name not in MODEL_PINS:
        raise ModelError(f"unknown model {name!r}: one of {', '.join(MODEL_PINS)}")
    return Path(data_dir) / "models" / name


def check(data_dir: Path, name: str) -> list[tuple[bool | None, str]]:
    """One model as (ok, text) lines, as the doctor prints them: None a note (not fetched),
    False a problem."""
    d = model_dir(data_dir, name)
    pin = MODEL_PINS[name]
    if not any((d / f).is_file() for f in pin.files):
        return [(None, f"{name}: not fetched (fetch-models --only {name})")]
    return _check_snapshot(pin, d) if pin.repo else _check_release(pin, d)


def verify(data_dir: Path) -> list[str]:
    """Every problem with the models fetched; one not fetched at all is none."""
    return [text for name in MODEL_PINS for ok, text in check(data_dir, name) if ok is False]


def _sha256(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _check_release(pin: ModelPin, d: Path) -> list[tuple[bool | None, str]]:
    out: list[tuple[bool | None, str]] = []
    for f in pin.files:
        if not (d / f).is_file():
            out.append((False, f"{pin.name}: {f} missing (fetch-models --only {pin.name})"))
            continue
        got, want = _sha256(d / f), pin.sha256[f]
        out.append((True, f"{pin.name}: {f} sha256 {got} as pinned") if got == want else
                   (False, f"{pin.name}: {f} sha256 {got}, not the pinned {want}"))
    return out


def _record(d: Path, f: str) -> tuple[str, str] | None:
    """huggingface_hub's download record of a file in a local_dir: (commit, etag)."""
    path = d / ".cache" / "huggingface" / "download" / f"{f}.metadata"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return (lines[0].strip(), lines[1].strip()) if len(lines) >= 2 else None


def _matches(path: Path, etag: str) -> bool:
    """A large (LFS) file's etag is its sha256, a small file's its git blob id."""
    if len(etag) == 64:
        return _sha256(path) == etag
    data = path.read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False)
    return blob.hexdigest() == etag


def _check_snapshot(pin: ModelPin, d: Path) -> list[tuple[bool | None, str]]:
    problems = []
    for f in pin.files:
        record = _record(d, f) if (d / f).is_file() else None
        if not (d / f).is_file():
            problems.append(f"{f} missing (fetch-models --only {pin.name})")
        elif record is None:
            problems.append(f"{f} has no download record (fetch-models --only {pin.name})")
        elif record[0] != pin.revision:
            problems.append(f"{f} is from revision {record[0][:12]}, not the pinned "
                            f"{pin.revision[:12]}")
        elif not _matches(d / f, record[1]):
            problems.append(f"{f} does not match its download record (etag {record[1][:12]})")
    if problems:
        return [(False, f"{pin.name}: {p}") for p in problems]
    return [(True, f"{pin.name}: {pin.repo} @ {pin.revision[:12]}, {len(pin.files)} files as "
                   "pinned")]


def lab_dirs() -> list[Path]:
    """Where the listening kit may keep its copies: this repo, then, when it is a git worktree,
    the main checkout (private/ lives only there)."""
    dirs = [REPO]
    main = _main_checkout(REPO)
    if main is not None and main != REPO:
        dirs.append(main)
    return dirs


def _main_checkout(repo: Path) -> Path | None:
    """A worktree's .git file names <main>/.git/worktrees/<name>, whose commondir leads back to
    <main>/.git; in the main checkout .git is a folder."""
    try:
        text = (repo / ".git").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text.removeprefix("gitdir:").strip())
    gitdir = gitdir if gitdir.is_absolute() else repo / gitdir
    try:
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return (gitdir / common).resolve().parent


def fetch_models(data_dir: Path, only: Iterable[str] | None = None, *,
                 say: Callable[[str], None] = print) -> list[tuple[bool | None, str]]:
    """Fetch the models named in `only` (every pinned one when None), in the table's order, and
    return their check() lines. Raises ModelError for an unknown name (before fetching
    anything) or with every problem left once fetched."""
    data_dir = Path(data_dir)
    wanted = set(MODEL_PINS) if only is None else set(only)
    unknown = sorted(wanted - MODEL_PINS.keys())
    if unknown:
        raise ModelError(f"unknown model(s) {', '.join(unknown)}: one of {', '.join(MODEL_PINS)}")
    names = [n for n in MODEL_PINS if n in wanted]
    for name in names:
        pin = MODEL_PINS[name]
        (_fetch_snapshot if pin.repo else _fetch_release)(pin, data_dir, say)
    lines = [line for name in names for line in check(data_dir, name)]
    problems = [text for ok, text in lines if ok is False]
    if problems:
        raise ModelError("; ".join(problems))
    return lines


def _fetch_release(pin: ModelPin, data_dir: Path, say: Callable[[str], None]) -> None:
    d = model_dir(data_dir, pin.name)
    for f in pin.files:
        target, want = d / f, pin.sha256[f]
        if target.is_file() and _sha256(target) == want:
            say(f"{pin.name}: {f} already fetched")
            continue
        d.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        kit = next((p for p in (base / pin.lab / f for base in lab_dirs()) if p.is_file()), None)
        try:
            how = _get(pin, f, kit, part, say)
            got = _sha256(part)
            if got != want:
                raise ModelError(f"{pin.name}: {f} {how} has sha256 {got}, not the pinned {want}")
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)
        say(f"{pin.name}: {f} {how}")


def _get(pin: ModelPin, f: str, kit: Path | None, part: Path, say: Callable[[str], None]) -> str:
    """Copy `f` from the kit, else download it, into `part`; returns how. A failure, the
    network's included, is a ModelError."""
    try:
        if kit is not None:
            shutil.copyfile(kit, part)  # a copy: the kit keeps its own
            return f"copied from {kit}"
        say(f"{pin.name}: downloading {f} from {pin.url}")
        with _urlopen(f"{pin.url}/{f}", timeout=DOWNLOAD_TIMEOUT_S) as r, open(part, "wb") as out:
            shutil.copyfileobj(r, out, _CHUNK)
        return f"downloaded from {pin.url}"
    except (OSError, http.client.HTTPException) as e:
        what = "the copy from the kit" if kit is not None else "the download"
        raise ModelError(f"{pin.name}: {f}: {what} failed: {_reason(e)}") from e


def _snapshot_download(repo_id: str, **kw):
    from huggingface_hub import snapshot_download
    return snapshot_download(repo_id, **kw)


def _fetch_snapshot(pin: ModelPin, data_dir: Path, say: Callable[[str], None]) -> None:
    cache = data_dir / HF_CACHE
    # huggingface_hub reads these when it is first imported; no model needs a token
    os.environ["HF_HOME"] = str(cache)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    say(f"{pin.name}: {pin.repo} @ {pin.revision[:12]}")
    try:
        _snapshot_download(pin.repo, revision=pin.revision, local_dir=model_dir(data_dir, pin.name),
                           allow_patterns=list(pin.files), cache_dir=cache, token=False)
    except Exception as e:  # huggingface_hub's and httpx's errors share no narrower base
        raise ModelError(f"{pin.name}: {pin.repo} @ {pin.revision[:12]} failed: "
                         f"{_reason(e)}") from e
