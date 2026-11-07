"""The shared per-slot loop (Task 3): flagged retries, one oom room, identity and grid
requests, against the fake worker."""
import asyncio
from pathlib import Path

import pytest

from conftest import run
from leeroy_mind.selfies.slot import render_slot
from leeroy_mind.selfies.stage import SetRefused
from leeroy_mind.selfies.worker import Identity, SelfieWorker, WorkerError

from .fakes import JPEG, fake_command, request


def test_render_slot_replaces_a_flagged_image_and_retries_one_oom(tmp_path):
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_FLAG_SEEDS="11", FAKE_SELFIE_OOM="1")
    rooms = []

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        try:
            return await render_slot(w, lambda a: request(tmp_path, seed=11 + a),
                                     rid=lambda a: f"t:{a}",
                                     make_room=lambda: rooms.append(1) or asyncio.sleep(0))
        finally:
            await w.stop()
    rendered, attempts = run(go())
    assert attempts == 2 and not rendered.flagged and rooms == [1]


def test_three_flags_raise_set_refused(tmp_path):
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_FLAG_SEEDS="11,12,13")

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        try:
            await render_slot(w, lambda a: request(tmp_path, seed=11 + a),
                              rid=lambda a: f"t:{a}", make_room=lambda: asyncio.sleep(0))
        finally:
            await w.stop()
    with pytest.raises(SetRefused):
        run(go())


def test_a_second_oom_propagates(tmp_path):
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_OOM="2")

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        try:
            await render_slot(w, lambda a: request(tmp_path), rid=lambda a: f"t:{a}",
                              make_room=lambda: asyncio.sleep(0))
        finally:
            await w.stop()
    with pytest.raises(WorkerError, match="^oom$"):
        run(go())


def test_an_identity_request_carries_its_reference_and_shape(tmp_path):
    log = tmp_path / "requests.log"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_IDENTITY="1", FAKE_SELFIE_LOG=str(log))
    ref = tmp_path / "ref.jpg"
    ref.write_bytes(JPEG)
    ident = Identity(ref, (0.0, 0.0, 0.5, 0.5), 0.6)

    async def go():
        w = SelfieWorker(cmd, env=env)
        ready = await w.start()
        try:
            got = await w.render(request(tmp_path, identity=ident, shape="portrait"), rid="i1")
            with pytest.raises(WorkerError, match="^bad_request$"):
                await w.render(request(tmp_path), rid="i2")
            return ready, got
        finally:
            await w.stop()
    ready, got = run(go())
    assert ready.identity is True
    assert (got.width, got.height) == (832, 1216)
    assert log.read_text().splitlines()[0] == f"i1 1234 identity {ref}"


def test_a_grid_answers_its_size_and_a_missing_tile_keeps_the_worker(tmp_path):
    cmd, env = fake_command(tmp_path)
    tiles = []
    for i in range(4):
        t = tmp_path / f"t{i}.jpg"
        t.write_bytes(JPEG)
        tiles.append(t)

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        try:
            got = await w.grid(tiles, tile=512, gutter=8, out=tmp_path / "g.jpg",
                               thumb=tmp_path / "g.thumb.jpg", thumb_size=384)
            with pytest.raises(WorkerError, match="^missing_tile$"):
                await w.grid([*tiles, tmp_path / "nope.jpg"], tile=512, gutter=8,
                             out=tmp_path / "h.jpg", thumb=tmp_path / "h.thumb.jpg",
                             thumb_size=384)
            return got, w.running
        finally:
            await w.stop()
    got, running = run(go())
    assert (got.width, got.height) == (8 + 2 * 520, 8 + 2 * 520)
    assert running is True
    assert (tmp_path / "g.jpg").read_bytes() == JPEG
    assert (tmp_path / "g.thumb.jpg").read_bytes() == JPEG
