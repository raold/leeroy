"""Requested images borrow only fake GPU resources and own all cleanup."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import run
from nightly.conftest import h, FakeAdmin
from leeroy_mind.llm.borrow import BorrowGate, BorrowInterrupted, LiveReloadFailed
from leeroy_mind.nightly.gpu import GpuArbiter, LiveLeaseHeld, PausedRoom
from leeroy_mind.nightly.wiring import enable_borrow
from leeroy_mind.testing.fake_llm import FakeLLM


class Worker:
    def __init__(self, order):
        self.order = order
        self.running = False
        self.stopping = asyncio.Event()
        self.release_stop = None
        self.fail_stop = False

    async def start(self):
        self.order.append(('start',))
        self.running = True

    async def stop(self):
        self.stopping.set()
        if self.release_stop is not None:
            await self.release_stop.wait()
        if self.fail_stop:
            raise RuntimeError('synthetic stop fault')
        self.order.append(('stop',))
        self.running = False


def rig(h):
    admin = FakeAdmin([h.LIVE, h.REFL, h.EMB, 'invented-extra'])
    arb = GpuArbiter(admin,h.settings)
    arb.live_admission = BorrowGate(h.clock)
    return arb,admin,Worker(admin.calls)


async def settle():
    for _ in range(12):
        await asyncio.sleep(0)


@pytest.mark.parametrize('holder', ['human','offline_raid','voice_gpu','preview','unknown'])
def test_hard_lease_refuses_without_touching_models_or_budget(h,holder):
    arb,admin,worker = rig(h)
    arb.hold(holder)
    budget=[]
    async def go():
        with pytest.raises(LiveLeaseHeld):
            async with arb.borrow('request:1',on_admitted=lambda:budget.append(True)):
                pytest.fail('admitted a hard lease')
    run(go())
    assert not admin.calls and not budget and not arb.live_admission.blocked


def test_paused_and_disabled_refuse(h):
    arb,admin,_ = rig(h)
    async def go():
        arb.paused=True
        with pytest.raises(PausedRoom):
            async with arb.borrow('request:1'):
                pass
        arb.paused=False
        arb.live_admission=None
        with pytest.raises(LiveLeaseHeld):
            async with arb.borrow('request:1'):
                pass
    run(go())
    assert not admin.calls


def test_presence_leases_allow_borrow_and_stop_precedes_live_reload(h):
    arb,admin,w = rig(h)
    arb.hold('server'); arb.hold('discord')
    admitted=[]
    async def go():
        async with arb.borrow('request:1',on_admitted=lambda:admitted.append(True)) as room:
            assert admin.loaded == [h.EMB]
            assert not room.blockers() and not arb.reflection_allowed()
            room.own_worker(w)
            await w.start()
        assert not w.running and not arb.live_admission.blocked
        assert arb.holders == frozenset({'server','discord'})
        assert admin.calls[-2:] == [('stop',),('preload',h.LIVE,-1)]
        assert admin.preload_options[-1] == {'num_ctx':4096}
        assert room.timings['ms'] >= 0
    run(go())
    assert admitted == [True]
    assert ('unload',h.EMB) not in admin.calls


def test_budget_refusal_never_unloads_or_closes_admission(h):
    arb,admin,_=rig(h)
    async def go():
        with pytest.raises(LiveLeaseHeld) as caught:
            async with arb.borrow('request:1',on_admitted=lambda:False):
                pass
        assert caught.value.holders == frozenset({'request_budget'})
    run(go())
    assert not admin.calls and not arb.live_admission.blocked


def test_borrow_drains_active_chat_and_blocks_new_chat_but_not_embeddings(h):
    arb,admin,w=rig(h)
    async def go():
        old_done=asyncio.Event()
        llm=FakeLLM(gate=old_done)
        llm.live_borrow=arb.live_admission
        first=asyncio.create_task(llm.chat('live',[],options={}))
        await settle()
        llm._gate=None
        entered,release=asyncio.Event(),asyncio.Event()
        async def render():
            async with arb.borrow('request:1') as room:
                room.own_worker(w)
                await w.start()
                entered.set()
                await release.wait()
        borrow=asyncio.create_task(render())
        await settle()
        assert arb.live_admission.blocked and not admin.calls
        second=asyncio.create_task(llm.chat('live',[],options={}))
        assert await llm.embed('embed',['invented'])
        await settle()
        assert len([c for c in llm.calls if 'messages' in c]) == 1
        old_done.set()
        await first
        await entered.wait()
        assert not second.done()
        release.set()
        await borrow
        await second
        assert len([c for c in llm.calls if 'messages' in c]) == 2
    run(go())


@pytest.mark.parametrize('cause',['external','hard_lease','pause','shutdown','models'])
def test_midrender_interrupt_joins_worker_before_any_handback(h,cause):
    arb,admin,w=rig(h)
    async def go():
        entered=asyncio.Event()
        w.release_stop=asyncio.Event()
        async def render():
            async with arb.borrow('request:1') as room:
                room.own_worker(w)
                await w.start()
                entered.set()
                await asyncio.Event().wait()
        task=asyncio.create_task(render())
        await entered.wait()
        handback=None
        if cause=='external':
            task.cancel()
        elif cause=='hard_lease':
            handback=asyncio.create_task(arb.acquire_live('voice_gpu'))
        elif cause=='pause':
            arb.cancel_borrow('gpu_pause')
            arb.paused=True
        elif cause=='shutdown':
            handback=asyncio.create_task(arb.close_borrow())
        else:
            arb.reconfigure(replace(h.settings,live_model='new-live'))
        await w.stopping.wait()
        if cause=='external':
            task.cancel()  # repeated cancellation may not cut teardown short
        await settle()
        assert w.running and not task.done() and arb.live_admission.blocked
        assert not any(c[0]=='preload' for c in admin.calls)
        if handback:
            assert not handback.done()
        w.release_stop.set()
        with pytest.raises(asyncio.CancelledError if cause=='external' else BorrowInterrupted):
            await task
        if handback:
            await handback
        assert not w.running and arb.borrow_holder is None
        if cause=='pause':
            assert arb.live_admission.blocked
            assert not any(c[0]=='preload' for c in admin.calls)
            arb.paused=False
            await arb.recover_borrow()
        assert not arb.live_admission.blocked
        preloads=[i for i,c in enumerate(admin.calls) if c[0]=='preload']
        assert preloads and admin.calls.index(('stop',)) < min(preloads)
        if cause=='models':
            assert admin.calls[-1] == ('preload','new-live',-1)
    run(go())


def test_180_second_work_budget_does_not_cut_off_safe_cleanup(h):
    arb,admin,w=rig(h)
    assert arb.borrow_timeout==180
    arb.borrow_timeout=.01  # exercise the real timeout without a three-minute test
    async def go():
        entered=asyncio.Event()
        w.release_stop=asyncio.Event()
        credit=arb.live_admission.mark()
        async def render():
            async with arb.borrow('request:1') as room:
                room.own_worker(w)
                await w.start()
                entered.set()
                await asyncio.Event().wait()
        task=asyncio.create_task(render())
        await entered.wait()
        await w.stopping.wait()
        h.clock.advance(205)  # cleanup can outlive the admitted work budget
        assert credit.seconds==205 and not task.done()
        w.release_stop.set()
        with pytest.raises(TimeoutError):
            await task
        assert credit.seconds==205 and not arb.live_admission.blocked
    run(go())


def test_failed_live_reload_stays_closed_and_next_admission_repairs_it(h):
    arb,admin,w=rig(h)
    original=admin.preload
    attempts=[]
    async def fail(*a,**kw):
        attempts.append(True)
        raise RuntimeError('synthetic model down')
    async def go():
        admin.preload=fail
        with pytest.raises(LiveReloadFailed):
            async with arb.borrow('request:1') as room:
                room.own_worker(w)
                await w.start()
        assert attempts==[True,True] and not w.running
        assert arb.live_admission.blocked and arb.needs_live_reload
        assert not arb.reflection_allowed()
        llm=FakeLLM(); llm.live_borrow=arb.live_admission
        with pytest.raises(LiveReloadFailed):
            await llm.chat('live',[],options={})
        assert not llm.calls
        admin.preload=original
        await arb.recover_borrow()
        assert not arb.live_admission.blocked
        await llm.chat('live',[],options={})
        assert len(llm.calls)==1
    run(go())


def test_failed_worker_stop_never_loads_live_until_retry_proves_it_gone(h):
    arb,admin,w=rig(h)
    async def go():
        w.fail_stop=True
        with pytest.raises(LiveReloadFailed):
            async with arb.borrow('request:1') as room:
                room.own_worker(w)
                await w.start()
        assert w.running and arb.live_admission.blocked
        assert not any(c[0]=='preload' for c in admin.calls)
        with pytest.raises(LiveReloadFailed):
            await arb.recover_borrow()
        with pytest.raises(LiveLeaseHeld):
            await arb.make_room()
        with pytest.raises(LiveReloadFailed):
            await arb.ensure_model(h.REFL)
        w.fail_stop=False
        await arb.recover_borrow()
        assert not w.running and not arb.live_admission.blocked
        assert admin.calls[-2:] == [('stop',),('preload',h.LIVE,-1)]
    run(go())


def test_opt_in_wiring_shares_fake_client_and_refuses_winding_down_work(h):
    arb,admin,_=rig(h)
    arb.live_admission=None
    drains=[]
    llm=FakeLLM()
    app=SimpleNamespace(clock=h.clock,llm=llm,arbiter=arb,engine=SimpleNamespace(),
        scheduler=SimpleNamespace(),night_scheduler=SimpleNamespace(_task=None),
        register_shutdown_drain=drains.append)
    async def go():
        gate=enable_borrow(app)
        assert enable_borrow(app) is gate and len(drains)==1
        assert app.engine.live_borrow is app.scheduler.live_borrow is llm.live_borrow is gate
        app.night_scheduler._task=asyncio.create_task(asyncio.Event().wait())
        with pytest.raises(LiveLeaseHeld) as caught:
            async with arb.borrow('request:1'):
                pass
        assert caught.value.holders==frozenset({'image_work'})
        app.night_scheduler._task.cancel()
        await asyncio.gather(app.night_scheduler._task,return_exceptions=True)
    run(go())
    assert not admin.calls


def test_timeout_during_initial_drain_never_unloads_an_active_chat(h):
    arb,admin,_=rig(h)
    arb.borrow_timeout=.01
    async def go():
        release=asyncio.Event()
        llm=FakeLLM(gate=release); llm.live_borrow=arb.live_admission
        chat=asyncio.create_task(llm.chat('live',[],options={}))
        await settle()
        async def attempt():
            async with arb.borrow('request:1'):
                pytest.fail('drain timed out before admission')
        task=asyncio.create_task(attempt())
        # Await the actual timeout signal/cleanup state without advancing fake time.
        async with asyncio.timeout(1):
            while arb._borrow is None or not arb._borrow.cleaning:
                await asyncio.sleep(.001)
        assert not admin.calls and not task.done()
        release.set(); await chat
        with pytest.raises(TimeoutError):
            await task
        assert not arb.live_admission.blocked
    run(go())


def test_cancelled_unload_is_joined_before_reloading_live(h):
    arb,admin,_=rig(h)
    async def go():
        entered,release=asyncio.Event(),asyncio.Event()
        original=admin.unload
        async def unload(name):
            entered.set()
            await release.wait()
            await original(name)
        admin.unload=unload
        async def attempt():
            async with arb.borrow('request:1'):
                pytest.fail('cancelled before worker admission')
        task=asyncio.create_task(attempt())
        await entered.wait()
        task.cancel()
        await settle()
        assert not task.done() and not any(c[0]=='preload' for c in admin.calls)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admin.calls[-1]==('preload',h.LIVE,-1)
        assert not arb.live_admission.blocked
    run(go())


def test_worker_registered_before_failed_start_is_still_stopped(h):
    arb,admin,w=rig(h)
    async def go():
        with pytest.raises(RuntimeError,match='synthetic start'):
            async with arb.borrow('request:1') as room:
                room.own_worker(w)
                w.running=True  # partial startup already owns a process
                raise RuntimeError('synthetic start')
        assert not w.running and admin.calls[-2:]==[('stop',),('preload',h.LIVE,-1)]
    run(go())


def test_gpu_control_pause_can_interrupt_request_lease_and_resume_reopens_gate(tmp_path):
    from selfies.fakes import mind_app,known_offline
    async def go():
        app=known_offline(mind_app(tmp_path,toml='[server]\nenabled=true\n'))
        try:
            app.arbiter.admin=FakeAdmin([app.cfg.models.live,app.cfg.models.embedding])
            gate=enable_borrow(app)
            w=Worker(app.arbiter.admin.calls)
            entered=asyncio.Event()
            async def render():
                async with app.arbiter.borrow('request:1') as room:
                    room.own_worker(w)
                    await w.start()
                    entered.set()
                    await asyncio.Event().wait()
            task=asyncio.create_task(render())
            await entered.wait()
            reply=await app.gpu.action({'action':'pause'})
            assert reply['ok'] and reply['paused']
            with pytest.raises(BorrowInterrupted):
                await task
            assert not w.running and gate.blocked and app.arbiter.admin.loaded==[]
            assert not any(c[0]=='preload' for c in app.arbiter.admin.calls)
            reply=await app.gpu.action({'action':'resume'})
            assert reply['ok'] and not reply['paused']
            await app.engine.live_admission()
            assert not gate.blocked
            assert app.cfg.models.live in app.arbiter.admin.loaded
        finally:
            await app.stop()
    run(go())


def test_cancelled_recovery_handback_keeps_lock_until_preload_finishes(h):
    arb,admin,_=rig(h)
    async def go():
        entered,release=asyncio.Event(),asyncio.Event()
        original=admin.preload
        async def preload(*args,**kwargs):
            entered.set()
            await release.wait()
            await original(*args,**kwargs)
        admin.preload=preload
        arb.live_admission.close()
        arb.live_admission.fail()
        arb.hold('server')
        task=asyncio.create_task(arb.hand_over())
        await entered.wait()
        task.cancel()
        await settle()
        assert not task.done() and arb._lock.locked() and arb.live_admission.blocked
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not arb._lock.locked() and not arb.live_admission.blocked
        assert admin.calls[-1]==('preload',h.LIVE,-1)
    run(go())


@pytest.mark.parametrize('change',['connection','presence'])
def test_request_service_losing_known_offline_cancels_borrow_before_reload(tmp_path,change):
    from photos.test_request_service import make_app,offline
    async def go():
        app=make_app(tmp_path)
        try:
            await offline(app)
            service=app.photos.requests
            w=Worker(app.arbiter.admin.calls)
            entered=asyncio.Event()
            async def render():
                async with app.arbiter.borrow('request:1') as room:
                    room.own_worker(w)
                    await w.start()
                    entered.set()
                    await asyncio.Event().wait()
            task=asyncio.create_task(render())
            await entered.wait()
            if change=='connection':
                await service._connection(False)
            else:
                app.night_scheduler.presence.online=None
                await service._presence(SimpleNamespace(human=True))
            with pytest.raises(BorrowInterrupted) as caught:
                await task
            assert caught.value.code==('presence_unknown' if change=='connection' else 'presence_changed')
            assert not service.known_offline() and service._offline_at is None
            assert not w.running and not app.live_borrow.blocked
            calls=app.arbiter.admin.calls
            assert calls.index(('stop',)) < next(i for i,c in enumerate(calls) if c[0]=='preload')
        finally:
            await app.stop()
    run(go())
