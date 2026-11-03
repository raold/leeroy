"""Voice specs: mind-data/voices/<guid>.json, one recipe per member."""
import json
import logging
import os
import tempfile
from pathlib import Path

from leeroy_mind.logs import log as event
from leeroy_mind.voice import wire

log = logging.getLogger(__name__)

SPEC_WATCH_S = 30.0  # how often SpecWatch looks at the spec files
BRIEF_KEYS = ("guid", "name", "gender", "accent", "age", "descriptors", "speed", "guild_role",
              "raid_role", "core")  # all the casting may know of a member; nothing private


def load_specs(data_dir: Path) -> dict[int, dict]:
    """Every voices/<guid>.json that parses as an object whose integer `guid` matches its file
    name. Others (not JSON, not an object, another guid, brief.json) are logged and skipped."""
    folder = Path(data_dir) / "voices"
    if not folder.is_dir():
        return {}
    specs: dict[int, dict] = {}
    for path in sorted(folder.glob("*.json")):
        if not path.stem.isdigit():
            continue
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("voice_spec_skipped: file=%s reason=unreadable", path.name)
            continue
        guid = spec.get("guid") if isinstance(spec, dict) else None
        if type(guid) is not int or guid != int(path.stem):
            log.warning("voice_spec_skipped: file=%s reason=guid_mismatch", path.name)
            continue
        specs[guid] = spec
    return specs


def brief_entries(store) -> list[dict]:
    """One BRIEF_KEYS object per member of the cast who has a persona, by guid. The cast is the
    bridge's (store.cast()), so a persona row of someone who left gets no voice. Plain reads."""
    from leeroy_mind.personas.io import load_persona
    out = []
    for guid, member in sorted(store.cast().items()):
        p = load_persona(store, guid)
        if p is None:
            continue
        out.append({"guid": guid, "name": member.name, "gender": p.identity.player_gender,
                    "accent": p.identity.accent, "age": p.identity.age,
                    "descriptors": list(p.identity.voice.base),
                    "speed": float(p.identity.voice.speed), "guild_role": p.play.guild_role,
                    "raid_role": p.play.raid_role, "core": p.core})
    return out


def write_entries(entries: list[dict], out_dir: Path) -> Path:
    """<out_dir>/brief.json, whole or not at all."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "brief.json"
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".brief-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def write_brief(store, out_dir: Path) -> Path:
    """What the casting reads (Task 19): the cast's members and what their voices turn on."""
    return write_entries(brief_entries(store), out_dir)


class SpecWatch:
    """A ticker of the voice extension: every 30 s, a spec file that is new or whose mtime or
    size changed is loaded into ext.specs and its voice sent again, so a recast or a lock
    reaches the service while the mind runs and a member added tonight can be rolled on comms
    from the next session. The first look only records what is there. A failure is logged by
    type and tried again at the next look; it never reaches the tick loop."""

    def __init__(self, ext):
        self.ext = ext
        self._seen: dict[int, tuple[int, int]] | None = None
        self._due = 0.0

    def __call__(self) -> None:
        now = self.ext.app.clock.now()
        if self._seen is not None and now < self._due:
            return
        self._due = now + SPEC_WATCH_S
        try:
            self._look()
        except Exception as exc:
            event("voice_spec_watch_failed", level=logging.WARNING, reason=type(exc).__name__)

    def _look(self) -> None:
        folder = Path(self.ext.app.data_dir) / "voices"
        stats: dict[int, tuple[int, int]] = {}
        if folder.is_dir():
            for path in folder.glob("*.json"):
                if not path.stem.isdigit():
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                stats[int(path.stem)] = (st.st_mtime_ns, st.st_size)
        if self._seen is None:
            self._seen = stats
            return
        changed = sorted(g for g, st in stats.items() if self._seen.get(g) != st)
        if changed:
            specs = load_specs(self.ext.app.data_dir)  # may raise: _seen stays and we retry
            for guid in changed:
                spec = specs.get(guid)
                if spec is None:
                    continue
                self.ext.specs[guid] = spec
                self.ext.send(wire.VoiceDef(guid, str(spec.get("name") or guid), spec))
        self._seen = stats
