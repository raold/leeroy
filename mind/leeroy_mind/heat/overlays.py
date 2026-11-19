"""The owner's wording for the heat tiers: the loader, and a listing that never shows text.

The files are his, under mind-data/prompts/heat/ (README.md has the layout). Nothing here writes
there, and the repo holds no content for them: built-in text stays non-explicit, and a tier with
no file reads the tier below it. The three files that existed before the dial
(romance_explicit.txt, romance_explicit_whisper.txt, nightly_photo_lust.txt) are read exactly as
before and never renamed or moved.

Lookup order for a tier's wording: heat/<tier>.txt, then the legacy name
(romance_<band>.txt), then the next tier down. With no file in heat/ every tier reads what it
read before the dial.

The frank and explicit files are for private channels. With `[heat] public_cap` on, `variants`
refuses them for any other channel without opening the file (the resolver has already capped the
tier there; this is the second lock)."""
import logging
from pathlib import Path

from leeroy_mind.prompts import note_template, read_overlay
from leeroy_mind.text import estimate_tokens

log = logging.getLogger("leeroy_mind.heat")

FOLDER = "heat"
# name -> what it is for (the listing's words)
FILES = {
    "suggestive": "the suggestive tier's wording",
    "frank": "the frank tier's wording",
    "explicit": "the explicit tier's wording",
    "frank_whisper": "the frank tier's whisper tail",
    "explicit_whisper": "the explicit tier's whisper tail",
    "voice": "how a spoken line should differ from a typed one",
    "session_open": "the line she reads on a no-limits evening",
    "public_block": "words never to appear in public",
}
PRIVATE_ONLY = frozenset({"frank", "explicit", "frank_whisper", "explicit_whisper",
                          "session_open"})
LEGACY = {"suggestive": "romance_suggestive", "explicit": "romance_explicit",
          "explicit_whisper": "romance_explicit_whisper"}
_warned: set[str] = set()


def _read(name: str, data_dir: Path) -> str:
    found = read_overlay(name, data_dir, FOLDER)
    return found.text if found is not None else ""


def variants(name: str, data_dir: Path, *, private: bool = True,
             public_cap: bool = False) -> tuple[str, ...]:
    """prompts/heat/<name>.txt as its variants (split on `---` lines, each capped like the band
    overlays); () with no file or a blank one. A private-only file is never opened for a
    channel other people read while the public cap is on."""
    if name not in FILES:
        raise ValueError(f"no such heat file {name!r}")
    if name in PRIVATE_ONLY and public_cap and not private:
        return ()
    from leeroy_mind.talk.romance import _capped, split_variants
    text = _read(name, data_dir)
    return tuple(_capped(v, f"{FOLDER}/{name}", data_dir) for v in split_variants(text)) \
        if text else ()


def _pick(found: tuple[str, ...], rng):
    if not found:
        return None
    return found[0] if rng is None or len(found) == 1 else rng.choice(found)


def _warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def band_text(tier: str, data_dir: Path, rng=None, *, private: bool = True,
              public_cap: bool = False) -> str:
    """The wording of a text tier (warm, flirty, suggestive, frank or explicit) by the lookup
    order. Warm and flirty are romance.py's as ever."""
    from leeroy_mind.talk import romance
    if public_cap and not private and tier in ("suggestive", "frank", "explicit"):
        tier = "flirty"  # never reached: resolve has capped it; kept as the loader's own lock
    if tier in ("warm", "flirty"):
        return romance._band_text(tier, data_dir, rng)
    gate = dict(private=private, public_cap=public_cap)
    if tier == "explicit":
        text = _pick(variants("explicit", data_dir, **gate), rng)
        if text:
            note_template("talk.heat.explicit", text)
            return text
        if romance.overlay_variants("romance_explicit", data_dir):
            return romance._band_text("explicit", data_dir, rng)
        if not variants("frank", data_dir, **gate) and not variants("suggestive", data_dir):
            return romance._band_text("explicit", data_dir, rng)  # as before the dial
        tier = "frank"
    if tier == "frank":
        text = _pick(variants("frank", data_dir, **gate), rng)
        if text:
            note_template("talk.heat.frank", text)
            return text
        _warn_once(f"frank:{Path(data_dir).resolve()}",
                   "no heat/frank overlay in %s: the frank tier uses the suggestive wording",
                   Path(data_dir) / "prompts")
    text = _pick(variants("suggestive", data_dir), rng)
    if text:
        note_template("talk.heat.suggestive", text)
        return text
    return romance._band_text("suggestive", data_dir, rng)


def tail_variants(tier: str, data_dir: Path) -> tuple[str, ...]:
    """The whisper tail's variants for a tier (a whisper to him is private): explicit reads
    heat/explicit_whisper.txt, else the legacy romance_explicit_whisper.txt; frank reads
    heat/frank_whisper.txt; every other tier has none."""
    from leeroy_mind.talk import romance
    if tier == "explicit":
        return (variants("explicit_whisper", data_dir)
                or romance.overlay_variants(romance.WHISPER_OVERLAY, data_dir))
    if tier == "frank":
        return variants("frank_whisper", data_dir)
    return ()


def has_explicit(data_dir: Path) -> bool:
    """He has written explicit wording: heat/explicit.txt or the legacy romance_explicit.txt."""
    from leeroy_mind.talk import romance
    return bool(variants("explicit", data_dir)
                or romance.overlay_variants("romance_explicit", data_dir))


def one_line(name: str, data_dir: Path, rng=None) -> str:
    """A one-line file (voice, session_open): one of its variants, "" with none."""
    return _pick(variants(name, data_dir), rng) or ""


def block_words(data_dir: Path) -> tuple[str, ...]:
    """prompts/heat/public_block.txt: one word or phrase a line, lower-cased; blank lines and
    lines starting with # left out."""
    text = _read("public_block", data_dir)
    return tuple(dict.fromkeys(
        w for w in (" ".join(line.split()).lower() for line in text.splitlines())
        if w and not w.startswith("#")))


def listing(data_dir: Path) -> list[dict]:
    """Each file's name, purpose, whether it is there, its variant count and estimated tokens,
    and which fallback is in force without it. Never its text."""
    from leeroy_mind.talk import romance
    out = []
    for name, purpose in FILES.items():
        text = _read(name, data_dir)
        parts = romance.split_variants(text) if text else ()
        legacy = LEGACY.get(name)
        legacy_n = len(romance.overlay_variants(legacy, data_dir)) if legacy else 0
        if parts:
            reads = f"heat/{name}.txt"
        elif legacy_n:
            reads = f"{legacy}.txt"
        elif name == "explicit":
            reads = "the frank tier's wording"
        elif name == "frank":
            reads = "the suggestive tier's wording"
        elif name == "suggestive":
            reads = "the built-in suggestive wording"
        else:
            reads = "nothing"
        out.append({"name": name, "file": f"heat/{name}.txt", "purpose": purpose,
                    "present": bool(parts), "variants": len(parts),
                    "tokens": sum(estimate_tokens(v) for v in parts),
                    "legacy": f"{legacy}.txt" if legacy else None, "legacy_variants": legacy_n,
                    "reads": reads})
    return out
