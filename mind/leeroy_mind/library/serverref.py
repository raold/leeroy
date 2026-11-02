"""Installed command and game_tele references from complete SELECT/source exports.

This adapter does not discover handlers or execute GM commands. Export coverage is
stored verbatim so source-observed registrations never become runtime verification.
Coordinates are decimal strings throughout; map names do not imply zone names.
"""
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from .chunks import ChunkDraft, CommandRow, SourceResult, TeleportRow
from .snapshots import OfflineSnapshot, _integer, _string, read_json

# ChatCommand's canonical path delimiter is ASCII space, not an alnum grammar.
# Punctuation in a genuinely registered node must not invalidate a whole export.
_COMMAND = re.compile(r"[^\s\x00-\x1f\x7f]+(?: [^\s\x00-\x1f\x7f]+)*")
_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_VERIFIED = {"source-observed", "runtime-verified"}


def _records(raw: bytes, format_name: str) -> tuple[dict, list[dict]]:
    doc = read_json(raw)
    if (not isinstance(doc, dict) or doc.get("format") != format_name
            or doc.get("complete") is not True):
        raise ValueError("incomplete reference export")
    records = doc.get("records")
    expected = _integer(doc.get("expected_records"), "expected_records")
    if not isinstance(records, list) or len(records) != expected:
        raise ValueError("incomplete reference records")
    if not all(isinstance(r, dict) for r in records):
        raise ValueError("invalid reference record")
    return doc, records


def _metadata(record: dict) -> dict:
    value = record.get("metadata", {})
    if not isinstance(value, dict):
        raise ValueError("invalid reference metadata")
    return value


def _command(record: dict) -> CommandRow:
    name = _string(record.get("name"), "command name")
    if not _COMMAND.fullmatch(name):
        raise ValueError("command name must be a canonical registered path")
    permission = record.get("permission")
    if not isinstance(permission, dict) or permission.get("kind") not in {"security", "rbac", "unknown"}:
        raise ValueError("invalid command permission")
    value = permission.get("value")
    if permission["kind"] == "unknown":
        if value is not None:
            raise ValueError("unknown permission must not invent a value")
    elif not ((type(value) is int and value >= 0) or (isinstance(value, str) and value.strip())):
        raise ValueError("invalid command permission value")
    console = record.get("allow_console")
    if console is not None and type(console) is not bool:
        raise ValueError("invalid console availability")
    path = _string(record.get("source_path"), "command source path")
    if "\\" in path or ":" in path or PurePosixPath(path).is_absolute() or ".." in path.split("/"):
        raise ValueError("command source path must be relative")
    line = record.get("source_line")
    if line is not None:
        _integer(line, "source line", minimum=1)
    verification = record.get("verification")
    if verification not in _VERIFIED:
        raise ValueError("invalid command verification")
    return CommandRow(name, _string(record.get("syntax"), "syntax", empty=True),
                      _string(record.get("help"), "help", empty=True),
                      _string(record.get("handler"), "handler", empty=True),
                      permission, console, path, line, verification, _metadata(record))


def _coordinate(value) -> str:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise ValueError("coordinates require exact decimal strings")
    try:
        if not Decimal(value).is_finite():
            raise ValueError("nonfinite coordinate")
    except InvalidOperation as exc:
        raise ValueError("invalid coordinate") from exc
    return value


def _teleport(record: dict) -> TeleportRow:
    ident = _integer(record.get("id"), "teleport id", minimum=1)
    map_id = _integer(record.get("map"), "map id")
    if ident > 2**32 - 1 or map_id > 2**32 - 1:
        raise ValueError("teleport identifier exceeds unsigned range")
    return TeleportRow(ident, _string(record.get("name"), "teleport name"), map_id,
                       *(_coordinate(record.get(k)) for k in
                         ("position_x", "position_y", "position_z", "orientation")),
                       _string(record.get("map_name", ""), "map name", empty=True), _metadata(record))


class ServerRefSource:
    name = "serverref"

    def __init__(self, root: Path):
        self.root = Path(root)

    def load(self, library) -> SourceResult:
        snapshot = OfflineSnapshot(self.root, self.name)
        files = {f.path: f for f in snapshot.files}
        command_doc, raw_commands = _records(snapshot.read(files["commands.json"]), "ac-command-export-v1")
        _, raw_teleports = _records(snapshot.read(files["teleports.json"]), "ac-teleport-export-v1")
        coverage = command_doc.get("coverage")
        if (not isinstance(coverage, dict) or coverage.get("verification") not in _VERIFIED
                or _integer(coverage.get("registered"), "registered count") != len(raw_commands)
                or not isinstance(coverage.get("unresolved"), list)
                or not isinstance(coverage.get("unmatched_overrides"), list)):
            raise ValueError("command export requires explicit coverage")
        commands = [_command(r) for r in raw_commands]
        teleports = [_teleport(r) for r in raw_teleports]
        if len({r.name.casefold() for r in commands}) != len(commands):
            raise ValueError("duplicate registered command")
        if len({r.id for r in teleports}) != len(teleports):
            raise ValueError("duplicate teleport id")
        if coverage["verification"] == "runtime-verified" and (
                coverage["unresolved"] or any(r.verification != "runtime-verified" for r in commands)):
            raise ValueError("runtime coverage contradicts observed records")
        result = SourceResult(self.name, commands=commands, teleports=teleports)

        def draft(path, key, kind, title, text, refs, metadata, *, map_id=None):
            file = files[path]
            return ChunkDraft(key, kind, title, text, url=file.url, license=snapshot.provenance.license,
                              map_id=map_id, ref_ids=refs,
                              source_revision=snapshot.provenance.revision, source_path=path,
                              source_sha256=file.sha256,
                              metadata={**metadata, "reference_only": True, "game_version": "3.3.5",
                                        "snapshot_manifest_sha256": snapshot.provenance.manifest_sha256})

        for row in commands:
            permission = row.permission
            value = permission.get("value")
            permission_text = "unknown" if permission["kind"] == "unknown" else f"{permission['kind']}: {value}"
            console = "unknown" if row.allow_console is None else ("yes" if row.allow_console else "no")
            text = (f"Registered command: .{row.name}\n"
                    f"Syntax: {row.syntax or '(not resolved)'}\n"
                    f"Help: {row.help or '(not resolved)'}\n"
                    f"Permission: {permission_text}\nConsole: {console}\n"
                    f"Handler: {row.handler or '(command namespace)'}\n"
                    f"Verification: {row.verification}\nSource: {row.source_path}"
                    + (f":{row.source_line}" if row.source_line is not None else ""))
            for label, field in (("Target", "target"), ("Target selection", "target_selection"),
                                 ("Effects", "effects"), ("Arguments", "arguments")):
                if isinstance(row.metadata.get(field), str) and row.metadata[field]:
                    text += f"\n{label}: {row.metadata[field]}"
            result.chunks.append(draft(
                "commands.json", f"command:{row.name}", "command", f".{row.name}", text,
                (f"command:{row.name}",), {**row.metadata, "name": row.name, "syntax": row.syntax,
                                          "help": row.help, "handler": row.handler,
                                          "verification": row.verification, "permission": permission,
                                          "allow_console": row.allow_console, "source_path": row.source_path,
                                          "source_line": row.source_line}))
        for row in teleports:
            text = (f"Named teleport destination: {row.name}\nID: {row.id}\nMap ID: {row.map}\n"
                    + (f"Map name: {row.map_name}\n" if row.map_name else "")
                    + f"World coordinates: x={row.position_x}, y={row.position_y}, z={row.position_z}\n"
                    f"Orientation: {row.orientation}\n"
                    "These are world coordinates, not zone-map percentages.\n"
                    "This is a game_tele destination record; command availability is listed separately.")
            result.chunks.append(draft(
                "teleports.json", f"teleport:{row.id}", "teleport", row.name, text,
                (f"teleport:{row.id}",), {**row.metadata, "id": row.id, "name": row.name,
                                         "map": row.map, "map_name": row.map_name,
                                         "position_x": row.position_x, "position_y": row.position_y,
                                         "position_z": row.position_z, "orientation": row.orientation},
                map_id=row.map))
        counts = {"commands.json": len(commands), "teleports.json": len(teleports)}
        inventory = [replace(f, chunks=counts.get(f.path, 0),
                             status=("imported" if counts.get(f.path) else "empty")
                             if f.path in counts else "evidence",
                             note="no records" if f.path in counts and not counts[f.path] else "")
                     for f in snapshot.files]
        result.provenance = snapshot.finish(inventory, coverage=coverage)
        result.log.append(f"{len(commands)} commands ({coverage['verification']}; "
                          f"{len(coverage['unresolved'])} unresolved); {len(teleports)} teleports")
        return result
