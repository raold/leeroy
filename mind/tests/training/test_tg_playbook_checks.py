"""[training] playbooks: stored pulls checked under version 1 are rechecked once with the raid
playbooks' version 2 rows; with the switch off nothing is rechecked."""

import asyncio
from dataclasses import replace

from leeroy_mind.training import protocol as p, store
from leeroy_mind.training.checks import rescore
from leeroy_mind.training.ledger import PullContext
from leeroy_mind.training.service import rescore_stored

from training.test_tg_checks import member, record


class Book:
    def ids(self, name):
        return frozenset((7,)) if name == "Mortal Wound" else frozenset((8,))


def _gluth(fake):
    rec = record(member(debuffs=(p.DebuffTotal(7, 1, 9),)), map_id=533, boss=2)
    rec = replace(rec, header=replace(rec.header, boss_entry=15932))
    store.put_pull(fake.conn, rec, PullContext("offline", 0, None, None, None, "", {}), {1101: "tank"}, received=1)


def test_checks_rescore_moves_to_version_two_only_when_on(fake):
    _gluth(fake)
    assert asyncio.run(rescore(fake, Book())) == 1
    row = store.pull_rows(fake.conn)[0]
    assert row["checks_version"] == 1 and row["failed_mechanics"] == 0
    assert asyncio.run(rescore(fake, Book())) == 0
    assert asyncio.run(rescore(fake, Book(), playbooks=True)) == 1
    row = store.pull_rows(fake.conn)[0]
    assert row["checks_version"] == 2 and row["failed_mechanics"] == 1
    assert [tuple(r) for r in fake.conn.execute("SELECT kind,key,count FROM tg_failure WHERE count>0")] == [
        ("debuff", "Mortal Wound", 4)]
    assert asyncio.run(rescore(fake, Book(), playbooks=True)) == 0


def test_rescore_stored_follows_the_config(fake):
    _gluth(fake)
    assert asyncio.run(rescore_stored(fake, Book())) == 1
    assert store.pull_rows(fake.conn)[0]["checks_version"] == 1
    fake.cfg = replace(fake.cfg, playbooks=True)
    assert asyncio.run(rescore_stored(fake, Book())) == 1
    assert store.pull_rows(fake.conn)[0]["checks_version"] == 2
    assert asyncio.run(rescore_stored(fake, Book())) == 0
