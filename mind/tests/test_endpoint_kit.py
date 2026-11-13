import asyncio
import time
import threading
from types import SimpleNamespace

import pytest

from conftest import run
from test_endpoint import served, ask, Clock, line, KEY
from leeroy_mind.endpoint import parse_request, BadRequest
from leeroy_mind import endpoint


class Armory:
    def __init__(self,counts=None,delay=0):
        self.counts, self.delay, self.calls = counts,delay,[]
        self.called = threading.Event()
    def kit(self,guid):
        self.calls.append(guid)
        self.called.set()
        time.sleep(self.delay)
        return self.counts


def test_kit_takes_no_args():
    assert parse_request(line(v=1,key=KEY,op='kit'),KEY) == ('kit',None)
    with pytest.raises(BadRequest):
        parse_request(line(v=1,key=KEY,op='kit',args={}),KEY)


def test_kit_is_available_while_an_ops_action_is_busy(tmp_path):
    async def go():
        async with served(tmp_path) as (app,ep):
            app.human_guid = 1
            app.guild = SimpleNamespace(armory=Armory({}))
            original = ep.ops
            try:
                ep.ops = SimpleNamespace(busy=True)
                assert (await ask(ep,'kit'))['ok']
            finally:
                ep.ops = original
    run(go())


def test_kit_without_his_character(tmp_path):
    async def go():
        async with served(tmp_path) as (app,ep):
            app.human_guid = 1
            app.guild = SimpleNamespace(armory=Armory())
            assert (await ask(ep,'kit'))['why'] == 'no_character'
    run(go())


def test_kit_op_reuses_a_fresh_reply_and_runs_one_at_a_time(tmp_path):
    clock = Clock()
    async def go():
        async with served(tmp_path,clock=clock,bank_s=.01) as (app,ep):
            app.human_guid = 1
            armory = Armory({46376:2},delay=.1)
            app.guild = SimpleNamespace(armory=armory)
            a,b = await asyncio.gather(ask(ep,'kit'),ask(ep,'kit'))
            assert a['why']==b['why']=='slow' and armory.calls==[1]
            await asyncio.sleep(.15)
            a = await ask(ep,'kit')
            assert a['ok'] and a['groups'][0]['have']==2
            assert await ask(ep,'kit') == a and armory.calls==[1]
            clock.now+=31
            armory.called.clear()
            await ask(ep,'kit')
            assert await asyncio.to_thread(armory.called.wait,1)
            assert armory.calls==[1,1]
    run(go())


def test_late_kit_success_survives_until_the_next_sixty_second_poll(tmp_path):
    clock=Clock()
    entered,release=threading.Event(),threading.Event()
    class GatedArmory:
        calls=[]
        counts={46376:2}
        def kit(self,guid):
            self.calls.append(guid)
            entered.set()
            assert release.wait(3)
            return dict(self.counts)
    async def go():
        async with served(tmp_path,clock=clock,bank_s=.5) as (app,ep):
            app.human_guid=1
            armory=GatedArmory()
            app.guild=SimpleNamespace(armory=armory)
            # The real loopback client gives up before the endpoint's read deadline.
            request=asyncio.create_task(asyncio.to_thread(endpoint.request,ep.port,KEY,'kit',timeout=.1))
            try:
                assert await asyncio.to_thread(entered.wait,2)
                with pytest.raises(TimeoutError):
                    await request
                clock.now+=5
                release.set()
                completed=await ask(ep,'kit')
                assert completed['ok'] and completed['groups'][0]['have']==2
                assert armory.calls==[1]
                # The next tab poll is a minute later. Its refresh is also slow, but
                # the completed first read is useful immediately and only one refresh starts.
                clock.now+=60
                armory.counts={46376:7}
                release.clear()
                entered.clear()
                poll=await ask(ep,'kit')
                assert poll==completed
                assert await asyncio.to_thread(entered.wait,1)
                assert (await ask(ep,'kit'))==completed and armory.calls==[1,1]
                release.set()
                for _ in range(100):
                    fresh=await ask(ep,'kit')
                    if fresh['ok'] and fresh['groups'][0]['have']==7:
                        break
                    await asyncio.sleep(.01)
                assert fresh['groups'][0]['have']==7
            finally:
                release.set()
                await asyncio.gather(request,return_exceptions=True)
    run(go())


def test_kit_cache_follows_his_guid_and_invalid_identity_never_reuses_it(tmp_path):
    class ByGuid:
        calls=[]
        def kit(self,guid):
            self.calls.append(guid)
            return {46376:2 if guid==1 else 7}
    async def go():
        async with served(tmp_path) as (app,ep):
            armory=ByGuid()
            app.guild=SimpleNamespace(armory=armory)
            app.human_guid=1
            assert (await ask(ep,'kit'))['groups'][0]['have']==2
            app.human_guid=101
            assert (await ask(ep,'kit'))['groups'][0]['have']==7
            app.human_guid=0
            assert (await ask(ep,'kit'))['why']=='no_character'
            app.human_guid=1
            assert (await ask(ep,'kit'))['groups'][0]['have']==2
            assert armory.calls==[1,101]
    run(go())


def test_changed_guid_during_a_pending_read_cannot_return_or_cache_the_wrong_kit(tmp_path):
    entered,release=threading.Event(),threading.Event()
    class ChangingArmory:
        calls=[]
        def kit(self,guid):
            self.calls.append(guid)
            if guid==1:
                entered.set()
                assert release.wait(3)
            return {46376:2 if guid==1 else 7}
    async def go():
        async with served(tmp_path,bank_s=2) as (app,ep):
            armory=ChangingArmory()
            app.guild=SimpleNamespace(armory=armory)
            app.human_guid=1
            first=asyncio.create_task(ask(ep,'kit'))
            try:
                assert await asyncio.to_thread(entered.wait,2)
                app.human_guid=101
                a,b=await asyncio.gather(ask(ep,'kit'),ask(ep,'kit'))
                assert a['groups'][0]['have']==b['groups'][0]['have']==7
                assert armory.calls==[1,101]
                release.set()
                old=await first
                assert not old['ok'] and 'items' not in old
                assert (await ask(ep,'kit'))['groups'][0]['have']==7
                app.human_guid=1
                assert (await ask(ep,'kit'))['groups'][0]['have']==2
                assert armory.calls==[1,101]
            finally:
                release.set()
                await asyncio.gather(first,return_exceptions=True)
    run(go())


def test_retained_kit_expires_after_two_poll_intervals(tmp_path):
    clock=Clock()
    async def go():
        async with served(tmp_path,clock=clock) as (app,ep):
            app.human_guid=1
            armory=Armory({46376:2})
            app.guild=SimpleNamespace(armory=armory)
            assert (await ask(ep,'kit'))['ok']
            clock.now+=120
            armory.counts=None
            assert (await ask(ep,'kit'))['why']=='no_character'
            assert armory.calls==[1,1]
    run(go())


def test_invalid_guid_during_a_pending_read_cannot_equal_the_old_character(tmp_path):
    entered,release=threading.Event(),threading.Event()
    class PendingArmory:
        def kit(self,guid):
            entered.set()
            assert release.wait(3)
            return {46376:2}
    async def go():
        async with served(tmp_path,bank_s=2) as (app,ep):
            app.human_guid=1
            app.guild=SimpleNamespace(armory=PendingArmory())
            waiting=asyncio.create_task(ask(ep,'kit'))
            try:
                assert await asyncio.to_thread(entered.wait,2)
                app.human_guid=True  # Equal to 1 in Python, but never a character identity.
                release.set()
                reply=await waiting
                assert not reply['ok'] and 'items' not in reply
            finally:
                release.set()
                await asyncio.gather(waiting,return_exceptions=True)
    run(go())
