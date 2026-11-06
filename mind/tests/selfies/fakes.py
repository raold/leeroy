"""Test doubles for the selfie worker (P2): the fake worker's command, a render request, and a
ready set with its files. Every person here is invented."""
import os
import sys
from dataclasses import replace
from pathlib import Path

from leeroy_mind.selfies import files
from leeroy_mind.selfies.store import (Candidate, ensure_first_sets, mark_ready, queue_more,
                                       queued_sets)
from leeroy_mind.selfies.worker import RenderRequest

from .fake_worker import JPEG  # the bytes the fake writes; the tests take it from here

FAKE_WORKER = Path(__file__).with_name("fake_worker.py")


def tagged_jpeg(tag: str) -> bytes:
    """JPEG with a comment segment naming `tag`: still a valid JPEG, but no two tags give the
    same bytes, so a test can tell which candidate a file was copied from."""
    text = tag.encode("ascii")
    return JPEG[:2] + b"\xff\xfe" + (len(text) + 2).to_bytes(2, "big") + text + JPEG[2:]


def ready(store, data_dir: Path, guid: int, set_no: int, *, now: float = 1.0,
          night: str = "2026-10-29") -> list[Candidate]:
    """Set `set_no` of `guid` queued (a first set when the member has none, else "4 more"),
    then marked ready with four invented candidates, their JPEG files and thumbnails, as a
    night's render leaves it."""
    if (guid, set_no) not in {(s.guid, s.set_no) for s in queued_sets(store)}:
        with store.transaction():
            had = store.conn.execute("SELECT count(*) FROM selfie_set WHERE guid=?",
                                     (guid,)).fetchone()[0]
        if had:
            queue_more(store, guid, now=now)
        else:
            ensure_first_sets(store, [guid], now=now)
    assert (guid, set_no) in {(s.guid, s.set_no) for s in queued_sets(store)}, \
        f"set {set_no} is not the one queued next for {guid}"
    d = files.set_dir(data_dir, guid, set_no)
    d.mkdir(parents=True)
    for i in range(4):
        files.candidate(data_dir, guid, set_no, i).write_bytes(
            tagged_jpeg(f"{guid}/{set_no}/c{i}"))
        files.candidate(data_dir, guid, set_no, i, thumb=True).write_bytes(
            tagged_jpeg(f"{guid}/{set_no}/c{i}.thumb"))
    cands = [Candidate(idx=i, seed=1000 * set_no + i, tries=1, nsfw=0.01,
                       prompt=f"photo of an invented test person, set {set_no}, slot {i}")
             for i in range(4)]
    mark_ready(store, guid, set_no, cands, now=now, night=night,
               persona_version=store.persona_versions().get(guid))
    return cands


def fake_command(tmp_path: Path, **env: str) -> tuple[list[str], dict[str, str]]:
    """The fake worker under this interpreter, isolated as the real one runs (on Windows a venv
    launcher that starts the base interpreter as its child), and its environment: this
    process's, plus the FAKE_SELFIE_* behaviour given."""
    return ([sys.executable, "-I", str(FAKE_WORKER)],
            {**os.environ, "LEEROY_SELFIE_DATA": str(tmp_path / "selfie-data"), **env})


def request(tmp_path: Path, **over) -> RenderRequest:
    req = RenderRequest(
        phrases=(("photo of a woman in her thirties", 0), ("short auburn hair", 1),
                 ("standing in a sunlit kitchen", 3)),
        negative=(("child, teenager", 0), ("blurry", 1)),
        seed=1234, steps=30, cfg=5.0, size=1024, out=tmp_path / "c0.jpg",
        thumb=tmp_path / "c0.thumb.jpg", thumb_size=512, nsfw_max=0.2)
    return replace(req, **over)
