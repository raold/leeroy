"""The audition page (Task 19): every member reading one line in their voice, as the mixer would
play it (the voice through the mic chain at the configured grit), with name, gender, accent and
lock state, and the closest pairs by ECAPA cosine played side by side, so the owner's ear can set
the distinctness threshold (M25). A static page with its clips beside it; the whole folder is
local (voice-data/audition), never committed.

The line is SAMPLE with the member's name, rewritten for the ear and the lexicon like any line
(so a name misaki gets wrong shows up here, and Task 19's step 6 adds it to the lexicon)."""
import html
import os
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from . import fx
from .engines import EngineError
from .lexicon import Lexicon
from .textear import ear
from .voices import BASES, VoiceSpec

SAMPLE = "Hey, it's {name}. Pulling in three, healers on the tank."
PAGE = "index.html"
CLIPS = "clips"


def gender_of(spec: VoiceSpec) -> str:
    return "woman" if all(BASES[b][0] == "f" for b in spec.timbre) else "man"


def accent_of(spec: VoiceSpec) -> str:
    return "British" if spec.lang == "b" else "American"


def _write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    os.close(fd)
    try:
        sf.write(tmp, audio, rate, subtype="PCM_16", format="WAV")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _line(spec: VoiceSpec, engine, lexicon: Lexicon | None) -> str:
    text = SAMPLE.format(name=spec.name)
    if lexicon is None:
        return text
    return ear(text, lexicon=lexicon, engine=engine.name).text or text


def build_audition(specs, engine, out_dir: Path, *, report: dict, bank, decision,
                   grit: int = 3, lexicon: Lexicon | None = None, expressive=None) -> Path:
    """<out_dir>/index.html and <out_dir>/clips/<guid>.wav for each spec; the page path.
    `report` is the cast report (cast.cast_voices fills it): its closest_pairs and threshold.
    `engine` and `bank` are Kokoro's. For a cloning spec, `expressive(name)` supplies its
    running engine; the caller owns its lifetime. A missing or different engine is an error,
    never an audition of the member's fallback blend. A single cloning engine may also be
    passed as `engine` when no resolver is needed."""
    out_dir = Path(out_dir)
    clips = out_dir / CLIPS
    clips.mkdir(parents=True, exist_ok=True)
    by_guid = {s.guid: s for s in specs}
    for spec in specs:
        if spec.engine == "kokoro":
            chosen = engine
            if chosen is None or bank is None:
                raise EngineError("kokoro: no engine for audition")
            style = bank.style(spec, "none", decision)
        else:
            chosen = expressive(spec.engine) if expressive is not None else engine
            if chosen is None or chosen.name != spec.engine:
                raise EngineError(f"{spec.engine}: no matching engine for audition of {spec.guid}")
            style = chosen.levers("none")
        dry = chosen.synth(_line(spec, chosen, lexicon), spec, style, spec.speed)
        clip = fx.process(dry, chosen.sample_rate, spec, grit=grit, whisper=False)
        _write_wav(clips / f"{spec.guid}.wav", np.clip(clip, -1.0, 1.0), fx.OUT_RATE)
    page = out_dir / PAGE
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".index-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(_html(sorted(specs, key=lambda s: (s.name.casefold(), s.guid)), by_guid,
                          report))
        os.replace(tmp, page)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return page


def _player(spec: VoiceSpec | None, guid: int) -> str:
    if spec is None:
        return f"<span>{guid} (no spec)</span>"
    return (f'<span class="who">{html.escape(spec.name)} <small>{spec.guid}</small></span>'
            f'<audio controls preload="none" src="{CLIPS}/{spec.guid}.wav"></audio>')


def _html(specs, by_guid, report) -> str:
    threshold = report.get("threshold")
    rows = []
    for s in specs:
        state = "locked" if s.locked else "provisional"
        rows.append(
            f'<tr class="{state}"><td>{html.escape(s.name)}</td><td>{s.guid}</td>'
            f"<td>{gender_of(s)}</td><td>{accent_of(s)}</td><td>{state}</td>"
            f'<td><audio controls preload="none" src="{CLIPS}/{s.guid}.wav"></audio></td></tr>')
    pairs = []
    for p in report.get("closest_pairs", []):
        pairs.append(f'<li><b>{p["cosine"]:.3f}</b> '
                     f'{_player(by_guid.get(p["a"]), p["a"])} '
                     f'{_player(by_guid.get(p["b"]), p["b"])}</li>')
    limit = f" (threshold {threshold:.2f})" if isinstance(threshold, (int, float)) else ""
    pair_note = ("These scores come from the saved casting report, not from the clips on this "
                 "page. Designed reference voices may differ from the voices scored during "
                 "casting." if any(s.engine != "kokoro" for s in specs) else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Guild voices: audition</title>
<style>
body {{ font: 15px system-ui, sans-serif; margin: 1.5rem auto; max-width: 62rem; padding: 0 1rem; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ padding: .3rem .5rem; border-bottom: 1px solid #8884; text-align: left; }}
tr.locked td:nth-child(5) {{ font-weight: 600; }}
li {{ margin: .5rem 0; }} .who {{ margin: 0 .5rem; }} small {{ opacity: .6; }}
audio {{ height: 2rem; vertical-align: middle; }}
</style></head><body>
<h1>Guild voices: audition</h1>
<p>{len(specs)} members, each reading one line as the mixer plays it. Voices stay provisional
until you lock them.</p>
<h2>Closest pairs{html.escape(limit)}</h2>
<p>The ten pairs ECAPA finds most alike, by cosine (1 is the same voice). Listen to each pair
side by side; if they are clearly different people, the threshold can go up.</p>
{f'<p>{pair_note}</p>' if pair_note else ''}
<ol>
{chr(10).join(pairs)}
</ol>
<h2>Everyone</h2>
<table><thead><tr><th>Name</th><th>Guid</th><th>Gender</th><th>Accent</th><th>State</th>
<th>Listen</th></tr></thead><tbody>
{chr(10).join(rows)}
</tbody></table>
</body></html>
"""
