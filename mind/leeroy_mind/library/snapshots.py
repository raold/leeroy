"""Validated, offline reference snapshots. Acquisition is deliberately outside the library.

The inventory is an acquisition claim about a pinned upstream revision, not proof that
an upstream crawler was exhaustive. We verify that claim against every local source
file and retain it for audit. A partial or corrupt snapshot never becomes SourceResult.
"""
import hashlib
import json
import re
import stat
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import TypeVar

from .chunks import SNAPSHOT_SOURCES, SnapshotProvenance, SourceFile

FORMAT = "ac-library-snapshot-v1"
_HASH = re.compile(r"[0-9a-f]{64}")
DEFAULT_SNAPSHOT_WORKERS = 8
MAX_SNAPSHOT_WORKERS = 16
MAX_SNAPSHOT_PARSE_PROCESSES = 16
_PROGRESS_EVERY = 256
_T = TypeVar("_T")
_R = TypeVar("_R")


def snapshot_workers(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_SNAPSHOT_WORKERS:
        raise ValueError(f"snapshot workers must be an integer from 1 to {MAX_SNAPSHOT_WORKERS}")
    return value


def snapshot_parse_processes(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SNAPSHOT_PARSE_PROCESSES:
        raise ValueError(f"snapshot parse processes must be an integer from 0 to {MAX_SNAPSHOT_PARSE_PROCESSES}")
    return value


def _bounded_executor_map(executor: Executor, function: Callable[[_T], _R], values: Iterable[_T],
                          *, limit: int) -> Iterator[_R]:
    """Borrow an executor; bound pending inputs/results and retain input order."""
    iterator = iter(values)
    pending = deque()
    try:
        for _ in range(limit):
            try:
                value = next(iterator)
            except StopIteration:
                break
            pending.append(executor.submit(function, value))
        while pending:
            result = pending.popleft().result()
            yield result
            try:
                value = next(iterator)
            except StopIteration:
                continue
            pending.append(executor.submit(function, value))
    finally:
        for future in pending:
            future.cancel()


def _bounded_map(function: Callable[[_T], _R], values: Iterable[_T], *, workers: int) -> Iterator[_R]:
    """Ordered results with at most 2*workers futures, including completed ones.

    A manual window works on older tooling Pythons too, without relying on the
    Python 3.14 Executor.map buffersize argument. Workers never receive a database.
    On any failure/cancellation, queued work is cancelled and running work joined.
    """
    workers = snapshot_workers(workers)
    if workers == 1:
        for value in values:
            yield function(value)
        return
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="library-snapshot")
    results = _bounded_executor_map(executor, function, values, limit=workers * 2)
    try:
        yield from results
    finally:
        try:
            results.close()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)


def _relative_parts(relative: str) -> tuple[str, ...]:
    p = PurePosixPath(relative)
    if ("\\" in relative or ":" in relative or p.is_absolute()
            or any(part in ("", ".", "..", ".git") for part in relative.split("/"))):
        raise ValueError("unsafe snapshot path")
    return p.parts


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def read_json(raw: bytes):
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(
                          ValueError(f"invalid JSON constant {value}")))


def _string(value, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or "\x00" in value:
        raise ValueError(f"invalid {label}")
    # Refuse unencodable escapes instead of silently altering source evidence.
    value.encode("utf-8")
    return value


def _integer(value, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid {label}")
    return value


class OfflineSnapshot:
    def __init__(self, root: Path, source: str, *, workers: int = DEFAULT_SNAPSHOT_WORKERS,
                 progress: Callable[[str], None] | None = None):
        self.workers = snapshot_workers(workers)
        self.progress = progress
        self.root = Path(root).resolve(strict=True)
        if source not in SNAPSHOT_SOURCES:
            raise ValueError("not a snapshot source")
        raw = (self.root / "manifest.json").read_bytes()
        m = read_json(raw)
        if (not isinstance(m, dict) or m.get("format") != FORMAT or m.get("source") != source
                or m.get("complete") is not True or m.get("errors") != []):
            raise ValueError("incomplete or incompatible snapshot manifest")
        self.manifest = m
        self.source = source
        origin = _string(m.get("origin"), "origin")
        revision = _string(m.get("revision"), "revision")
        if revision.lower() in {"latest", "head", "main", "master", "unknown"}:
            raise ValueError("snapshot revision is not pinned")
        license_name = _string(m.get("license"), "license")
        retrieved = _integer(m.get("retrieved_at"), "retrieved_at")
        entries = m.get("files")
        if not isinstance(entries, list) or not entries:
            raise ValueError("snapshot has no file inventory")
        expected = _integer(m.get("expected_files"), "expected_files", minimum=1)
        if expected != len(entries):
            raise ValueError("incomplete file inventory")
        inventory_hash = hashlib.sha256(canonical_json(entries)).hexdigest()
        if inventory_hash != m.get("inventory_sha256"):
            raise ValueError("inventory hash mismatch")
        self.notify(f"{source}: validating inventory ({expected} files)")
        files = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid inventory entry")
            path = _string(entry.get("path"), "inventory path")
            # Metadata/lexical validation precedes all worker activity. Full path
            # and link validation remains inside every actual verified read.
            _relative_parts(path)
            digest = _string(entry.get("sha256"), "file hash")
            if not _HASH.fullmatch(digest):
                raise ValueError("invalid file hash")
            size = _integer(entry.get("bytes"), "file bytes")
            url = _string(entry.get("url"), "file URL")
            language = _string(entry.get("language", "und"), "language")
            archived = entry.get("archived", False)
            if type(archived) is not bool:
                raise ValueError("invalid archive marker")
            version = _string(entry.get("version", ""), "version", empty=True)
            files.append(SourceFile(path, digest, size, url, language, archived, version))
        if len({f.path.casefold() for f in files}) != len(files):
            raise ValueError("duplicate inventory paths")
        self.files = tuple(files)
        self.entries = {e["path"]: e for e in entries}
        expected_paths = {f.path for f in files}
        if source != "serverref":
            self.notify(f"{source}: checking documentation file coverage")
            suffixes = {".md", ".markdown"} if source == "acwiki" else {".html", ".htm"}
            actual = {p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                      if ".git" not in p.relative_to(self.root).parts
                      and p.suffix.lower() in suffixes and p.is_file()}
            if actual != expected_paths:
                raise ValueError("inventory does not cover every documentation file")
        elif not {"commands.json", "teleports.json"} <= expected_paths:
            raise ValueError("server reference export is incomplete")
        started = perf_counter()
        self.notify(f"{source}: verifying {expected} files with {self.workers} workers")
        verified = _bounded_map(self._verify, self.files, workers=self.workers)
        try:
            for count, _ in enumerate(verified, 1):
                if count == 1 or count % _PROGRESS_EVERY == 0 or count == expected:
                    self.notify(f"{source}: verified {count}/{expected} files")
        finally:
            verified.close()
        self.notify(f"{source}: verification complete in {perf_counter() - started:.1f}s")
        evidence = m.get("license_evidence")
        if not isinstance(evidence, list) or (not evidence and license_name.lower() != "unknown"):
            raise ValueError("license requires observed evidence or explicit unknown")
        for item in evidence:
            if not isinstance(item, dict):
                raise ValueError("invalid license evidence")
            path = self.path(_string(item.get("path"), "license path"))
            if hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256"):
                raise ValueError("license evidence hash mismatch")
            _string(item.get("url"), "license URL")
        self.provenance = SnapshotProvenance(
            origin, revision, retrieved, license_name, hashlib.sha256(raw).hexdigest(),
            inventory_hash, expected, self.files, license_evidence=tuple(evidence))
        self.provenance.validate(require_processed=False)

    def notify(self, message: str) -> None:
        # Call only from the coordinator thread, never from read/parse workers.
        if self.progress is not None:
            self.progress(message)

    def path(self, relative: str) -> Path:
        parts = _relative_parts(relative)
        path = self.root.joinpath(*parts)
        # Reject links rather than making the pinned inventory depend on an external tree.
        # lstat covers Windows junction/reparse points as well as ordinary symlinks.
        for n in range(1, len(parts) + 1):
            info = self.root.joinpath(*parts[:n]).lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise ValueError("linked snapshot path")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise ValueError("snapshot path is not a contained file")
        return resolved

    def read(self, entry: SourceFile) -> bytes:
        raw = self.path(entry.path).read_bytes()
        if len(raw) != entry.bytes or hashlib.sha256(raw).hexdigest() != entry.sha256:
            raise ValueError(f"snapshot file changed or is corrupt: {entry.path}")
        return raw

    def _verify(self, entry: SourceFile) -> None:
        self.read(entry)  # Do not retain raw pages in verification futures.

    def finish(self, files: list[SourceFile], *, coverage: dict | None = None) -> SnapshotProvenance:
        p = replace(self.provenance, files=tuple(files), coverage=coverage or {})
        p.validate()
        return p
