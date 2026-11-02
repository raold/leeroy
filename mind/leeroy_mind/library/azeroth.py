"""All pages of pinned official AzerothCore wiki and generated API snapshots.

No web requests, lore-era cutoff, language filter, or page cap. Code and table
lines remain intact; upstream instructions are reference text, never executed.
"""
import hashlib
import re
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from html.parser import HTMLParser
import multiprocessing
from pathlib import Path
from time import perf_counter

from .chunks import ChunkDraft, SourceFile, SourceResult
from .snapshots import (DEFAULT_SNAPSHOT_WORKERS, OfflineSnapshot, _bounded_executor_map, _bounded_map,
                        snapshot_parse_processes, snapshot_workers)

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_WORDS = 240


class _HTMLText(HTMLParser):
    """Keep headings, table cells and preformatted source; omit page chrome/scripts."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.title = []
        self.head = False
        self.in_title = False
        self.ignored: list[str] = []
        self.heading = 0
        self.pre = False

    def handle_starttag(self, tag, attrs):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.append(tag)
            return
        if tag in {"script", "style", "nav", "noscript"}:
            self.ignored.append(tag)
            return
        if tag == "head":
            self.head = True
        elif tag == "title":
            self.in_title = True
        elif not self.head:
            if re.fullmatch(r"h[1-6]", tag):
                self.heading = int(tag[1])
                self.parts.append("\n\n" + "#" * self.heading + " ")
            elif tag == "pre":
                self.pre = True
                self.parts.append("\n\n```\n")
            elif tag in {"p", "div", "section", "article", "dl", "dt", "dd", "ul", "ol", "li", "tr"}:
                self.parts.append("\n")
            elif tag == "br":
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append("\t")

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag == "head":
            self.head = False
        elif tag == "title":
            self.in_title = False
        elif not self.head:
            if tag == "pre":
                self.parts.append("\n```\n\n")
                self.pre = False
            elif re.fullmatch(r"h[1-6]", tag):
                self.parts.append("\n\n")
                self.heading = 0
            elif tag in {"p", "div", "section", "article", "dl", "dt", "dd", "li", "tr"}:
                self.parts.append("\n")

    def handle_data(self, data):
        if self.ignored:
            return
        if self.in_title:
            self.title.append(data)
        elif not self.head:
            self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))


def _markdown(text: str, fallback: str) -> tuple[str, list[tuple[str, str]]]:
    """A conservative heading parser; fenced blocks never become headings."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    title = fallback
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
        if end is not None:
            for line in lines[1:end]:
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip("\"'") or title
            lines = lines[end + 1:]
    sections = []
    label, current, fence = "", [], ""
    found_title = title != fallback
    for line in lines:
        f = _FENCE.match(line)
        if f:
            if not fence:
                fence = f.group(1)
            elif f.group(1)[0] == fence[0] and len(f.group(1)) >= len(fence):
                fence = ""
        heading = _HEADING.match(line) if not fence and not f else None
        if heading:
            if current:
                sections.append((label, "\n".join(current).strip()))
            label, current = heading.group(2), []
            if len(heading.group(1)) == 1 and not found_title:
                title, found_title = label, True
        else:
            current.append(line)
    if current:
        sections.append((label, "\n".join(current).strip()))
    # Heading-only pages are part of the corpus too, not a silently omitted file.
    if not any(body.strip() for _, body in sections) and found_title:
        sections = [("", title)]
    return title, sections


def _pieces(text: str):
    """Bound by words where line boundaries permit it; retain exact syntax on each line."""
    current, words, fence = [], 0, ""
    for line in text.splitlines():
        n = len(line.split())
        if current and words + n > _WORDS:
            body = "\n".join(current).strip()
            if body:
                yield body + ("\n" + fence if fence else "")
            current, words = ([fence] if fence else []), 0
        current.append(line)
        words += n
        f = _FENCE.match(line)
        if f:
            if not fence:
                fence = f.group(1)
            elif f.group(1)[0] == fence[0] and len(f.group(1)) >= len(fence):
                fence = ""
    body = "\n".join(current).strip()
    if body:
        yield body + ("\n" + fence if fence else "")


@dataclass(frozen=True, slots=True)
class _Document:
    """Only verified bytes and immutable provenance cross the process boundary."""
    raw: bytes
    name: str
    file: SourceFile
    license: str
    revision: str


def _parse_document(document: _Document) -> tuple[list[ChunkDraft], SourceFile]:
    """Pure CPU work; module-level entry point is importable by Windows spawn."""
    file, name = document.file, document.name
    text = document.raw.decode("utf-8-sig")
    fallback = Path(file.path).stem.replace("-", " ").replace("_", " ")
    if name == "acdoxygen":
        parser = _HTMLText()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
        fallback = "".join(parser.title).strip() or fallback
    title, sections = _markdown(text, fallback)
    chunks = []
    seen: dict[str, int] = {}
    for section, body in sections:
        for piece in _pieces(body):
            digest = hashlib.sha256((section + "\x00" + piece).encode("utf-8")).hexdigest()
            occurrence = seen.get(digest, 0)
            seen[digest] = occurrence + 1
            chunks.append(ChunkDraft(
                f"{name}:{file.path}:{digest}:{occurrence}",
                "documentation" if name == "acwiki" else "api", title, piece,
                section=section, url=file.url,
                license=document.license,
                ref_ids=(f"{name}:{file.path}",),
                source_revision=document.revision,
                source_path=file.path, source_sha256=file.sha256,
                metadata={"language": file.language, "archived": file.archived,
                          "version": file.version, "game_version": "3.3.5",
                          "reference_only": True}))
    added = len(chunks)
    return chunks, replace(file, chunks=added, status="imported" if added else "empty",
                           note="" if added else "no visible document text")


class AzerothDocsSource:
    def __init__(self, root: Path, *, name: str = "acwiki", workers: int = DEFAULT_SNAPSHOT_WORKERS,
                 parse_processes: int = 0, progress: Callable[[str], None] | None = None):
        """``parse_processes=0`` retains threaded parsing; opt-in CPU workers use spawn.

        Application scripts enabling processes must guard their entry point with
        ``if __name__ == '__main__'``. Library construction never starts a pool.
        """
        if name not in {"acwiki", "acdoxygen"}:
            raise ValueError("documentation source must be acwiki or acdoxygen")
        self.root = Path(root)
        self.name = name
        self.workers = snapshot_workers(workers)
        self.parse_processes = snapshot_parse_processes(parse_processes)
        self.progress = progress

    def load(self, library) -> SourceResult:
        snapshot = OfflineSnapshot(self.root, self.name, workers=self.workers, progress=self.progress)
        chunks = []
        inventory = []
        total = len(snapshot.files)
        started = perf_counter()
        mode = (f"{self.parse_processes} processes ({self.workers} read workers)" if self.parse_processes
                else f"{self.workers} workers")
        snapshot.notify(f"{self.name}: parsing {total} files with {mode}")
        results = (self._process_files(snapshot) if self.parse_processes else
                   _bounded_map(lambda file: self._load_file(snapshot, file), snapshot.files, workers=self.workers))
        try:
            for count, (file_chunks, status) in enumerate(results, 1):
                chunks.extend(file_chunks)
                inventory.append(status)
                if count == 1 or count % 256 == 0 or count == total:
                    snapshot.notify(f"{self.name}: parsed {count}/{total} files ({len(chunks)} chunks)")
        finally:
            results.close()
        snapshot.notify(f"{self.name}: parsing complete in {perf_counter() - started:.1f}s")
        result = SourceResult(self.name, chunks=chunks, provenance=snapshot.finish(inventory))
        result.log.append(f"verified {len(inventory)} files; {sum(f.status == 'empty' for f in inventory)} empty")
        return result

    def _read_document(self, snapshot: OfflineSnapshot, file: SourceFile) -> _Document:
        # The second safe path/read/hash pass stays in this process's IO threads.
        return _Document(snapshot.read(file), self.name, file,
                         snapshot.provenance.license,
                         snapshot.provenance.revision)

    def _load_file(self, snapshot: OfflineSnapshot, file: SourceFile) -> tuple[list[ChunkDraft], SourceFile]:
        return _parse_document(self._read_document(snapshot, file))

    def _process_files(self, snapshot: OfflineSnapshot):
        reads = _bounded_map(lambda file: self._read_document(snapshot, file), snapshot.files, workers=self.workers)
        # Explicit spawn also on Unix: never inherit the caller's SQLite handles,
        # sockets or model clients. Neither executor receives the Library object.
        executor = ProcessPoolExecutor(max_workers=self.parse_processes,
                                       mp_context=multiprocessing.get_context("spawn"))
        results = _bounded_executor_map(executor, _parse_document, reads, limit=self.parse_processes * 2)
        try:
            yield from results
        finally:
            try:
                results.close()
            finally:
                try:
                    reads.close()
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)


class AzerothWikiSource(AzerothDocsSource):
    def __init__(self, root: Path, *, workers: int = DEFAULT_SNAPSHOT_WORKERS,
                 parse_processes: int = 0, progress: Callable[[str], None] | None = None):
        super().__init__(root, name="acwiki", workers=workers, parse_processes=parse_processes, progress=progress)


class AzerothDoxygenSource(AzerothDocsSource):
    def __init__(self, root: Path, *, workers: int = DEFAULT_SNAPSHOT_WORKERS,
                 parse_processes: int = 0, progress: Callable[[str], None] | None = None):
        super().__init__(root, name="acdoxygen", workers=workers, parse_processes=parse_processes, progress=progress)
