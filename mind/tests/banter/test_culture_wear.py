from banter.test_banter import rig
from leeroy_mind.banter.topics import Topic


def test_culture_banter_counts_delivery_and_leaves_dropped_allowance(rig):
    path=rig.engine.data_dir / "mind.toml"
    path.write_text("[server]\nenabled=true\n[culture]\nenabled=true\n")
    now=rig.clock.now()
    rig.store.conn.execute("INSERT INTO lexicon(id,kind,term,target,origin_ref,originator,born,last_used) "
        "VALUES(1,'joke','floor inspector',0,'chat:1',101,?,?)",(now-10*86400,now-10*86400))
    topic=Topic('joke:1','joke',now,'recorded')
    assert rig.banter.reserve(topic,now)
    rig.banter.settle_reservation(topic,0)
    assert rig.store.conn.execute("SELECT uses_total FROM lexicon").fetchone()[0]==0
    rig.sync(rig.advance(86401,86401))
    now=rig.clock.now()
    assert rig.banter.reserve(topic,now)
    rig.banter.settle_reservation(topic,1,17)
    row=rig.store.conn.execute("SELECT uses_total,last_used FROM lexicon").fetchone()
    assert tuple(row)==(1,now)
    assert rig.store.get_meta('culture:wear-counted:joke:1:17')=='1'
    assert rig.store.get_meta('banter:joke-use:joke:1') is None
