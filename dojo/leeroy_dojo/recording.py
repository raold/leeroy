"""Episode recordings: bounded, checksummed chunks of decision records, a header and a footer.

An episode is a directory:

    header.json            the manifest: scenario, seeds, worker and schema versions, clock and RNG capabilities,
                           bounds and the policies that chose the actions
    chunk-000000.jsonl     up to chunk_records transitions, one JSON line each, then a trailer line
    ...                    {"chunk": {"index", "records", "bytes", "sha256", "prev_chain", "chain"}}
    footer.json            the outcome and every chunk's hash, written last

A chunk is written to a .tmp file and renamed once complete, so a crash leaves either a whole chunk or a visibly
unfinished one. "sha256" covers the record lines; "chain" hashes the previous chunk's chain with this chunk's sha256,
so a missing, reordered or replaced chunk breaks every later one. Only one chunk is held in memory. verify_episode()
names every problem; load_transitions() refuses an episode that is incomplete or aborted, so neither can reach a
learner.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .protocol import encode

FORMAT = 1
CHUNK_RECORDS = 64
CHUNK_BYTES = 1 << 20
GENESIS = "0" * 64
OUTCOMES = ("terminated", "truncated", "aborted")
_CHUNK = re.compile(r"chunk-(\d{6})\.jsonl")
_TMP = re.compile(r"chunk-(\d{6})\.jsonl\.tmp")


class IncompleteEpisode(Exception):
    pass


def decision_record(observations: list[dict[str, Any]], result: dict[str, Any], origin_ms: int,
                    log_probs: list[float] | None = None) -> dict[str, Any]:
    """One transition from the observations a decision answered and the step_result that followed.

    It keeps what the policy saw (observation and collection-time mask), what it requested and its log probability
    when it has one, the action ids execution events refer to, rewards, the next observation and mask, durations and
    termination flags. t_ms is relative to the episode's start.
    """
    actors = []
    for slot, requested in enumerate(result["requested"]):
        before, after = observations[slot], result["observations"][slot]
        reward = result["rewards"][slot]
        actors.append({"actor": requested["actor"], "obs": before["obs"], "mask": before["mask"],
                       "action": requested["action"], "action_id": requested["action_id"],
                       "log_prob": None if log_probs is None else log_probs[slot],
                       "reward": {"terminal": reward["terminal"], "shaping": reward["shaping"]},
                       "next_obs": after["obs"], "next_mask": after["mask"]})
    return {"decision": result["decision"], "request_id": result["id"], "t_ms": result["sim_ms"] - origin_ms,
            "elapsed_ms": result["elapsed_ms"], "substeps": result["substeps"], "actors": actors,
            "events": result["events"], "terminated": result["terminated"], "truncated": result["truncated"],
            "outcome": result["outcome"]}


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _chain(previous: str, digest: str) -> str:
    return hashlib.sha256((previous + digest).encode("ascii")).hexdigest()


class EpisodeRecorder:
    def __init__(self, root: Path, episode: str, header: dict[str, Any], *, chunk_records: int = CHUNK_RECORDS,
                 chunk_bytes: int = CHUNK_BYTES):
        self.dir = Path(root) / episode
        self.dir.mkdir(parents=True, exist_ok=False)
        self.episode = episode
        self.chunk_records = max(chunk_records, 1)
        self.chunk_bytes = max(chunk_bytes, 1)
        self.transitions = 0
        self.chunks: list[dict[str, Any]] = []
        self.max_buffered = 0
        self.finished = False
        self._lines: list[bytes] = []
        self._size = 0
        self._chain = GENESIS
        _write_atomic(self.dir / "header.json", (json.dumps(
            {"kind": "leeroy-dojo-episode", "format": FORMAT, "episode": episode, **header},
            indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    def add(self, transition: dict[str, Any]) -> None:
        if self.finished:
            raise RuntimeError("the episode is finished")
        line = (encode(transition) + "\n").encode("utf-8")
        self._lines.append(line)
        self._size += len(line)
        self.transitions += 1
        self.max_buffered = max(self.max_buffered, len(self._lines))
        if len(self._lines) >= self.chunk_records or self._size >= self.chunk_bytes:
            self._flush()

    def finish(self, outcome: str, **summary: Any) -> Path:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        if self._lines:
            self._flush()
        footer = {"kind": "leeroy-dojo-episode-footer", "format": FORMAT, "episode": self.episode,
                  "outcome": outcome, "usable": outcome != "aborted", "transitions": self.transitions,
                  "chunks": self.chunks, "chain": self._chain, **summary}
        _write_atomic(self.dir / "footer.json", (json.dumps(footer, indent=2, ensure_ascii=False) + "\n").encode())
        self.finished = True
        return self.dir

    def _flush(self) -> None:
        data = b"".join(self._lines)
        index = len(self.chunks)
        digest = hashlib.sha256(data).hexdigest()
        chain = _chain(self._chain, digest)
        trailer = {"chunk": {"index": index, "records": len(self._lines), "bytes": len(data), "sha256": digest,
                             "prev_chain": self._chain, "chain": chain}}
        name = f"chunk-{index:06d}.jsonl"
        _write_atomic(self.dir / name, data + (encode(trailer) + "\n").encode("utf-8"))
        self.chunks.append({"index": index, "file": name, "records": len(self._lines), "sha256": digest})
        self._chain = chain
        self._lines = []
        self._size = 0


@dataclass
class EpisodeCheck:
    path: Path
    complete: bool = False
    usable: bool = False
    outcome: str | None = None
    transitions: int = 0
    valid_chunks: int = 0
    problems: list[str] = field(default_factory=list)


def _read_chunk(path: Path) -> tuple[list[bytes], dict[str, Any] | None]:
    lines = path.read_bytes().splitlines(keepends=True)
    if not lines:
        return [], None
    try:
        trailer = json.loads(lines[-1]).get("chunk")
    except (ValueError, AttributeError):
        return lines, None
    return lines[:-1], trailer if isinstance(trailer, dict) else None


def verify_episode(path: Path) -> EpisodeCheck:
    path = Path(path)
    check = EpisodeCheck(path)
    problems = check.problems
    try:
        header = json.loads((path / "header.json").read_text(encoding="utf-8"))
        if header.get("kind") != "leeroy-dojo-episode" or header.get("format") != FORMAT:
            problems.append("header.json is not a dojo episode header")
    except (OSError, ValueError):
        problems.append("header.json is missing or unreadable")

    names = sorted(p.name for p in path.iterdir()) if path.is_dir() else []
    for name in names:
        if _TMP.fullmatch(name):
            problems.append(f"chunk {int(_TMP.fullmatch(name).group(1))} is incomplete ({name})")
    indices = sorted(int(m.group(1)) for m in (_CHUNK.fullmatch(n) for n in names) if m)

    chain = GENESIS
    decision = 0
    found: dict[int, str] = {}
    broken = False
    for expected, index in enumerate(indices):
        if index != expected:
            problems.append(f"chunk {expected} is missing")
            broken = True
            break
        lines, trailer = _read_chunk(path / f"chunk-{index:06d}.jsonl")
        data = b"".join(lines)
        digest = hashlib.sha256(data).hexdigest()
        if trailer is None:
            problems.append(f"chunk {index} has no trailer")
            broken = True
            break
        if trailer.get("index") != index or trailer.get("records") != len(lines) or trailer.get("bytes") != len(data):
            problems.append(f"chunk {index} does not match its trailer")
            broken = True
            break
        if trailer.get("sha256") != digest:
            problems.append(f"chunk {index} fails its checksum")
            broken = True
            break
        if trailer.get("prev_chain") != chain or trailer.get("chain") != _chain(chain, digest):
            problems.append(f"chunk {index} does not follow chunk {index - 1}")
            broken = True
            break
        for line in lines:
            record = json.loads(line)
            if record.get("decision") != decision:
                problems.append(f"chunk {index} has decision {record.get('decision')} where {decision} belongs")
                broken = True
                break
            decision += 1
        if broken:
            break
        chain = trailer["chain"]
        found[index] = digest
        check.valid_chunks += 1
        check.transitions += len(lines)

    try:
        footer = json.loads((path / "footer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        footer = None
        problems.append("no footer: the episode is incomplete and its final chunks may be missing")
    if footer is not None:
        check.outcome = footer.get("outcome")
        listed = footer.get("chunks", [])
        for entry in listed:
            index = entry.get("index")
            if index not in found:
                if not (path / str(entry.get("file", ""))).exists():
                    problems.append(f"chunk {index} is missing (listed in the footer)")
                elif not broken:
                    problems.append(f"chunk {index} is not readable as listed")
            elif entry.get("sha256") != found[index]:
                problems.append(f"chunk {index} differs from the footer")
        extra = sorted(set(found) - {e.get("index") for e in listed})
        if extra:
            problems.append(f"chunks {extra} are not in the footer")
        if footer.get("transitions") != check.transitions and not problems:
            problems.append("the footer's transition count does not match the chunks")
        if footer.get("chain") != chain and not problems:
            problems.append("the footer's chain does not match the chunks")
        if check.outcome not in OUTCOMES:
            problems.append(f"unknown outcome {check.outcome!r}")
    check.complete = footer is not None and not problems
    check.usable = check.complete and check.outcome in ("terminated", "truncated")
    return check


def load_transitions(path: Path) -> list[dict[str, Any]]:
    """The episode's decision records, only when it is complete and not aborted."""
    check = verify_episode(path)
    if not check.usable:
        why = "; ".join(check.problems) or f"outcome is {check.outcome}"
        raise IncompleteEpisode(f"{path}: {why}")
    records = []
    for index in range(check.valid_chunks):
        lines, _ = _read_chunk(Path(path) / f"chunk-{index:06d}.jsonl")
        records.extend(json.loads(line) for line in lines)
    return records
