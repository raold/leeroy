"""The pinned models, fetched by fetch-models into <data dir>/models/<name>.

Each is a HuggingFace snapshot at its pinned revision, only the files its loader reads.
huggingface_hub keeps a download record of each file in the folder
(.cache/huggingface/download/<file>.metadata: the commit, the etag, the time), which check()
reads for the revision and the content. check() and verify() only read. A model not fetched is
a note; a file missing, changed or from another revision is a problem."""
import hashlib
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

HF_CACHE = "hf-cache"  # in the data dir: HF_HOME while fetching

_UNET = ("unet/config.json", "unet/diffusion_pytorch_model.fp16.safetensors",
         "unet/diffusion_pytorch_model.safetensors.index.json")
_TOKENIZER = ("merges.txt", "special_tokens_map.json", "tokenizer_config.json", "vocab.json")


class ModelError(Exception):
    """A fetch or check that failed; its message is one line."""


def _reason(e: BaseException) -> str:
    text = " ".join(str(e).split())
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


@dataclass(frozen=True)
class ModelPin:
    name: str
    title: str
    licence: str
    repo: str
    revision: str  # the repo's pinned commit
    files: tuple[str, ...]  # the snapshot's allow_patterns


MODEL_PINS: dict[str, ModelPin] = {
    "realvisxl": ModelPin(
        "realvisxl", "RealVisXL V5.0 (SDXL), fp16", "openrail++",
        "SG161222/RealVisXL_V5.0", "ac93e0dda1f6d448cae19bbfab8c5e720a5e48bc",
        ("model_index.json", "scheduler/scheduler_config.json",
         "text_encoder/config.json", "text_encoder/model.fp16.safetensors",
         "text_encoder_2/config.json", "text_encoder_2/model.fp16.safetensors",
         *(f"tokenizer/{f}" for f in _TOKENIZER), *(f"tokenizer_2/{f}" for f in _TOKENIZER),
         *_UNET, "vae/config.json", "vae/diffusion_pytorch_model.fp16.safetensors")),
    "nsfw": ModelPin(
        "nsfw", "Falconsai NSFW image classifier (ViT)", "Apache-2.0",
        "Falconsai/nsfw_image_detection", "96cb0d0342c7afb80cab76ecc58b265fa44da256",
        ("config.json", "model.safetensors", "preprocessor_config.json")),
    "ipadapter": ModelPin(
        "ipadapter", "IP-Adapter Plus Face (SDXL) with its ViT-H image encoder", "Apache-2.0",
        "h94/IP-Adapter", "018e402774aeeddd60609b4ecdb7e298259dc729",
        ("models/image_encoder/config.json", "models/image_encoder/model.safetensors",
         "sdxl_models/ip-adapter-plus-face_sdxl_vit-h.safetensors")),
}
BASE_PINS = ("realvisxl", "nsfw")  # what every worker needs
IDENTITY_PINS = ("ipadapter",)  # what only an --identity worker needs


def model_dir(data_dir: Path, name: str) -> Path:
    if name not in MODEL_PINS:
        raise ModelError(f"unknown model {name!r}: one of {', '.join(MODEL_PINS)}")
    return Path(data_dir) / "models" / name


def check(data_dir: Path, name: str, *, quick: bool = False) -> list[tuple[bool | None, str]]:
    """One model as (ok, text) lines, as the doctor prints them: None a note (not fetched),
    False a problem. Reads download records and, unless `quick`, hashes each file."""
    d = model_dir(data_dir, name)
    pin = MODEL_PINS[name]
    if not any((d / f).is_file() for f in pin.files):
        return [(None, f"{name}: not fetched (fetch-models --only {name})")]
    problems = []
    for f in pin.files:
        record = _record(d, f) if (d / f).is_file() else None
        if not (d / f).is_file():
            problems.append(f"{f} missing (fetch-models --only {name})")
        elif record is None:
            problems.append(f"{f} has no download record (fetch-models --only {name})")
        elif record[0] != pin.revision:
            problems.append(f"{f} is from revision {record[0][:12]}, not the pinned "
                            f"{pin.revision[:12]}")
        elif not quick and not _matches(d / f, record[1]):
            problems.append(f"{f} does not match its download record (etag {record[1][:12]})")
    if problems:
        return [(False, f"{name}: {p}") for p in problems]
    how = "present (contents not hashed)" if quick else "as pinned"
    return [(True, f"{name}: {pin.repo} @ {pin.revision[:12]}, {len(pin.files)} files {how}")]


def verify(data_dir: Path) -> list[str]:
    """Every problem with the models fetched; one not fetched at all is none."""
    return [text for name in MODEL_PINS for ok, text in check(data_dir, name) if ok is False]


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
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest() == etag
    data = path.read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False)
    return blob.hexdigest() == etag


def _snapshot_download(repo_id: str, **kw):
    from huggingface_hub import snapshot_download
    return snapshot_download(repo_id, **kw)


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
    cache = data_dir / HF_CACHE
    for name in names:
        pin = MODEL_PINS[name]
        # huggingface_hub reads these when it is first imported; no model needs a token
        os.environ["HF_HOME"] = str(cache)
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        say(f"{name}: {pin.repo} @ {pin.revision[:12]}")
        try:
            _snapshot_download(pin.repo, revision=pin.revision,
                               local_dir=model_dir(data_dir, name),
                               allow_patterns=list(pin.files), cache_dir=cache, token=False)
        except Exception as e:  # huggingface_hub's and httpx's errors share no narrower base
            raise ModelError(f"{name}: {pin.repo} @ {pin.revision[:12]} failed: "
                             f"{_reason(e)}") from e
    lines = [line for name in names for line in check(data_dir, name)]
    problems = [text for ok, text in lines if ok is not True]
    if problems:
        raise ModelError("; ".join(problems))
    return lines
