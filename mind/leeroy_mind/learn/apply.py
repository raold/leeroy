"""Bounded live style application. All wording is data in the user message, never rules."""
from dataclasses import asdict, dataclass, field, replace

from leeroy_mind.discord.channels import is_discord, is_private, is_officer_channel
from leeroy_mind.bond.policy import eligible as bond_eligible
from leeroy_mind.fingerprint import measure, seed, render_style_hint
from leeroy_mind.fingerprint.config import enabled as fingerprint_enabled
from leeroy_mind.memory.callbacks import CallbackContext, load_callbacks_config, pick_callback, record_callback
from leeroy_mind.personas.stances import is_woman
from leeroy_mind.prompts import note_template
from leeroy_mind.tuning.effective import dials_of
from . import arms, audience, bandit, lessons
from .config import LearnCfg
from .control import effective, vetoed
from .recipe import Snapshot

LESSON_HEAD = 'Style observation: '
CALLBACK_HEAD = 'Something you might bring up if it fits: '


@dataclass
class Applied:
    context: object
    selected: dict
    entries: list = field(default_factory=list)  # (kind, identifier, reviewed text)
    num_predict: int | None = None
    callback: object = None
    callback_context: object = None
    used: tuple = ()


def active(engine, guid):
    cfg = getattr(engine, 'learn_settings', LearnCfg())
    if not getattr(engine, 'learn_enabled', False) or not cfg.enabled or not cfg.apply:
        return None
    cfg = effective(engine.store.conn, cfg)
    return cfg if not cfg.freeze and guid not in cfg.frozen_members else None


def render_arm(arm, *, humour, forward, romance, female, rng):
    """Closed parameters to reviewed phrases; dials are enforced after selection."""
    if arm.status == 'control' or not arms.valid(arm.family, arm.params):
        return '', None
    p = arms.bounded(arm, humour=humour, forward=min(forward, romance) if female else 0)
    family = arm.family
    if family == 'length':
        lo, hi = p['min_words'], p['max_words']
        return f'This reply usually fits in {lo}-{hi} words; never pad it.', min(240, max(32, hi*2+16))
    if family == 'questions':
        return ('One brief, relevant question can fit, if there is something real to ask.'
                if rng.random() < p['rate'] else 'Let this reply stand without adding a new question.'), None
    if family == 'humour':
        return {'none':'A straightforward reply fits; no added joke.',
                'light':'A little light humour can fit when the moment supports it.',
                'playful':'A playful observation can fit; never force a joke.'}[p['level']], None
    if family == 'tease':
        return ('Gentle teasing of the owner can fit if welcome; stay within your current boundaries.'
                if p['level'] == 'light' else 'No added teasing in this reply.'), None
    if family == 'followup':
        # This shapes this generation, not another scheduled turn. No arm can
        # override a single-line job, combat gate, deadline or delivery cadence.
        return ('If one small follow-up thought belongs, include it in this same reply, within its line limit.'
                if rng.random() < p['rate'] else 'One complete thought is enough; leave room for an answer.'), None
    # Callback/typing choices are materialized from scoped data separately.
    return '', None


def scoped_profile(conn, guid, persona, *, channel, to_guid, readers, officer):
    rows = conn.execute("SELECT id,text FROM chat_line WHERE from_guid=? AND origin='mind' "
                        "ORDER BY ts DESC,id DESC LIMIT 300", (guid,)).fetchall()
    lines = [r['text'] for r in rows if audience.visible(conn, audience.evidence(conn, [r['id']]),
             guid=guid, channel=channel, to_guid=to_guid, readers=readers, officer=officer)]
    dials = dials_of(persona)
    return measure(lines) if len(lines) >= 50 else seed(asdict(persona), samples=len(lines),
        talkative=dials.value('talkative',persona), humour_dial=dials.value('humour',persona))


async def prepare(engine, turn, meta, persona, *, now):
    cfg = active(engine, turn.speaker)
    if cfg is None or turn.template is not None:
        return None
    dials = dials_of(persona)
    path = engine.data_dir/'mind.toml'
    # Loaded once per engine/start, just like the learning switch itself.
    if not hasattr(engine, '_learn_features'):
        engine._learn_features = (load_callbacks_config(path), fingerprint_enabled(path))
    callbacks, fingerprint = engine._learn_features
    member_romance = persona.romance.dial
    romance = min(engine.cfg.romance.dial, engine.cfg.romance.dial if member_romance is None else member_romance)
    context = bandit.Context(turn.channel, persona.romance.stance if is_woman(persona) else 'none',
        int(dials.value('talkative', persona)), cfg, is_woman(persona),
        int(dials.value('humour', persona)), int(dials.value('forward', persona)),
        romance, callbacks, fingerprint)
    with engine.store.transaction():
        selected = bandit.choose(engine.store.conn, turn.speaker, context, rng=engine.rng, now=now)
        registry = {a.id:a for a in arms.read(engine.store.conn)}
    result = Applied(context, selected)
    readers = tuple(engine.world.audience(turn.channel, from_guid=turn.speaker, to_guid=turn.to_guid))
    officer = bool(engine.world.is_officer(turn.speaker))
    # WorldView's game audience intentionally lists cast only. Callback context
    # additionally observes him when he can actually hear this conversation.
    callback_readers = set(readers)
    human = engine.human.guid
    if is_private(turn.channel):
        callback_readers = {turn.speaker, turn.to_guid}
    elif not is_discord(turn.channel) and human and engine.world.human_online:
        if turn.channel == 'guild' or is_officer_channel(turn.channel) and engine.human.is_guild_master:
            callback_readers.add(human)
        elif turn.channel in ('party','raid','raid_warning') and engine.world.in_group(human):
            callback_readers.add(human)
    for family, aid in selected.items():
        arm = registry.get(aid)
        if arm is None or family in cfg.frozen_families:
            continue
        text, predict = render_arm(arm, humour=context.humour, forward=context.forward,
                                  romance=context.romance, female=context.female, rng=engine.rng)
        if text:
            result.entries.append(('arm', aid, text))
        if predict is not None:
            result.num_predict = predict
    def read(conn):
        notes = []
        for note in lessons.read(conn, guid=turn.speaker, now=now):
            if note['status'] != 'active' or vetoed(conn, 'lesson', note['id']):
                continue
            if not audience.visible(conn, audience.saved(conn, note['id']), guid=turn.speaker,
                    channel=turn.channel, to_guid=turn.to_guid, readers=readers, officer=officer):
                continue
            checked = lessons.screen(dict(text=note['text'], scope=note['scope'],
                non_manipulative=True, forward_min=audience.forward_floor(conn,note['id'])), scope=note['scope'], persona=persona,
                ident=engine.human, world_romance=engine.cfg.romance.dial)
            if checked:
                notes.append(('lesson', note['id'], LESSON_HEAD+checked['text']))
            if len(notes) >= 5:
                break
        typing = registry.get(selected['typing'])
        profile = scoped_profile(conn, turn.speaker, persona, channel=turn.channel,
            to_guid=turn.to_guid, readers=readers, officer=officer) if (fingerprint and typing
                and typing.params.get('strength',0)>0 and 'typing' not in cfg.frozen_families) else None
        cb_context = CallbackContext(callbacks, turn.channel, turn.to_guid,
            tuple(g for g in callback_readers if g != turn.speaker), tuple(callback_readers),
            bond_enabled=engine.cfg.romance.activities and bond_eligible(persona, engine.cfg.romance.dial))
        arm = registry.get(selected['callback'])
        callback = pick_callback(conn, turn.speaker, cb_context, now=now) if (
            callbacks and 'callback' not in cfg.frozen_families and arm is not None
            and arm.params.get('enabled') == 1) else None
        return notes, profile, cb_context, callback
    notes, profile, cb_context, callback = await engine.live_review.read(read)
    # An owner can freeze while the snapshot worker runs. Do not apply stale data.
    current = active(engine, turn.speaker)
    if current is None:
        return None
    for family in current.frozen_families:
        result.selected[family] = arms.controls()[family]
    result.entries = [entry for entry in result.entries if registry[entry[1]].family not in current.frozen_families]
    if 'length' in current.frozen_families:
        result.num_predict = None
    # The owner can expire/veto a note while its detached read is running.
    notes = [entry for entry in notes if engine.store.conn.execute(
        "SELECT 1 FROM talk_lesson WHERE id=? AND guid=? AND status='active' AND expires>?",
        (entry[1],turn.speaker,now)).fetchone() and not vetoed(engine.store.conn,'lesson',entry[1])]
    result.entries.extend(notes)
    typing = registry.get(result.selected['typing'])
    if fingerprint and typing and typing.params.get('strength', 0) > 0 and profile:
        hint = render_style_hint(profile)
        if typing.params['strength'] < 1:
            hint = 'A light touch of your usual typing style: '+hint
        result.entries.append(('arm', typing.id, hint))
    if callback and 'callback' not in current.frozen_families:
        holds = getattr(engine, '_learn_callback_holds', {})
        engine._learn_callback_holds = holds
        # In-flight offers count against the quotas too; failed/cancelled turns
        # release their holds, and only an acknowledged line records use.
        from leeroy_mind.memory.callbacks import _play_floor
        floor = _play_floor(engine.store.conn, now)
        uses = engine.store.conn.execute('SELECT guid FROM callback_use WHERE ts BETWEEN ? AND ?',
                                        (floor, now)).fetchall() if floor is not None else []
        if (floor is not None and len(holds)+len(uses) < 3
                and turn.speaker not in holds.values() and all(r[0] != turn.speaker for r in uses)):
            holds[meta.turn_id] = turn.speaker
            result.callback, result.callback_context = callback, cb_context
            result.entries.append(('callback', callback.memory_id,
                CALLBACK_HEAD+callback.seed))
    return result


def used(meta, count):
    applied = meta.learning
    if applied is None:
        return
    applied.used = tuple(applied.entries[:count])
    if applied.used:
        from leeroy_mind.talk.prompt import STYLE_HEAD
        note_template('learn.style.v1', STYLE_HEAD)
    for kind, ident, text in applied.used:
        if kind == 'arm':  # renderer output contains reviewed words/numbers only
            family = next(f for f, aid in applied.selected.items() if aid == ident)
            note_template(f'learn.arm.{family}.v1', text)
        elif kind == 'lesson':
            note_template('learn.lesson.v1', LESSON_HEAD)
        elif kind == 'callback':
            note_template('learn.callback.v1', CALLBACK_HEAD)
    # A trimmed arm did not shape the model; attribute its control instead.
    used_arms = {i for kind,i,_ in applied.used if kind == 'arm'}
    selected = dict(applied.selected)
    for family, aid in selected.items():
        if aid != arms.controls()[family] and aid not in used_arms:
            if family != 'callback' or not any(k == 'callback' for k,_,_ in applied.used):
                selected[family] = arms.controls()[family]
    if selected['length'] == arms.controls()['length']:
        applied.num_predict = None
    applied.selected = selected
    if isinstance(meta.recipe_snapshot, Snapshot):
        meta.recipe_snapshot = replace(meta.recipe_snapshot, arm_ids=tuple(selected.values()),
            bandit_context=applied.context.key,
            lesson_ids=tuple(i for k,i,_ in applied.used if k == 'lesson'),
            callback_id=next((i for k,i,_ in applied.used if k == 'callback'), None),
            num_predict=applied.num_predict)


def record(engine, turn, meta):
    applied = meta.learning
    if applied and applied.callback and any(k == 'callback' for k,_,_ in applied.used):
        record_callback(engine.store.conn, turn.speaker, applied.callback_context,
                        applied.callback, now=engine.clock.now())


def release(engine, meta):
    getattr(engine, '_learn_callback_holds', {}).pop(meta.turn_id, None)


def rejected(engine, turn, meta, reason):
    applied = meta.learning
    if applied is not None and (reason.startswith('guard_') or reason == 'tells'):
        with engine.store.transaction():
            bandit.reject(engine.store.conn, turn.speaker, applied.context, applied.selected,
                          attempt_id=meta.turn_id, reason='guard' if reason.startswith('guard_') else 'content')
