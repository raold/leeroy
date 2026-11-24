"""Read only named boolean settings from the installed module files.

Configured values are never reported as running state: reloads, alternate config
paths, environment overrides and a different installed build cannot be proven by
reading a file. No native commands, environment reads or process access here.
"""
from pathlib import Path

from .native_switch_keys import LEEROY, PLAYERBOTS


FILES = (("mod_leeroy.conf", LEEROY), ("playerbots.conf", PLAYERBOTS))
NOTE = ("Native rows show configured values in the installed module files. Running state is unknown; "
        "a file edit does not prove a reload or restart applied it. Confirm supported reloads and "
        "a fresh native greeting, or apply the setting at the next approved native restart. "
        "Missing keys use the installed build's defaults or other overrides, which are not inferred here.")


def values(text, keys):
    """Mirror the core's first-assignment and quote removal; never return raw values."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")) or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        if key not in keys or key in result:
            continue
        token = raw.strip().replace('"', '').lower()
        result[key] = ("on" if token in {"1", "true", "yes", "y", "on"} else
                       "off" if token in {"0", "false", "no", "n", "off"} else "invalid")
    return result


def view(root: Path, *, read, check):
    directory = root / "build" / "bin" / "RelWithDebInfo" / "configs" / "modules"
    sources = []
    for filename, keys in FILES:
        check()
        path = directory / filename
        states, status = {}, "ok"
        try:
            for parent in (path, *path.parents):
                if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                    raise OSError("linked source")
                if parent == root:
                    break
            states = values(read(path, 512 * 1024).decode("utf-8-sig"), keys)
        except FileNotFoundError:
            status = "missing"
        except (OSError, UnicodeError):
            status = "error"
        # Budget exceptions belong to the existing Ops deadline/size handler.
        sources.append(dict(source=f"build/bin/RelWithDebInfo/configs/modules/{filename}", status=status,
            rows=[dict(key=key, configured=states.get(key, "unset" if status == "ok" else "unknown"),
                       effective="unknown") for key in sorted(keys)]))
    return dict(note=NOTE, sources=sources)
