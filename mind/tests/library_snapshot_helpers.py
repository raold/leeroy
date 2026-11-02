"""Synthetic offline reference exports; never reads an installed server or corpus."""
import hashlib
import json


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def snapshot(root, source="acwiki", files=None, *, revision="a" * 40, license="MIT", entries=None):
    root.mkdir(parents=True, exist_ok=True)
    files = {"intro.md": "# Introduction\nA complete reference page.\n"} if files is None else files
    inventory = []
    for name, content in files.items():
        raw = content.encode("utf-8") if isinstance(content, str) else content
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        inventory.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                          "url": "https://www.azerothcore.org/reference/" + name,
                          "language": "en", "archived": False, "version": ""})
    for name, changes in (entries or {}).items():
        next(entry for entry in inventory if entry["path"] == name).update(changes)
    license_path = root / ".github" / "LICENSE"
    license_path.parent.mkdir(parents=True, exist_ok=True)
    license_path.write_text("Synthetic test license evidence.\n", encoding="utf-8")
    evidence = [] if license == "unknown" else [{
        "path": ".github/LICENSE", "sha256": hashlib.sha256(license_path.read_bytes()).hexdigest(),
        "url": "https://example.invalid/license"}]
    manifest = {"format": "ac-library-snapshot-v1", "source": source,
                "origin": "https://github.com/azerothcore/synthetic", "revision": revision,
                "retrieved_at": 1793500000, "license": license, "license_evidence": evidence,
                "complete": True, "errors": [], "expected_files": len(inventory),
                "inventory_sha256": hashlib.sha256(dump(inventory)).hexdigest(), "files": inventory}
    (root / "manifest.json").write_bytes(dump(manifest))
    return manifest


def rewrite(root, manifest, *, rehash=True):
    if rehash:
        manifest["inventory_sha256"] = hashlib.sha256(dump(manifest["files"])).hexdigest()
    (root / "manifest.json").write_bytes(dump(manifest))


def command(**over):
    return {"name": "teleport", "syntax": ".teleport $location", "help": "Go to a named location.\nExact help.",
            "handler": "HandleTeleCommand", "permission": {"kind": "security", "value": 2},
            "allow_console": False, "source_path": "src/cs_tele.cpp", "source_line": 42,
            "verification": "source-observed", "metadata": {"help_source": "command table"}, **over}


def teleport(**over):
    return {"id": 7, "name": "Test Harbor", "map": 0, "position_x": "-1234.5000",
            "position_y": "+42.125000", "position_z": "0.00000001", "orientation": "6.283185",
            "map_name": "Eastern Kingdoms", "metadata": {"table": "game_tele"}, **over}


def server_snapshot(root, *, commands=None, teleports=None, unresolved=None, unmatched=None):
    commands = [command()] if commands is None else commands
    teleports = [teleport()] if teleports is None else teleports
    c = {"format": "ac-command-export-v1", "complete": True, "expected_records": len(commands),
         "coverage": {"verification": "source-observed", "registered": len(commands),
                      "unresolved": unresolved or [], "unmatched_overrides": unmatched or []},
         "records": commands}
    t = {"format": "ac-teleport-export-v1", "complete": True,
         "expected_records": len(teleports), "records": teleports}
    manifest = snapshot(root, "serverref", {"commands.json": dump(c), "teleports.json": dump(t)},
                        license="unknown")
    return manifest, c, t


def update_export(root, manifest, name, value):
    raw = dump(value)
    (root / name).write_bytes(raw)
    entry = next(e for e in manifest["files"] if e["path"] == name)
    entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    rewrite(root, manifest)
