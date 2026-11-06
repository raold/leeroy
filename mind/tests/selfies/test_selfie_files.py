"""Profile photo files under mind-data/selfies/<guid>/ (P2 Task 8): paths built from integers
only, chosen.jpg replaced only through a temporary file in its own folder and a rename (retried
while a reader holds it), and rebuilt from the pick log after a crash. Synthetic bytes only."""
import os
import threading
import time
from pathlib import Path

import pytest

from leeroy_mind.selfies import files
from leeroy_mind.selfies.files import (candidate, chosen, clear_stale, history, member_dir,
                                       replace_atomic, set_dir, staging_dir, sync_chosen)
from leeroy_mind.selfies.store import Current, current, pick, undo

from .fakes import JPEG, ready


def test_sync_rebuilds_chosen_from_the_pick_log(tmp_path, store):
    ready(store, tmp_path, 101, 1)          # helper: mark_ready plus four candidate files
    n = pick(store, 101, 1, 2, source="console", now=2.0)   # crash: committed, file never written
    assert sync_chosen(tmp_path, 101, current(store, 101)) is True
    assert chosen(tmp_path, 101).read_bytes() == candidate(tmp_path, 101, 1, 2).read_bytes()
    assert history(tmp_path, 101, n).exists()
    undo(store, n, source="console", now=3.0)
    assert sync_chosen(tmp_path, 101, current(store, 101)) is True and not chosen(tmp_path, 101).exists()


def test_replace_retries_while_a_reader_holds_the_file(tmp_path, monkeypatch):
    dst = tmp_path / "chosen.jpg"; dst.write_bytes(b"old"); src = tmp_path / "new.jpg"; src.write_bytes(JPEG)
    calls = {"n": 0}; real = os.replace
    def busy(a, b):
        calls["n"] += 1
        if calls["n"] <= 2:
            assert dst.read_bytes() == b"old"   # never missing while it waits
            raise PermissionError(32, "in use")
        real(a, b)
    monkeypatch.setattr(os, "replace", busy)
    replace_atomic(src, dst)
    assert dst.read_bytes() == JPEG and calls["n"] == 3 and not list(tmp_path.glob(".chosen.jpg.*"))


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to replace a file held open")
def test_replace_waits_for_a_real_reader_to_let_go(tmp_path):
    dst = tmp_path / "chosen.jpg"; dst.write_bytes(b"old"); src = tmp_path / "new.jpg"; src.write_bytes(JPEG)
    reader = open(dst, "rb")  # as the console's face endpoint holds it while it sends it
    let_go = threading.Timer(0.3, reader.close)
    start = time.monotonic()
    let_go.start()
    try:
        replace_atomic(src, dst)
    finally:
        let_go.join()
        reader.close()
    assert dst.read_bytes() == JPEG and time.monotonic() - start >= 0.25
    assert not list(tmp_path.glob(".chosen.jpg.*"))


def test_replace_gives_up_after_its_tries_and_leaves_the_old_file(tmp_path, monkeypatch):
    dst = tmp_path / "chosen.jpg"; dst.write_bytes(b"old"); src = tmp_path / "new.jpg"; src.write_bytes(JPEG)
    calls = []
    def held(a, b):
        calls.append(a)
        raise PermissionError(32, "in use")
    monkeypatch.setattr(os, "replace", held)
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        replace_atomic(src, dst)
    assert len(calls) == files.REPLACE_TRIES == 40
    assert dst.read_bytes() == b"old" and not list(tmp_path.glob(".chosen.jpg.*"))


def test_replace_never_leaves_a_partial_file(tmp_path, monkeypatch):
    dst = tmp_path / "chosen.jpg"; dst.write_bytes(b"old"); src = tmp_path / "new.jpg"; src.write_bytes(JPEG)
    seen = []
    def broken(a, b):
        seen.append(Path(a))
        assert Path(a).parent == dst.parent and Path(a).read_bytes() == JPEG  # whole, beside it
        raise OSError(28, "no space left")
    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError, match="no space"):
        replace_atomic(src, dst)
    assert len(seen) == 1 and seen[0].name == f".chosen.jpg.{os.getpid()}.tmp"
    assert dst.read_bytes() == b"old" and not list(tmp_path.glob(".chosen.jpg.*"))


def test_replace_overwrites_a_crash_leftover_and_makes_the_folder(tmp_path):
    src = tmp_path / "new.jpg"; src.write_bytes(JPEG)
    dst = tmp_path / "member" / "history" / "7.jpg"
    dst.parent.mkdir(parents=True)
    leftover = dst.with_name(f".7.jpg.{os.getpid()}.tmp"); leftover.write_bytes(b"half")
    replace_atomic(src, dst)
    assert dst.read_bytes() == JPEG and not leftover.exists()
    replace_atomic(src, tmp_path / "other" / "chosen.jpg")
    assert (tmp_path / "other" / "chosen.jpg").read_bytes() == JPEG


def test_paths_are_built_from_integers_only(tmp_path):
    d = tmp_path / "selfies" / "101"
    assert member_dir(tmp_path, 101) == d
    assert member_dir(str(tmp_path), 101) == d
    assert set_dir(tmp_path, 101, 2) == d / "sets" / "2"
    assert staging_dir(tmp_path, 101, 2) == d / "sets" / "2.part"
    assert candidate(tmp_path, 101, 2, 3) == d / "sets" / "2" / "c3.jpg"
    assert candidate(tmp_path, 101, 2, 3, thumb=True) == d / "sets" / "2" / "c3.thumb.jpg"
    assert chosen(tmp_path, 101) == d / "chosen.jpg"
    assert history(tmp_path, 101, 7) == d / "history" / "7.jpg"
    for bad in ("101", "../101", 101.0, True, None, b"101"):
        for build in (lambda: member_dir(tmp_path, bad), lambda: set_dir(tmp_path, bad, 1),
                      lambda: set_dir(tmp_path, 101, bad), lambda: staging_dir(tmp_path, 101, bad),
                      lambda: candidate(tmp_path, 101, 1, bad),
                      lambda: candidate(tmp_path, bad, 1, 0), lambda: chosen(tmp_path, bad),
                      lambda: history(tmp_path, 101, bad), lambda: clear_stale(tmp_path, bad, 1),
                      lambda: sync_chosen(tmp_path, bad, None),
                      lambda: sync_chosen(tmp_path, 101, Current(101, bad, 0, 1))):
            with pytest.raises(TypeError):
                build()
    for build in (lambda: member_dir(tmp_path, 0), lambda: set_dir(tmp_path, 101, 0),
                  lambda: candidate(tmp_path, 101, 1, 4), lambda: candidate(tmp_path, 101, 1, -1),
                  lambda: history(tmp_path, 101, 0), lambda: member_dir(tmp_path, 1 << 32)):
        with pytest.raises(ValueError):
            build()
    with pytest.raises(ValueError):  # another member's photo is never this member's
        sync_chosen(tmp_path, 101, Current(102, 1, 0, 1))


def test_clear_stale_removes_the_set_dir_and_its_staging_dir_only(tmp_path):
    for guid, set_no in ((101, 1), (101, 2), (102, 1)):
        set_dir(tmp_path, guid, set_no).mkdir(parents=True)
        candidate(tmp_path, guid, set_no, 0).write_bytes(JPEG)
    staging_dir(tmp_path, 101, 1).mkdir()
    candidate(tmp_path, 101, 1, 0).with_name("c1.thumb.jpg").write_bytes(JPEG)
    (staging_dir(tmp_path, 101, 1) / "c0.jpg").write_bytes(JPEG)
    chosen(tmp_path, 101).write_bytes(JPEG)
    history(tmp_path, 101, 1).parent.mkdir()
    history(tmp_path, 101, 1).write_bytes(JPEG)
    clear_stale(tmp_path, 101, 1)
    assert not set_dir(tmp_path, 101, 1).exists() and not staging_dir(tmp_path, 101, 1).exists()
    assert candidate(tmp_path, 101, 2, 0).exists() and candidate(tmp_path, 102, 1, 0).exists()
    assert chosen(tmp_path, 101).exists() and history(tmp_path, 101, 1).exists()
    clear_stale(tmp_path, 101, 1)  # nothing there: nothing to do
    clear_stale(tmp_path, 103, 1)  # no folder for that member at all


def test_sync_changes_nothing_when_the_files_already_agree(tmp_path, store):
    ready(store, tmp_path, 101, 1)
    assert sync_chosen(tmp_path, 101, None) is False and not member_dir(tmp_path, 101).joinpath(
        "chosen.jpg").exists()
    n = pick(store, 101, 1, 1, source="console", now=2.0)
    assert sync_chosen(tmp_path, 101, current(store, 101)) is True
    stamp = chosen(tmp_path, 101).stat().st_mtime_ns
    assert sync_chosen(tmp_path, 101, current(store, 101)) is False
    assert chosen(tmp_path, 101).stat().st_mtime_ns == stamp
    assert history(tmp_path, 101, n).read_bytes() == candidate(tmp_path, 101, 1, 1).read_bytes()


def test_sync_replaces_a_stale_photo_and_keeps_the_history(tmp_path, store):
    ready(store, tmp_path, 101, 1)
    a = pick(store, 101, 1, 0, source="console", now=2.0)
    sync_chosen(tmp_path, 101, current(store, 101))
    b = pick(store, 101, 1, 3, source="console", now=3.0)  # crash before its files
    assert chosen(tmp_path, 101).read_bytes() == candidate(tmp_path, 101, 1, 0).read_bytes()
    assert sync_chosen(tmp_path, 101, current(store, 101)) is True
    assert chosen(tmp_path, 101).read_bytes() == candidate(tmp_path, 101, 1, 3).read_bytes()
    history(tmp_path, 101, b).write_bytes(b"torn")  # a history file that is not the photo
    assert sync_chosen(tmp_path, 101, current(store, 101)) is True
    assert history(tmp_path, 101, b).read_bytes() == candidate(tmp_path, 101, 1, 3).read_bytes()
    u = undo(store, b, source="console", now=4.0)
    assert sync_chosen(tmp_path, 101, current(store, 101)) is True
    assert chosen(tmp_path, 101).read_bytes() == candidate(tmp_path, 101, 1, 0).read_bytes()
    assert {p.name for p in history(tmp_path, 101, a).parent.iterdir()} == {
        f"{a}.jpg", f"{b}.jpg", f"{u}.jpg"}
    assert not list(member_dir(tmp_path, 101).rglob("*.tmp"))


def test_sync_falls_back_to_the_history_copy_and_refuses_without_any(tmp_path, store):
    ready(store, tmp_path, 101, 1)
    n = pick(store, 101, 1, 2, source="console", now=2.0)
    sync_chosen(tmp_path, 101, current(store, 101))
    photo = chosen(tmp_path, 101).read_bytes()
    chosen(tmp_path, 101).unlink()
    clear_stale(tmp_path, 101, 1)  # the set's files are gone; the history copy is not
    assert sync_chosen(tmp_path, 101, current(store, 101)) is True
    assert chosen(tmp_path, 101).read_bytes() == photo
    history(tmp_path, 101, n).unlink()
    with pytest.raises(FileNotFoundError):
        sync_chosen(tmp_path, 101, current(store, 101))
    assert chosen(tmp_path, 101).read_bytes() == photo  # what was there stays


def test_ready_writes_four_distinct_jpegs_and_thumbnails(tmp_path, store):
    ready(store, tmp_path, 101, 1)
    shots = [candidate(tmp_path, 101, 1, i).read_bytes() for i in range(4)]
    thumbs = [candidate(tmp_path, 101, 1, i, thumb=True).read_bytes() for i in range(4)]
    assert len(set(shots + thumbs)) == 8
    assert all(b[:2] == b"\xff\xd8" and b[-2:] == b"\xff\xd9" for b in shots + thumbs)
    assert sorted(p.name for p in set_dir(tmp_path, 101, 1).iterdir()) == [
        "c0.jpg", "c0.thumb.jpg", "c1.jpg", "c1.thumb.jpg", "c2.jpg", "c2.thumb.jpg",
        "c3.jpg", "c3.thumb.jpg"]
