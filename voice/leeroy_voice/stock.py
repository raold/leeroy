"""Stock callouts (draft section 4.3): the short neutral lines members say all night ("pulling",
"oom", "ready"), rendered once per voice and kept on disk, so they play with no synthesis delay
(Kokoro's short lines are its weakest; these are rendered ahead, once). Only these lines ever
touch the disk: never a line a member actually said, nothing romantic or explicit (draft section
11).

A clip is dry, at the engine's rate: the pitch shift, the mic chain and the loudness come at play
time (fx.process), so the same clip serves a whisper or any grit. It is a float WAV in
root/<guid>/<voice_hash>/ (root is voice-data/cache/stock). The folder is keyed by the voice (I6):
voice_hash covers what a dry clip depends on, the spec's RENDER_FIELDS and the decision's engine,
prosody_swap and uk_timbre_mix, so a recast or a new decision never plays an old voice, and
drop_stale removes the member's other folders. The mic, pitch and gain act at play time and the
name and the lock never reach the sound, so locking the guild or changing a mic keeps the clips.
Each clip's file name ends in a digest of what was rendered (the line, the engine and its rate,
the speed), so a changed line or engine renders afresh too and the old clip is removed.

A stock line reads as a raid call does, whatever the line's attitude and state: the voice's
neutral style at its speed times attitude.RAID_CALL_LEVERS (x1.08), the clip a callout or urgent
line of these words would render live.

stock_key(text) finds a line's callout: the line casefolded, without apostrophes, its other
punctuation and spacing ignored, must be a key, an alias or the callout's own words ("Pulling!",
"pull", "Stack up!"); anything more ("OOM in 10") is not a stock line."""
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

import numpy as np
import soundfile as sf

from .attitude import RAID_CALL_LEVERS
from .engines import EngineError
from .logs import log
from .voices import spec_to_dict

STOCK: dict[str, str] = {
    "pulling": "Pulling.", "inc": "Incoming!", "oom": "I'm out of mana.", "ready": "Ready.",
    "adds": "Adds!", "help": "Help!", "heal me": "Heal me!", "rez": "Can I get a rez?",
    "brez": "Battle rez!", "taunt": "Taunt!", "lust": "Bloodlust, now!", "go": "Go go go!",
    "wait": "Wait.", "stop": "Stop!", "spread": "Spread out!", "stack": "Stack up!",
    "move": "Move!",
}
ALIASES: dict[str, str] = {
    "pull": "pulling", "pulling now": "pulling", "incoming": "inc", "out of mana": "oom",
    "bl": "lust", "hero": "lust", "heroism": "lust",
}
CLIP_FORMAT = 2  # expressive clips now honor speed; regenerate old clips that ignored it
# the spec's fields a dry clip depends on; mic and pitch_st act in fx.process, gain_db in the
# mixer, and version, name and locked never reach the sound
RENDER_FIELDS = ("guid", "engine", "lang", "timbre", "prosody", "speed", "ref", "voices_sha256")
_VOICE_HASH = re.compile(r"[0-9a-f]{12}")
_WORDS = re.compile(r"[^\W_]+")
_APOSTROPHES = dict.fromkeys(map(ord, "'`‘’ʼ"), None)


def _phrase(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_APOSTROPHES)
    return " ".join(_WORDS.findall(folded))


_PHRASES: dict[str, str] = {
    **{_phrase(text): key for key, text in STOCK.items()},
    **{_phrase(alias): key for alias, key in ALIASES.items()},
    **{_phrase(key): key for key in STOCK},
}


def stock_key(text: str) -> str | None:
    """The STOCK key that `text` says, or None when it is not a stock line."""
    return _PHRASES.get(_phrase(text))


class StockCache:
    """Each member's stock clips in `root`, rendered by `engine` in the style `bank` builds for
    the spec under `decision`."""

    def __init__(self, root: Path, engine, bank, decision):
        self.root = Path(root)
        self.engine = engine
        self.bank = bank
        self.decision = decision

    def voice_hash(self, spec) -> str:
        """The first 12 hex digits of the sha256 of the spec's RENDER_FIELDS and the decision's
        engine, prosody_swap and uk_timbre_mix (keys sorted, so a blend's order does not count).
        SpecError for a spec changed in place since it was made."""
        d, fields = self.decision, spec_to_dict(spec)
        voice = {"spec": {k: fields[k] for k in RENDER_FIELDS}, "engine": d.engine,
                 "prosody_swap": d.prosody_swap, "uk_timbre_mix": d.uk_timbre_mix}
        blob = json.dumps(voice, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(blob.encode("ascii")).hexdigest()[:12]

    def get(self, spec, key: str) -> np.ndarray | None:
        """The member's clip for a STOCK key: mono float32, dry, at the engine's rate; None when
        it is not rendered for this voice (or `key` is not a STOCK key)."""
        if key not in STOCK:
            return None
        return self._load(spec.guid, key, self._clip(self._folder(spec), spec, key))

    def render_missing(self, spec) -> int:
        """Render the member's clips this voice lacks; how many it wrote. A line the engine fails
        on is logged and left for the next pass. SpecError when the voice cannot be spoken (the
        bank refuses its style); then nothing is written."""
        folder = self._folder(spec)
        missing = [key for key in STOCK
                   if self._load(spec.guid, key, self._clip(folder, spec, key)) is None]
        written = 0
        if missing:
            style = self.bank.style(spec, "none", self.decision)
            folder.mkdir(parents=True, exist_ok=True)
            for key in missing:
                audio = self._render(spec, style, key)
                if audio is not None:
                    _write(self._clip(folder, spec, key), audio, self.engine.sample_rate)
                    written += 1
            log("stock_rendered", guid=spec.guid, clips=written, failed=len(missing) - written)
        if folder.is_dir():  # what an older line, engine or format left behind
            current = {self._clip(folder, spec, key).name for key in STOCK}
            for old in folder.glob("*.wav"):
                if old.name not in current:
                    old.unlink(missing_ok=True)
        return written

    def drop_stale(self, spec) -> int:
        """Remove the member's clip folders under any other voice hash; how many it removed."""
        member, keep = self.root / str(spec.guid), self.voice_hash(spec)
        if not member.is_dir():
            return 0
        dropped = 0
        for d in member.iterdir():
            if (d.name != keep and _VOICE_HASH.fullmatch(d.name) and d.is_dir()
                    and not d.is_symlink() and not d.is_junction()):
                shutil.rmtree(d)
                dropped += 1
        if dropped:
            log("stock_dropped", guid=spec.guid, folders=dropped)
        return dropped

    def _folder(self, spec) -> Path:
        return self.root / str(spec.guid) / self.voice_hash(spec)

    def _speed(self, spec) -> float:
        # RAID_CALL_LEVERS as this module imported it: a constant, so a later rebinding of
        # attitude.RAID_CALL_LEVERS would not reach here (a test patches stock.RAID_CALL_LEVERS)
        return spec.speed * RAID_CALL_LEVERS.speed_mult

    def _clip(self, folder: Path, spec, key: str) -> Path:
        made = json.dumps([CLIP_FORMAT, STOCK[key], self.engine.name, self.engine.sample_rate,
                           self._speed(spec)], ensure_ascii=True)
        digest = hashlib.sha256(made.encode("ascii")).hexdigest()[:8]
        return folder / f"{key.replace(' ', '_')}-{digest}.wav"

    def _render(self, spec, style, key: str) -> np.ndarray | None:
        try:
            audio = np.asarray(self.engine.synth(STOCK[key], spec, style, self._speed(spec)),
                               dtype=np.float32)
        except EngineError as e:  # its message carries no line text
            log("stock_render_failed", level=logging.WARNING, guid=spec.guid, key=key,
                error=str(e))
            return None
        if audio.ndim != 1 or not len(audio) or not np.isfinite(audio).all():
            log("stock_render_failed", level=logging.WARNING, guid=spec.guid, key=key,
                error=f"not a mono clip: shape {audio.shape}, or not finite")
            return None
        return audio

    def _load(self, guid: int, key: str, path: Path) -> np.ndarray | None:
        if not path.is_file():
            return None
        try:
            audio, rate = sf.read(path, dtype="float32")
        except (sf.SoundFileError, OSError) as e:
            log("stock_clip_unreadable", level=logging.WARNING, guid=guid, key=key,
                error=type(e).__name__)
            return None
        if (rate != self.engine.sample_rate or audio.ndim != 1 or not len(audio)
                or not np.isfinite(audio).all()):
            log("stock_clip_unreadable", level=logging.WARNING, guid=guid, key=key,
                error=f"{rate} Hz, shape {audio.shape}")
            return None
        return audio


def _write(path: Path, audio: np.ndarray, rate: int) -> None:
    """Write the clip whole or not at all: a reader never sees half of one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    os.close(fd)
    try:
        sf.write(tmp, audio, rate, subtype="FLOAT", format="WAV")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
