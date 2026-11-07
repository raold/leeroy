import json

import pytest

from conftest import run
from leeroy_mind.claims.check import check, judge
from leeroy_mind.nightly.llm import Reasoner
from leeroy_mind.testing.fake_llm import FakeLLM

from .test_store import VEC, put


@pytest.mark.parametrize('line,reason', [
    ('I hate that fight', 'polarity'), ('I hate Saurfang', 'polarity'),
    ("I don't like Saurfang", 'polarity'), ('I love Saurfang', None),
    ('I do not hate Saurfang', None), ('I hate coffee', None),
    ('She hates Saurfang', None), ('Brelva hates Saurfang', None),
    ('Do I hate Saurfang?', None), ('If I hated Saurfang', None),
    ('Brelva said "I hate Saurfang"', None),
])
def test_opinions(store, line, reason):
    cid = put(store)
    conflicts = check(store.conn, 101, line, VEC)
    assert [c.reason for c in conflicts] == ([reason] if reason else [])
    if reason:
        assert conflicts[0].claim_id == cid and conflicts[0].subject == 'saurfang'
    assert check(store.conn, 102, line, VEC) == []


@pytest.mark.parametrize('line,reason', [('I quit my job', 'replaced_fact'),
    ('I work at the library', 'replaced_fact'), ("I don't work at the bakery", 'negation'),
    ('I work at the bakery', None), ('I work at the bakery and enjoy raids', None)])
def test_facts(store, line, reason):
    put(store, kind='fact', subject='her job', text='works at the bakery', polarity=0)
    assert [c.reason for c in check(store.conn, 101, line, VEC)] == ([reason] if reason else [])


def test_judge_is_separate_and_read_only(store):
    put(store)
    conflict = check(store.conn, 101, 'I hate Saurfang', VEC)[0]
    llm = FakeLLM(lambda *_: json.dumps({'contradiction': True}))
    assert run(judge(Reasoner(llm, 'reflection'), 'I hate Saurfang', conflict))
    assert len(llm.calls) == 1 and llm.calls[0]['model'] == 'reflection'


def test_negated_opposite_is_a_compatible_restatement(store):
    put(store, text='loves Saurfang')
    assert check(store.conn, 101, "I don't hate Saurfang", VEC) == []


def test_extra_job_detail_is_compatible(store):
    put(store, kind='fact', subject='her job', text='works at the bakery', polarity=0)
    assert check(store.conn, 101, 'I work at the bakery on weekends', VEC) == []
