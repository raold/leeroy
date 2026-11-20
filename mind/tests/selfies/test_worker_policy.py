"""Real worker protocol with a fake renderer: no models, GPU, network or live data."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from conftest import run
from leeroy_mind.selfies import worker as worker_module
from leeroy_mind.selfies.slot import SetRefused, render_slot
from leeroy_mind.selfies.store import Candidate, ensure_first_sets, mark_ready
from leeroy_mind.selfies.worker import SelfieWorker, WorkerError
from .fakes import request


def policy_worker(tmp_path):
    source = Path(worker_module.__file__).resolve().parents[3]/'selfie'
    script = tmp_path/'policy_worker.py'
    script.write_text(f'''
import json
from pathlib import Path
import sys
sys.path.insert(0, {str(source)!r})
from leeroy_selfie.worker import serve
class Engine:
    info = {{'device': 'fake', 'free_gib': 20., 'versions': {{'fake': '1'}}}}
    def render(self, req):
        score = .15 if req.seed == 7 else .05
        flagged = score >= req.nsfw_max
        if not flagged:
            Path(req.out).write_bytes(b'fake jpeg')
            Path(req.thumb).write_bytes(b'fake jpeg')
        return {{'flagged': flagged, 'nsfw': score, 'prompt': 'invented test portrait',
                 'tokens': 3, 'seconds': .1, 'peak_gib': 1., 'width': 768, 'height': 768}}
    def caption(self, req):
        return {{'flagged': .15 >= req.nsfw_max, 'nsfw': .15,
                 'width': 768, 'height': 768, 'seconds': .1}}
class Input:
    def readline(self, limit):
        line = sys.stdin.buffer.readline(limit)
        if line:
            req = json.loads(line)
            assert 'nsfw_max' not in req, 'caller still sent a per-image cutoff'
        return line
raise SystemExit(serve(Engine, stdin=Input(), out=sys.stdout.buffer,
                       on_eof=lambda: None, nsfw_max=.1))
''', encoding='utf-8')
    return SelfieWorker([sys.executable, '-I', str(script)])


def test_omitted_cutoff_uses_real_worker_policy_and_keeps_database_score(tmp_path, store):
    async def go():
        worker = policy_worker(tmp_path)
        await worker.start()
        try:
            got, tries = await render_slot(worker,
                lambda _: replace(request(tmp_path), nsfw_max=None),
                rid=lambda n: f'set:{n}', make_room=lambda: asyncio.sleep(0))
            captioned = await worker.caption(tmp_path/'base.jpg', top='HELLO', bottom='',
                                              out=tmp_path/'caption.jpg')
            return got, tries, captioned
        finally:
            await worker.stop()
    got, tries, captioned = run(go())
    assert got.flagged is False and got.nsfw == .05 and tries == 1
    assert captioned.flagged is True and captioned.nsfw == .15
    ensure_first_sets(store, [101], now=1.)
    candidates = [Candidate(i, 1234+i, tries, got.nsfw, got.prompt) for i in range(4)]
    mark_ready(store, 101, 1, candidates, now=2., night='2026-11-20', persona_version=None)
    rows = store.conn.execute('SELECT nsfw,prompt FROM selfie_candidate WHERE guid=101').fetchall()
    assert len(rows) == 4 and all(tuple(row) == (.05, got.prompt) for row in rows)


def test_omitted_cutoff_still_refuses_after_three_flagged_images(tmp_path):
    async def go():
        worker = policy_worker(tmp_path)
        await worker.start()
        try:
            with pytest.raises(SetRefused):
                await render_slot(worker, lambda _: replace(request(tmp_path), seed=7, nsfw_max=None),
                                  rid=lambda n: f'flag:{n}', make_room=lambda: asyncio.sleep(0))
        finally:
            await worker.stop()
    run(go())
    assert not (tmp_path/'c0.jpg').exists()


@pytest.mark.parametrize('operation', ['render', 'caption'])
@pytest.mark.parametrize('score', [float('nan'), float('inf'), -1, 1.01, True, None, 10**500])
def test_invalid_result_scores_end_the_worker_without_a_usable_result(tmp_path, operation, score):
    script = tmp_path/'bad_score.py'
    script.write_text(f'''
import json, sys
print(json.dumps({{'id': 'ready', 'ok': True, 'error': '', 'device': 'fake',
                  'free_gib': 20., 'versions': {{'fake': '1'}}}}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req.get('op') == 'quit': break
    print(json.dumps({{'id': req['id'], 'ok': True, 'error': '', 'flagged': False,
                      'nsfw': json.loads({json.dumps(score)!r}), 'prompt': 'test', 'tokens': 1,
                      'seconds': .1, 'peak_gib': 1., 'width': 768, 'height': 768}}), flush=True)
''', encoding='utf-8')

    async def go():
        worker = SelfieWorker([sys.executable, '-I', str(script)])
        await worker.start()
        try:
            with pytest.raises(WorkerError, match='^gone$'):
                if operation == 'render':
                    await worker.render(replace(request(tmp_path), nsfw_max=None))
                else:
                    await worker.caption(tmp_path/'base.jpg', top='HELLO', bottom='',
                                         out=tmp_path/'caption.jpg')
            assert not worker.running
        finally:
            await worker.stop()
    run(go())
