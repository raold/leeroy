"""Borrow time is credited once and every direct non-embedding exchange is fenced."""
import asyncio
import threading

import pytest

from conftest import run
from nightly.conftest import StepClock, START
from leeroy_mind.llm.borrow import BorrowGate
from leeroy_mind.llm.client import OllamaClient
from leeroy_mind.llm.scheduler import LLMScheduler, Priority, Expired, SchedulerClosed
from leeroy_mind.testing.fake_llm import FakeLLM


async def settle():
    for _ in range(12):
        await asyncio.sleep(0)


def test_credit_counts_overlapping_wait_once_and_keeps_base_immutable():
    clock=StepClock(START)
    gate=BorrowGate(clock)
    before=gate.mark()
    base=clock.now()+10
    clock.advance(20)
    assert before.deadline(base)==base  # ordinary queue time receives no credit
    gate.close()
    clock.advance(100)
    during=gate.mark()
    clock.advance(25)
    assert before.seconds==125 and during.seconds==25
    assert before.deadline(base)==before.deadline(base)==base+125
    gate.open()
    clock.advance(50)
    assert before.seconds==125
    gate.close()
    clock.advance(10)
    gate.open()
    assert before.seconds==135 and during.seconds==35


def test_scheduler_credits_queued_and_running_waits_without_expiring_them():
    async def go():
        clock=StepClock(START)
        llm=FakeLLM()
        gate=llm.live_borrow=BorrowGate(clock)
        scheduler=LLMScheduler(llm,clock=clock,max_inflight=1)
        scheduler.live_borrow=gate
        hold=asyncio.Event()
        first=asyncio.create_task(scheduler.submit(Priority.SOCIAL,lambda llm:hold.wait()))
        await settle()
        credit=gate.mark(); base=clock.now()+10
        async def chat(llm):
            await gate.wait()
            assert not scheduler.expired(base,credit=credit)
            return await llm.chat('live',[],options={})
        queued=asyncio.create_task(scheduler.submit(Priority.REPLY_HUMAN,chat,deadline=base,credit=credit))
        await settle()
        gate.close(); clock.advance(120)
        hold.set(); await first
        await settle()
        assert not queued.done() and not llm.calls
        # Time beyond the 180s work budget can be safe cleanup. It is credited too.
        clock.advance(100)
        assert not scheduler.expired(base,credit=credit)
        gate.open()
        assert (await queued).text
        assert len(llm.calls)==1 and credit.deadline(base)==base+220
        await scheduler.close()
    run(go())


def test_borrow_does_not_revive_a_job_already_expired_before_it_started():
    async def go():
        clock=StepClock(START)
        gate=BorrowGate(clock)
        scheduler=LLMScheduler(FakeLLM(),clock=clock)
        scheduler.live_borrow=gate
        credit=gate.mark(); base=clock.now()+1
        clock.advance(2); gate.close(); clock.advance(100)
        with pytest.raises(Expired):
            await scheduler.submit(Priority.SOCIAL,lambda llm:llm.chat('live',[],options={}),
                                   deadline=base,credit=credit)
        gate.open()
        await scheduler.close()
    run(go())


def test_scheduler_marks_generic_background_jobs_when_borrowing_enabled():
    async def go():
        clock=StepClock(START)
        llm=FakeLLM()
        scheduler=LLMScheduler(llm,clock=clock,max_inflight=1)
        gate=llm.live_borrow=scheduler.live_borrow=BorrowGate(clock)
        first_release=asyncio.Event()
        first=asyncio.create_task(scheduler.submit(Priority.SOCIAL,lambda llm:first_release.wait()))
        await settle()
        queued=asyncio.create_task(scheduler.submit(Priority.BACKGROUND,
            lambda llm:llm.chat('live',[],options={}),deadline=clock.now()+1))
        await settle()
        gate.close(); clock.advance(100); gate.open()
        first_release.set()
        await first
        assert (await queued).text
        await scheduler.close()
    run(go())


def test_ollama_gate_tracks_real_thread_lifetime_and_bypasses_embeddings(monkeypatch):
    """The HTTP thread is fake, but OllamaClient's exchange/cancellation path is real."""
    entered,release,exited=threading.Event(),threading.Event(),threading.Event()
    calls=[]
    client=OllamaClient('http://unused.invalid')
    gate=client.live_borrow=BorrowGate(StepClock(START))
    def request(path,body,timeout,attempt):
        calls.append(path)
        if path=='/api/embed':
            return {'embeddings':[[1.0]*512]}
        if not entered.is_set():
            entered.set()
            while not release.wait(.005):
                if attempt.aborted:
                    break
            exited.set()
        return {'message':{'content':'invented reply'}}
    monkeypatch.setattr(client,'_request',request)
    async def go():
        first=asyncio.create_task(client.chat('live',[],options={}))
        assert await asyncio.to_thread(entered.wait,1)
        gate.close()
        drain=asyncio.create_task(gate.drain())
        second=asyncio.create_task(client.chat('live',[],options={}))
        assert await client.embed('embed',['invented'])
        await settle()
        assert calls==['/api/chat','/api/embed'] and not drain.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await drain
        assert exited.is_set() and not second.done()
        gate.open()
        assert (await second).text=='invented reply'
        assert calls==['/api/chat','/api/embed','/api/chat']
    try:
        run(go())
    finally:
        release.set()


def test_concurrent_child_waits_share_one_reacquired_slot():
    """Both gather children inherit the PauseGate job context; neither can jump a slot."""
    from leeroy_mind.llm.pause import PauseGate
    async def go():
        clock=StepClock(START)
        llm=FakeLLM()
        gate=llm.live_borrow=BorrowGate(clock)
        scheduler=LLMScheduler(llm,clock=clock,max_inflight=1)
        scheduler.live_borrow=gate
        scheduler.gpu_gate=PauseGate()
        gate.close()
        children=[]
        late_start=asyncio.Event()
        async def late_chat(llm):
            await late_start.wait()
            return await llm.chat('live',[],options={})
        async def chats(llm):
            children.extend(asyncio.create_task(llm.chat('live',[],options={})) for _ in range(2))
            children.append(asyncio.create_task(late_chat(llm)))
            return await asyncio.gather(*children,return_exceptions=True)
        waiting=asyncio.create_task(scheduler.submit(Priority.BACKGROUND,chats))
        blocker_release=asyncio.Event()
        blocker=None
        try:
            await settle()
            assert scheduler.pending()[Priority.BACKGROUND]==1
            assert scheduler._low_running==0
            blocker=asyncio.create_task(scheduler.submit(Priority.REPLY_HUMAN,
                lambda llm:blocker_release.wait()))
            await settle()
            gate.open()
            await settle()
            assert not llm.calls and not waiting.done()
            assert scheduler.pending()[Priority.BACKGROUND]==1
            # A later child sees an open gate, but still must join its parked
            # siblings rather than starting HTTP while another job owns the slot.
            late_start.set()
            await settle()
            assert not llm.calls
            # One inherited waiter cannot cancel the other's shared slot future.
            children[0].cancel()
            await settle()
            assert not llm.calls and not waiting.done()
            blocker_release.set()
            await blocker
            result=await waiting
            assert isinstance(result[0],asyncio.CancelledError) and all(r.text for r in result[1:])
            assert len(llm.calls)==2
            await settle()
            assert scheduler._low_running==0 and not scheduler._running
        finally:
            gate.open(); blocker_release.set(); late_start.set()
            await scheduler.close()
            await asyncio.gather(waiting,*( [blocker] if blocker is not None else []),
                                 return_exceptions=True)
    run(go())


@pytest.mark.parametrize('requeued', [False,True])
@pytest.mark.parametrize('shutdown', [False,True])
def test_parked_jobs_cancel_or_close_without_losing_slots(requeued,shutdown):
    async def go():
        clock=StepClock(START)
        llm=FakeLLM()
        gate=llm.live_borrow=BorrowGate(clock)
        scheduler=LLMScheduler(llm,clock=clock,max_inflight=1)
        scheduler.live_borrow=gate
        gate.close()
        waiting=asyncio.create_task(scheduler.submit(Priority.BACKGROUND,
            lambda llm:llm.chat('live',[],options={})))
        await settle()
        hold=asyncio.Event()
        blocker=asyncio.create_task(scheduler.submit(Priority.SOCIAL,lambda llm:hold.wait()))
        await settle()
        if requeued:
            gate.open()
            await settle()
        if shutdown:
            await scheduler.close()
            with pytest.raises(SchedulerClosed):
                await waiting
        else:
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        hold.set()
        await asyncio.gather(blocker,return_exceptions=True)
        await settle()
        assert not llm.calls and scheduler._low_running==0
        assert not scheduler._running and not sum(scheduler.pending().values())
        await scheduler.close()
    run(go())
