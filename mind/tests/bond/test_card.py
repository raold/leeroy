from dataclasses import replace
import threading

import pytest

from conftest import run
from guards.test_live import rig, turn
from leeroy_mind.talk.pipeline import TurnMeta


@pytest.mark.parametrize('enabled',[False,True])
def test_persona_and_dials_are_captured_before_read_only_worker(rig,monkeypatch,enabled):
    engine=rig.engine
    engine.cfg=replace(engine.cfg,talk=replace(engine.cfg.talk,aware=True),
                       romance=replace(engine.cfg.romance,activities=enabled))
    caller=threading.get_ident()
    original=engine.personas.get
    def caller_only(guid):
        assert threading.get_ident()==caller, 'persona_cache_accessed_in_worker'
        return original(guid)
    monkeypatch.setattr(engine.personas,'get',caller_only)
    text=run(engine.live_review.block_async(turn('whisper'),TurnMeta('bond-test'),close=True))
    assert '<awareness_observations>' in text
