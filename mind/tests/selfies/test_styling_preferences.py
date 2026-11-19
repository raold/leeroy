"""Private outfit progression, pose/light recipes and reversible feedback, with fakes only."""
from dataclasses import replace

import pytest

from leeroy_mind.discord import store as discord
from leeroy_mind.learn.config import LearnCfg
from leeroy_mind.selfies import romance_styling as S, styling_history as H, words_scene as W
from leeroy_mind.selfies.scene import _text_codes, build_scene_prompt, scene_from, scene_json, scene_renderable
from leeroy_mind.selfies.snaps import mark_rendered, mark_sent
from leeroy_mind.signals.store import feedback
from nightly.conftest import IDENT
from selfies.fakes import planned_snap
from selfies.test_request_scene import app  # noqa: F401
from selfies.test_romance_styling import enable, original, styled
from selfies.test_selfie_prompt import woman
from selfies.test_words_scene import words


def sent(store, *, guid=101, owner=1, medium='dm', now=10, scene=None, audience='him'):
    scene = scene or scene_json(styled(woman(age=30, stance='lust'), category='going_out'))
    scene = {**scene, 'audience': audience}
    sid = planned_snap(store, guid, scene=scene, audience=audience, now=now)
    mark_rendered(store, sid, width=832, height=1216, seed=7, tries=1, nsfw=.01, prompt='', now=now)
    channel = f'dm:{guid}' if medium == 'dm' else 'whisper'
    with store.transaction():
        line = store.conn.execute("INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin) "
            "VALUES(?,?,?,?,?,'An invented photo','mind')", (now, channel, guid, 'Testone', owner)).lastrowid
    assert mark_sent(store, sid, sender=guid, channel=medium, line_id=line, now=now)
    mid = None
    if medium == 'dm':
        mid = discord.post(store, channel=channel, author=guid, origin='mind', text='An invented photo',
            now=now, attachment=discord.AttachmentIn('snap', f'selfies/{guid}/snaps/{sid}.jpg',
                                                    832, 1216, 100, sid))
        store.set_meta(f'discord:line:{mid}', str(line))
    return sid, line, mid


def read(store, **kw):
    return H.read(store, 101, 1, **(dict(now=100, learn=LearnCfg(apply=True)) | kw))


def test_higher_romance_draws_more_confident_clothes_within_activity_categories():
    warm, hot = woman(age=30, stance='fond'), woman(age=30, stance='lust')
    for category in ('casual', 'going_out', 'gym', 'outdoors', 'warm'):
        ranks = [[S.boldness(S.template_of(styled(p, category=category, seed=i, dial=10).outfit))
                  for i in range(200)] for p in (warm, hot)]
        assert sum(ranks[1]) > sum(ranks[0]) + 80, category
    # The dial remains authoritative at the strongest stance.
    assert styled(hot, dial=2).romance_style == 'warm'


def test_recent_templates_are_avoided_even_in_a_different_colour():
    previous = [scene_json(styled(seed=i)) for i in range(8)]
    history = H.History(tuple(previous))
    old = {S.template_of(s['outfit']) for s in previous}
    assert all(S.template_of(styled(seed=i, history=history).outfit) not in old for i in range(60))
    assert styled(history=history) == styled(history=history)


def test_current_votes_bias_choices_without_removing_exploration():
    dress = '{colour} bodycon midi dress'
    suit = '{colour} tailored trouser suit'
    like = H.History(rated=tuple(({'outfit': 'a red bodycon midi dress'}, 1) for _ in range(6)))
    dislike = H.History(rated=tuple(({'outfit': 'a red bodycon midi dress'}, -1) for _ in range(6)))
    counts = []
    for history in (like, dislike):
        counts.append(sum(S._choose((dress, suit), i, 'outfit', history, tone='playful') == dress
                          for i in range(300)))
    assert counts[0] > counts[1] + 80
    assert 0 < counts[1] < counts[0] < 300


def test_new_pose_and_light_phrases_pass_screens_and_unknown_codes_are_held():
    p = woman(age=30, stance='drawn')
    s = styled(p)
    for phrase in (*S.POSES.values(), *S.LIGHTS.values()):
        assert not _text_codes('pose', phrase, ident=IDENT, names=[])
    for invalid in (replace(s, romance_style=['warm']), replace(s, romance_style=False),
                    replace(s, romance_pose='free text'), replace(s, romance_light=['lamp']),
                    replace(s, romance_style='', romance_pose='direct'),
                    replace(s, audience='anyone', romance_pose='direct')):
        assert scene_renderable(p, invalid) == 'scene.styling: held'
    assert scene_from(scene_json(s)) == s
    renamed = replace(p, identity=replace(p.identity, real_name='Golden'))
    assert scene_renderable(renamed, replace(s, romance_light='golden')) == 'scene.styling: named'


def test_enum_lighting_matches_place_and_time_and_follows_face_details():
    p = woman(age=30, stance='lust')
    s = styled(p, original(kind='selfie', when='evening', setting='a park', activity=''))
    assert s.romance_light == 'golden'
    spec = build_scene_prompt(p, s)
    assert (S.LIGHTS['golden'], 3) in spec.phrases
    assert spec.phrases.index(('hazel eyes', 2)) < spec.phrases.index((S.LIGHTS['golden'], 3))
    assert S.LIGHTS['golden'] in s.shows


def test_requested_camera_and_light_are_respected_and_face_stays_first(app):
    enable(app, stance='lust', dial=10)
    poses = set()
    for seed in range(30):
        s = words(app, {'wearing': 'going_out', 'framing': 'timer', 'moment': '',
                        'lighting': 'lamp', 'prop': None}, request=seed)
        poses.add(s.get('romance_pose'))
        assert s['romance_light'] in ('soft_lamp', 'side_lamp')
        spec = W.build_words_prompt(app.personas.get(101), s)
        assert spec.phrases.index(('hazel eyes', 2)) < spec.phrases.index((S.POSES[s['romance_pose']], 2))
        assert (S.LIGHTS[s['romance_light']], 3) in spec.phrases
    assert 'glance' in poses
    s = words(app, {'wearing': 'casual', 'framing': W.BEHIND, 'lighting': 'neon'})
    assert 'romance_pose' not in s and 'romance_light' not in s
    assert (W.LIGHT_WORDS['neon'], 2) in W.build_words_prompt(app.personas.get(101), s).phrases


def test_silence_and_another_members_reaction_give_no_preference(store):
    _, _, mid = sent(store)
    discord.react(store, mid, member=102, emoji='👍', now=20)
    before = store.conn.total_changes
    history = read(store)
    assert len(history.recent) == 1 and not history.rated
    assert store.conn.total_changes == before


def test_owner_reaction_is_reversible_and_control_vote_takes_precedence(store):
    _, line, mid = sent(store)
    discord.react(store, mid, member=1, emoji='👍🏽', now=20)
    assert read(store).rated[0][1] == 1
    with store.transaction():
        feedback(store.conn, {'line_id': line, 'value': -1, 'reasons': []}, 1, 30)
    assert read(store).rated[0][1] == -1
    with store.transaction():
        store.conn.execute('DELETE FROM talk_signal WHERE line_id=?', (line,))
    assert read(store).rated[0][1] == 1
    discord.unreact(store, mid, member=1, emoji='👍🏽', now=40)
    assert not read(store).rated


def test_whisper_and_dm_photo_feedback_have_the_same_credit(store):
    for medium in ('whisper', 'dm'):
        _, line, _ = sent(store, medium=medium)
        with store.transaction():
            feedback(store.conn, {'line_id': line, 'value': 1, 'reasons': []}, 1, 20)
    assert [v for _, v in read(store).rated] == [1, 1]


@pytest.mark.parametrize('learn', [LearnCfg(), LearnCfg(apply=True, freeze=True),
                                 LearnCfg(apply=True, frozen_members=(101,))])
def test_learning_controls_stop_preference_bias_but_keep_repeat_avoidance(store, learn):
    _, _, mid = sent(store)
    discord.react(store, mid, member=1, emoji='👍', now=20)
    history = read(store, learn=learn)
    assert history.recent and not history.rated


def test_lab_global_freeze_is_effective_immediately(store):
    _, _, mid = sent(store)
    discord.react(store, mid, member=1, emoji='👍', now=20)
    assert read(store).rated
    store.set_meta('learn:control:freeze', 'true')
    assert not read(store).rated


@pytest.mark.parametrize('tamper', ['delete', 'receipt', 'channel', 'author', 'recipient', 'attachment'])
def test_no_credit_or_recent_history_without_exact_private_delivery(store, tamper):
    sid, line, mid = sent(store)
    discord.react(store, mid, member=1, emoji='👍', now=20)
    with store.transaction():
        if tamper == 'delete':
            discord.delete(store, mid, author=101, now=30)
        elif tamper == 'receipt':
            store.set_meta(f'discord:line:{mid}', str(line + 1))
        elif tamper == 'channel':
            store.conn.execute("UPDATE discord_message SET channel='pics' WHERE id=?", (mid,))
        elif tamper == 'author':
            store.conn.execute('UPDATE discord_message SET author=102 WHERE id=?', (mid,))
        elif tamper == 'recipient':
            store.conn.execute('UPDATE chat_line SET to_guid=102 WHERE id=?', (line,))
        else:
            store.conn.execute('DELETE FROM discord_attachment WHERE message=?', (mid,))
    assert read(store) == H.History()


def test_public_other_member_future_and_old_photos_do_not_enter_history(store):
    for kw in ({'guid': 102}, {'audience': 'anyone'}, {'now': 110}, {'now': -H.WINDOW}):
        _, _, mid = sent(store, **kw)
        discord.react(store, mid, member=1, emoji='👍', now=20)
    assert read(store) == H.History()


def test_reader_bounds_history_and_reads_latest_votes_in_live_request(app):
    enable(app)
    app.engine.learn_settings = LearnCfg(apply=True)
    for i in range(H.LIMIT + 2):
        sent(app.store, medium='whisper', now=app.clock.now() - 1,
             scene=scene_json(styled(seed=i)))
    history = H.read(app.store, 101, 1, now=app.clock.now(), learn=app.engine.learn_settings)
    assert len(history.recent) == H.RECENT
    s = words(app, {'wearing': 'casual'})
    assert S.template_of(s['outfit']) not in {S.template_of(x['outfit']) for x in history.recent}
