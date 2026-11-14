import pytest

from conftest import seed_test_cast

DAY = 86400.0


@pytest.fixture
def guild(store, clock):
    """The invented cast with three finished raids: 102 raids every time and holds the most DKP,
    104 came once, 103 sat on the bench twice."""
    personas = seed_test_cast(store)
    store.set_meta('human_guid', '1')
    now = clock.now()
    with store.transaction():
        for rid, day in ((1, 9), (2, 6), (3, 2)):
            store.conn.execute("INSERT INTO guild_raid VALUES (?,?,?,'icc',10,0,0,102,101,102,3,1,NULL)",
                               (rid, now - day * DAY, now - day * DAY + 3600))
        rows = [(1, 101, 'raided'), (2, 101, 'raided'), (3, 101, 'raided'),
                (1, 102, 'raided'), (2, 102, 'raided'), (3, 102, 'raided'),
                (1, 104, 'raided'), (2, 103, 'benched'), (3, 103, 'benched'), (1, 103, 'raided')]
        store.conn.executemany('INSERT INTO guild_attendance VALUES (?,?,?)', rows)
        store.conn.executemany("INSERT INTO dkp_ledger(ts,guid,delta,reason) VALUES (?,?,?,'kill')",
                               [(now - DAY, 102, 340), (now - DAY, 101, 100), (now - DAY, 104, 20)])
    return personas
