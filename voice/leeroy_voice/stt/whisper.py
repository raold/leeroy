"""Whisper large-v3-turbo, int8, through faster-whisper 1.2.1 on the GPU: the recognition upgrade
(draft section 6.4), English only, in the service only while GpuGuard holds it (gpu.py). It hears
only what the speech gate passes (stt/vad.py: faster-whisper's own VAD stays off, settled item
14), and `transcribe` is called with the pinned arguments: language "en", beam_size 1,
condition_on_previous_text False, vad_filter False and the moment's hotwords.

Hotwords bias the decoder toward names: what the service passes (his room's occupants first,
then the last 10 members who spoke, service.py), then WoW words, each once, cut to 223 tokens (faster-whisper itself keeps no more than
that, half the decoder's prompt window) by the model's own tokenizer. The room's names are
never cut for the rest.

CTranslate2 4.6.3 and later needs no cuDNN, only cuBLAS 12, which the `gpu` extra installs as a
wheel: its folder is added with os.add_dll_directory before the model loads. faster-whisper is
imported only when a recognizer is made (M9). The model loads from its folder, which must exist;
close() drops it so the card is free again."""
import gc
import importlib.util
import math
import os
import sys
from pathlib import Path

import numpy as np

from . import Transcript

HOTWORD_TOKENS = 223
WOW_WORDS = (
    "tank", "healer", "adds", "pull", "wipe", "boss", "aggro", "interrupt", "kite", "enrage",
    "cooldown", "resurrect", "mana", "heroic", "raid", "dungeon", "loot", "ready", "buff",
    "debuff", "polymorph", "Azeroth", "Northrend", "Icecrown", "Ulduar", "Naxxramas",
    "Dalaran", "Orgrimmar", "Stormwind", "Arthas", "Shaman", "Paladin", "Druid",
    "Warlock", "Hunter", "Rogue", "Mage", "Priest", "Warrior",
)
_dll_handles: list[tuple[str, object]] = []  # kept alive: closing a handle removes the folder


def build_hotwords(occupants, speakers, wow_words, count) -> str:
    """The hotwords string: occupants, then speakers, then WoW words, each once (case aside),
    stopping before `count(text)` passes HOTWORD_TOKENS. `count` is the tokenizer's length."""
    seen: set[str] = set()
    kept: list[str] = []
    for word in (*occupants, *speakers, *wow_words):
        key = word.casefold()
        if not word.strip() or key in seen:
            continue
        if count(" ".join((*kept, word))) > HOTWORD_TOKENS:
            break
        seen.add(key)
        kept.append(word)
    return " ".join(kept)


def _cublas_dir() -> Path | None:
    """site-packages/nvidia/cublas/bin, where the `nvidia-cublas-cu12` wheel puts its DLLs."""
    try:
        spec = importlib.util.find_spec("nvidia.cublas")
    except (ImportError, ValueError):
        spec = None
    roots = list(spec.submodule_search_locations or ()) if spec else []
    roots += [str(Path(p) / "nvidia" / "cublas") for p in sys.path if p]
    for root in roots:
        folder = Path(root) / "bin"
        if folder.is_dir():
            return folder
    return None


def _add_dlls() -> None:
    folder = _cublas_dir()
    if folder is None or not hasattr(os, "add_dll_directory"):
        return
    if any(path == str(folder) for path, _ in _dll_handles):
        return
    _dll_handles.append((str(folder), os.add_dll_directory(str(folder))))


def _load_whisper(model_dir: Path, compute_type: str):
    from faster_whisper import WhisperModel
    return WhisperModel(str(model_dir), device="cuda", compute_type=compute_type)


def _counter(model):
    tokenizer = getattr(model, "hf_tokenizer", None)
    if tokenizer is None:  # a stand-in: three tokens a word is a safe over-count
        return lambda text: 3 * len(text.split())
    return lambda text: len(tokenizer.encode(" " + text.strip(), add_special_tokens=False).ids)


class WhisperRecognizer:
    name = "whisper"

    def __init__(self, model_dir, gate, *, compute_type: str = "int8"):
        d = Path(model_dir)
        if not d.is_dir():
            raise FileNotFoundError(f"{d}: no Whisper model there (fetch-models --only whisper)")
        self.model_dir, self.gate = d, gate
        _add_dlls()
        self._model = _load_whisper(d, compute_type)
        self._count = _counter(self._model)

    def transcribe(self, audio16k, hotwords) -> Transcript | None:
        model = self._model
        if model is None:
            raise RuntimeError("the Whisper model has been closed")
        cut = self.gate.speech(audio16k)
        if cut is None:
            return None
        audio, speech_ms = cut
        hot = build_hotwords(hotwords, (), WOW_WORDS, self._count)
        segments, _ = model.transcribe(
            audio, language="en", beam_size=1, condition_on_previous_text=False,
            vad_filter=False, hotwords=hot)
        texts, confidences = [], []
        for segment in segments:
            text = " ".join(segment.text.split())
            if text:
                texts.append(text)
                p = math.exp(segment.avg_logprob)
                confidences.append(min(1.0, max(0.0, p)) if p == p else 0.0)
        if not texts:
            return Transcript("", 0.0, int(speech_ms))
        return Transcript(" ".join(texts), float(np.mean(confidences)), int(speech_ms))

    def close(self) -> None:
        self._model = None
        gc.collect()
