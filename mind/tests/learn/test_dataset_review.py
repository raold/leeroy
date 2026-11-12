import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from tests.learn.test_dataset import data,add,pair,mine
from tests.learn.test_recipe import rig
from tests.claims.conftest import context
from leeroy_mind.learn import capture,dataset,offline_score,night
from leeroy_mind.learn.cli import learn_main
from leeroy_mind.learn.config import load_learn_config
from leeroy_mind.learn.weights import ensure
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.guards import Draft,GuardStores,GuardsCfg,record_sent,review
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn,TurnKind


def test_default_scratch_inside_checkout_and_other_paths_refused(data,monkeypatch,capsys):
    (data.root/'.git').mkdir()
    pair(data,offset=18*dataset.DAY)
    monkeypatch.setattr('leeroy_mind.learn.preference_cli.time.time',lambda:data.now)
    assert learn_main(['evalset','--data-dir',str(data.root)])==0
    assert list((data.root/'scratch').glob('learn-eval-*/pairs.jsonl'))
    assert not list((data.root/'scratch').glob('.learn-snapshot-*'))
    assert learn_main(['export','--data-dir',str(data.root),'--out',str(data.root/'elsewhere')])==1
    assert 'learn_output_in_repository' in capsys.readouterr().err


def test_scratch_rejects_junction(tmp_path):
    import subprocess
    root=tmp_path/'data'; root.mkdir()
    outside=tmp_path/'other'; outside.mkdir()
    link=root/'scratch'
    if __import__('os').name=='nt':
        result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True)
        if result.returncode: pytest.skip('junction unavailable')
    else:
        link.symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match='learn_output_linked'):
        dataset.output_dir(link/'private',data_dir=root)


def test_snapshot_cleanup_after_error(data):
    scratch=dataset.output_dir(data.root/'scratch',data_dir=data.root)
    with pytest.raises(RuntimeError):
        with dataset.snapshot(data.root,scratch) as conn:
            assert list(scratch.glob('.learn-snapshot-*/mind.db'))
            raise RuntimeError('fixture')
    assert not list(scratch.glob('.learn-snapshot-*'))


@pytest.mark.parametrize('channel',['say','yell','emote'])
def test_local_context_requires_zone_and_stored_visibility(data,channel):
    ids=pair(data,channel=channel)
    foreign=add(data,'unheard in another zone',channel=channel,ts=data.before-.2,guid=102)
    hidden=add(data,'unheard outside audience',channel=channel,ts=data.before-.1,guid=102,audience=[1,102])
    data.store.conn.execute('UPDATE chat_line SET zone=7 WHERE id=?',(foreign,))
    rows=dataset.context_rows(data.store.conn,data.store.conn.execute('SELECT * FROM chat_line WHERE id=?',(ids[0],)).fetchone(),1)
    assert rows and not {foreign,hidden}&{r['id'] for r in rows}


@pytest.mark.parametrize('channel',['discord:general','dm:101'])
def test_discord_ids_resolve_to_parent_vectors_and_reward_pairs(data,channel):
    ids=pair(data,channel=channel)
    data.store.conn.execute('DELETE FROM talk_signal')
    version=ensure(data.store.conn,now=data.before).version
    for i,lid in enumerate(ids):
        parent=data.store.conn.execute('SELECT * FROM chat_line WHERE id=?',(lid-1,)).fetchone()
        mid=dstore.post(data.store,channel=channel.removeprefix('discord:'),author=1,origin='human',
                        text=parent['text'],now=parent['ts'])
        assert mid!=parent['id'] or i==0  # sequences diverge after ordinary game traffic
        data.store.set_meta(f'discord:line:{mid}',str(parent['id']))
        capture.delivered(data.store.conn,lid,TurnMeta('reply',discord=DiscordTarget(mid,None)))
        if channel=='discord:general':
            data.store.conn.execute('UPDATE chat_line SET to_guid=0 WHERE id=?',(lid,))
        capture.vectors(data.store.conn,[NS(id=mid,text=parent['text'])],{mid:pack_vec([1.]+[0.]*511)},'fixture',channel=channel)
        data.store.conn.execute("INSERT INTO talk_reward(line_id,guid,value,parts,weights_version,scored_at) VALUES (?,101,?,'{}',?,?)",
                                (lid,1 if i==0 else -1,version,data.before+300))
    pairs,indices,_=mine(data,include_private=True)
    assert len(pairs)==1 and pairs[0]['source']=='reward'
    scored=offline_score.score(data.store.conn,indices,[{'id':indices[0]['id'],'text':'A reply.'}],owner=1,
                               model_hash=capture.digest('fixture'),include_private=True)
    assert scored['coverage']['parent_embeddings']==1


def test_rations_exclude_own_and_later_receipts(data):
    ids=pair(data)
    phrase='Copper lantern fans enjoy sailing'
    settings=GuardStores(data.store.conn,GuardsCfg(enabled=True))
    for i in range(3):
        lid=add(data,phrase,ts=data.before+10+i)
        record_sent(settings,Draft(101,'guild',phrase,now=data.before+10+i),receipt_id=f'line-{lid}')
    row=data.store.conn.execute('SELECT * FROM chat_line WHERE id=?',(ids[0],)).fetchone()
    candidate=Draft(101,'guild',phrase,now=row['ts'],line_id=row['id'])
    assert 'stock_phrase_ration' in review(settings,candidate).codes
    ledger=list(data.store.conn.execute("SELECT key,value FROM meta WHERE key LIKE 'guards:sent:%'"))
    _,indices,_=mine(data)
    scored=offline_score.score(data.store.conn,indices,[{'id':indices[0]['id'],'text':phrase}],owner=1,model_hash='fixture')
    assert 'stock_phrase_ration' not in scored['results'][0]['codes']
    assert list(data.store.conn.execute("SELECT key,value FROM meta WHERE key LIKE 'guards:sent:%'"))==ledger


def test_install_hash_stable_between_exports_and_not_exported(data):
    pair(data)
    pair(data,offset=18*dataset.DAY)
    training=mine(data)[0]
    evaluation=dataset.mine(data.store.conn,data.ident,since=data.now-7*dataset.DAY,until=data.now)[0]
    assert training[0]['member_guid_hash']==evaluation[0]['member_guid_hash']
    key=dataset.member_key(data.store.conn)
    assert key.hex() not in json.dumps(training+evaluation)
    assert dataset.member_key(data.store.conn)==key


def test_night_prunes_private_evidence_at_90_days(context):
    async def go():
        async with context(learn_enabled=True) as (ctx,_):
            fixture=NS(store=ctx.store,before=ctx.clock.now()-91*dataset.DAY)
            old=add(fixture,'old draft')
            fresh=add(fixture,'fresh draft',ts=ctx.clock.now()-89*dataset.DAY)
            for lid in (old,fresh):
                ctx.store.conn.execute('INSERT INTO learn_rewrite VALUES (?,?,?)',(str(lid),lid,'private original'))
                ctx.store.conn.execute('INSERT INTO learn_rewrite_line VALUES (?,?,?)',(lid,str(lid),'plain chosen'))
                capture.vectors(ctx.store.conn,[NS(id=lid,text='old draft' if lid==old else 'fresh draft')],
                                {lid:pack_vec([1.]+[0.]*511)},'fixture')
            await night.stage_rewards(ctx)
            assert [r[0] for r in ctx.store.conn.execute('SELECT first_line_id FROM learn_rewrite')]==[fresh]
            assert [r[0] for r in ctx.store.conn.execute('SELECT line_id FROM learn_line_vector')]==[fresh]
            assert [r[0] for r in ctx.store.conn.execute('SELECT line_id FROM learn_rewrite_line')]==[fresh]
    asyncio.run(go())


@pytest.mark.parametrize('value',['1','"true"','[]','{}'])
def test_capture_config_invalid_disables_learning(tmp_path,value):
    path=tmp_path/'mind.toml'; path.write_text('[learn]\nenabled=true\ncapture_preferences='+value)
    assert not load_learn_config(path).enabled


def test_capture_failure_keeps_recipe_and_rolls_back_only_capture(rig,monkeypatch,caplog):
    rig.engine.learn_enabled=rig.engine.learn_capture_preferences=True
    def fail(conn,lid,meta):
        conn.execute('INSERT INTO learn_rewrite VALUES (?,?,?)',(str(lid),lid,'private draft'))
        raise ValueError('private exception')
    monkeypatch.setattr(capture,'delivered',fail)
    lid=rig.engine._record(Turn(TurnKind.REPLY_HUMAN,101,'guild'),TurnMeta('capture-fails'),
                          rig.engine.personas.get(101),'fixture',[])
    assert rig.store.conn.execute('SELECT 1 FROM talk_recipe WHERE line_id=?',(lid,)).fetchone()
    assert not rig.store.conn.execute('SELECT * FROM learn_rewrite').fetchall()
    assert 'learn_preference_capture_failed' in caplog.text and 'private exception' not in caplog.text
