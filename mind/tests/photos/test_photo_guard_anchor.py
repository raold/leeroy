import asyncio
from conftest import run
from photos.test_send import setup, stock, ON
from photos.test_pipeline_photo import finish
from selfies.test_scene_choices import PICKS
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind


def test_dm_photo_uses_actual_scene_as_anchor_for_enabled_server_guards(tmp_path):
    async def go():
        app=setup(tmp_path,ON+'\n[guards]\nenabled=true\n')
        scene=compose_choices({**PICKS,'activity':'cooking','setting':'kitchen','prop':'none'})
        stock(app,scene=scene)
        caption='my kitchen experiment means stirring a pot of soup again tonight'
        app.llm._script=lambda *a:caption
        original='Officers voting on heroic raid loot distribution policies'
        turn=Turn(TurnKind.OPEN,101,'dm:101',to_guid=app.human_guid,addressee=app.human_guid)
        try:
            # Real guard code and the normal FakeLLM word vectors: the unrelated
            # old opener subject correctly asks for a rewrite before the photo.
            control=await app.engine.live_review.check(turn,TurnMeta('control',why='server:dm',server_anchor=original),caption)
            assert 'off_anchor' in control.codes
            checks=[]
            check=app.engine.live_review.check
            async def observed(*args,**kwargs):
                verdict=await check(*args,**kwargs)
                checks.append(verdict)
                return verdict
            app.engine.live_review.check=observed
            meta=TurnMeta('photo-anchor',why='server:dm',server_anchor=original,topic=original)
            assert len(await finish(app,asyncio.create_task(app.engine.speak_meta(turn,meta))))==1
            assert meta.server_anchor==scene['shows']
            assert checks and all(v.action=='send' for v in checks)
            assert all(score.value>=.35 for v in checks for score in v.scores if score.code=='anchor_relevance')
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0]==1
        finally:await app.stop()
    run(go())
