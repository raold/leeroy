"""Voice's admin pins obey the same opt-in borrow fence as generated chat."""
import asyncio
import threading

import pytest

from conftest import run
from nightly.conftest import h
from nightly.test_request_borrow import rig, settle
from leeroy_mind.llm.borrow import BorrowInterrupted
from leeroy_mind.voice.gpu import OllamaPins


def test_voice_lease_and_pin_wait_for_image_worker_stop(h,monkeypatch):
    arb,admin,w=rig(h)
    pins=OllamaPins('http://unused.invalid')
    pins.live_borrow=arb.live_admission
    calls=[]
    def loaded():
        assert not w.running
        calls.append('loaded')
        return {h.LIVE,h.EMB}
    def post(path,body):
        assert not w.running
        calls.append(path)
    monkeypatch.setattr(pins,'_loaded',loaded)
    monkeypatch.setattr(pins,'_post',post)
    async def go():
        entered=asyncio.Event(); w.release_stop=asyncio.Event()
        async def render():
            async with arb.borrow('request:1') as room:
                room.own_worker(w)
                await w.start()
                entered.set()
                await asyncio.Event().wait()
        task=asyncio.create_task(render())
        await entered.wait()
        assert arb.hold('voice_gpu')
        handback=asyncio.create_task(arb.hand_over())
        pin=asyncio.create_task(pins.pin(h.LIVE,h.EMB))
        await w.stopping.wait()
        await settle()
        assert calls==[] and not pin.done() and not handback.done()
        w.release_stop.set()
        with pytest.raises(BorrowInterrupted):
            await task
        await asyncio.gather(handback,pin)
        assert calls==['loaded','/api/chat','/api/embed']
    run(go())


def test_cancelled_pin_keeps_its_thread_in_the_borrow_drain(h,monkeypatch):
    arb,_,_=rig(h)
    pins=OllamaPins('http://unused.invalid')
    pins.live_borrow=arb.live_admission
    entered,release,exited=threading.Event(),threading.Event(),threading.Event()
    monkeypatch.setattr(pins,'_loaded',lambda:{h.LIVE})
    def post(path,body):
        entered.set()
        release.wait(2)
        exited.set()
    monkeypatch.setattr(pins,'_post',post)
    async def go():
        pin=asyncio.create_task(pins.pin(h.LIVE,h.EMB))
        assert await asyncio.to_thread(entered.wait,1)
        arb.live_admission.close()
        drain=asyncio.create_task(arb.live_admission.drain())
        pin.cancel()
        await settle()
        assert not pin.done() and not drain.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pin
        await drain
        assert exited.is_set()
        arb.live_admission.open()
    try:
        run(go())
    finally:
        release.set()
