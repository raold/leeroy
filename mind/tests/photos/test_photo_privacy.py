import asyncio
import pytest
from conftest import run
from photos.test_send import setup, stock, reply, ON
from photos.test_pipeline_photo import finish
from leeroy_mind.discord import store as dstore
from leeroy_mind.photos.notes import by_messages, by_lines
from leeroy_mind.selfies.scene_choices import compose_choices
from selfies.test_scene_choices import PICKS
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

SCENE='a copperleaf lantern beside the orchard gate'


def test_private_photo_is_remembered_in_her_whisper_and_nowhere_else(tmp_path):
    async def go():
        app=setup(tmp_path,ON.replace('selfies=3','selfies=3\naware=true'))
        stock(app,scene={**compose_choices(PICKS),'shows':SCENE})
        app.llm._script=lambda *a:'this seemed worth sharing'
        try:
            turn,meta=reply(app)
            assert await finish(app,asyncio.create_task(app.engine.speak_meta(turn,meta)))
            mid=app.store.conn.execute('SELECT message FROM discord_attachment').fetchone()[0]
            assert SCENE in str(app.discord_talk.context_lines(app.store.conn,'dm:101',None))
            for speaker,channel,to in [(101,'whisper',app.human_guid),(101,'dm:101',app.human_guid),
                                      (102,'whisper',app.human_guid),(102,'dm:102',app.human_guid),
                                      (101,'guild',0),(101,'party',0),(101,'officer',0),
                                      (101,'discord:general',0),(101,'discord:officer-chat',0),
                                      (101,'whisper',102)]:
                turn=Turn(TurnKind.OPEN,speaker,channel,to_guid=to,addressee=to)
                meta=TurnMeta('privacy',why='reply:whisper' if channel=='whisper' else 'test')
                composed=await app.engine._compose_prompt(turn,meta,live=False)
                card=app.engine.live_review.block(turn,meta,close=False)
                visible=speaker==101 and to==app.human_guid and channel in ('whisper','dm:101')
                assert (SCENE in str(composed.messages))==visible,(speaker,channel,composed.messages)
                assert (SCENE in str(card))==visible,(speaker,channel,card)
                if channel=='whisper' and visible:
                    assert 'Earlier between you two, elsewhere' in str(composed.messages)
                    assert 'Discord DM to' in str(composed.messages)
            dstore.delete(app.store,mid,author=101,now=app.clock.now())
            assert not by_messages(app.store.conn,[mid],members=True)
        finally:await app.stop()
    run(go())


@pytest.mark.parametrize('repair_receipt',[False,True])
def test_private_snap_cannot_label_an_unrelated_public_chat_line(tmp_path,repair_receipt):
    async def go():
        app=setup(tmp_path);sid=stock(app,scene={**compose_choices(PICKS),'shows':SCENE})
        app.llm._script=lambda *a:'this seemed worth sharing'
        try:
            assert await finish(app,asyncio.create_task(app.engine.speak_meta(*reply(app))))
            mid=app.store.conn.execute('SELECT message FROM discord_attachment').fetchone()[0]
            with app.store.transaction():
                cur=app.store.conn.execute("INSERT INTO chat_line(ts,from_guid,from_name,to_guid,channel,text,origin) "
                    "VALUES(?,101,'Invented',0,'guild','hello everyone','mind')",(app.clock.now(),))
                line=cur.lastrowid
                app.store.conn.execute('UPDATE selfie_snap SET line_id=? WHERE id=?',(line,sid))
                if repair_receipt:app.store.set_meta(f'discord:line:{mid}',str(line))
            assert not by_lines(app.store.conn,[line])
            assert not by_messages(app.store.conn,[mid],members=True)
        finally:await app.stop()
    run(go())


def test_mislinked_private_attachment_is_never_channel_evidence(tmp_path):
    async def go():
        app=setup(tmp_path);stock(app,scene={**compose_choices(PICKS),'shows':SCENE})
        app.llm._script=lambda *a:'this seemed worth sharing'
        try:
            assert await finish(app,asyncio.create_task(app.engine.speak_meta(*reply(app))))
            mid=app.store.conn.execute('SELECT message FROM discord_attachment').fetchone()[0]
            with app.store.transaction():
                app.store.conn.execute("UPDATE discord_message SET channel='general' WHERE id=?",(mid,))
            assert not by_messages(app.store.conn,[mid],members=True)
            assert SCENE not in str(app.discord_talk.context_lines(app.store.conn,'general',None))
        finally:await app.stop()
    run(go())
