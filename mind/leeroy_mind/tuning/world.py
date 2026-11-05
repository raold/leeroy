"""World dials over mind.toml: an override stored in mind.db replaces mind.toml's value wherever
the mind reads its config, and mind.toml itself is never written (it holds the owner's names).
"Back to mind.toml" clears the override."""
from dataclasses import replace

from .dials import WORLD_DIALS


def merge(cfg, tuning):
    """`cfg` (a MindConfig read from mind.toml) with the tuning's world overrides; `cfg` itself
    when there are none."""
    if tuning is None or not tuning.world:
        return cfg
    out = cfg
    for name, value in tuning.world.items():
        d = WORLD_DIALS.get(name)
        if d is None:
            continue
        section = getattr(out, d.section)
        out = replace(out, **{d.section: replace(section, **{d.key: value})})
    return out


def file_values(cfg) -> dict[str, int]:
    """mind.toml's own value of each world dial."""
    return {name: getattr(getattr(cfg, d.section), d.key) for name, d in WORLD_DIALS.items()}


def shadowed(cfg, tuning) -> list[tuple[str, int, int]]:
    """(dial, mind.toml's value, the override) for each override that differs from the file:
    the doctor's note."""
    if tuning is None:
        return []
    files = file_values(cfg)
    return [(name, files[name], v) for name, v in sorted(tuning.world.items())
            if name in files and files[name] != v]
