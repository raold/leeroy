import asyncio
import time
from types import SimpleNamespace

import pytest

from conftest import run
from test_endpoint import served, ask, Clock, line, KEY
from leeroy_mind.endpoint import parse_request, BadRequest


class Armory:
    def __init__(self,counts=None,delay=0):
        self.counts, self.delay, self.calls = counts,delay,[]
    def kit(self,guid):
        self.calls.append(guid)
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
            await ask(ep,'kit')
            assert armory.calls==[1,1]
    run(go())
