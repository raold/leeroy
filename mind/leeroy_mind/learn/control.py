"""Owner learning controls. Only the mind writes; no config files or models are touched."""
from dataclasses import replace
import json
import sqlite3

from . import arms, lessons, weights
from .config import LearnCfg

PREFIX = 'learn:control:'
REPORT_KEY = 'learn:weekly-report'  # Task 43 publishes its numeric report here.


def overrides(conn):
    try:
        rows = conn.execute("SELECT key,value FROM meta WHERE key=? OR key GLOB ?",
                            (PREFIX+'freeze', PREFIX+'family:*'))
        return {k.removeprefix(PREFIX): v != 'false' for k, v in rows}
    except sqlite3.OperationalError:
        return {}


def effective(conn, cfg=None):
    """The learning switches in force: mind.toml's, plus the Lab's overrides. The Lab can freeze
    more and lift its own freezes, never one mind.toml sets (owner ruling 2026-11-12)."""
    cfg = cfg or LearnCfg()
    saved = overrides(conn)
    families = set(cfg.frozen_families)
    for family in arms.FAMILIES:
        if saved.get('family:'+family) is True:
            families.add(family)
    return replace(cfg, freeze=cfg.freeze or saved.get('freeze') is True,
                   frozen_families=tuple(sorted(families)))


def frozen(conn, cfg=None):
    return effective(conn, cfg).freeze


def vetoed(conn, kind, ident):
    return conn.execute('SELECT 1 FROM meta WHERE key=?',
                        (PREFIX+f'veto:{kind}:{ident}',)).fetchone() is not None


def decide(conn, kind, ident, action, *, now):
    """An owner or guard decision a later rollback must not undo (learn/cycle.py replays it)."""
    try:
        conn.execute('INSERT INTO learn_decision(ts,kind,ident,action) VALUES (?,?,?,?)',
                     (now, kind, str(ident), action))
    except sqlite3.OperationalError:
        pass  # an older schema without the weekly cycle keeps no decision log


def arguments(args):
    if type(args) is not dict or type(args.get('action')) is not str:
        raise ValueError('Choose a learning action.')
    action = args['action']
    keys = {'freeze': {'frozen'}, 'family': {'family', 'frozen'},
            'arm': {'id', 'verb'}, 'lesson': {'id', 'verb'}, 'weights': {'values', 'version'},
            'rollback': {'version', 'weights'}}
    if action not in keys or set(args) != {'action', *keys[action]}:
        raise ValueError('The learning action has missing or unexpected fields.')
    if action in ('freeze', 'family'):
        if type(args['frozen']) is not bool:
            raise ValueError('Freeze must be on or off.')
        if action == 'family' and args['family'] not in arms.FAMILIES:
            raise ValueError('Choose a known arm family.')
    elif action == 'arm':
        if (type(args['id']) is not str or not 1 <= len(args['id']) <= 100
                or args['verb'] not in ('retire', 'accept', 'reject')):
            raise ValueError('Choose an arm and accept, reject or retire.')
    elif action == 'lesson':
        if (type(args['id']) is not int or not 0 < args['id'] < 2**63
                or args['verb'] not in ('approve', 'veto', 'expire')):
            raise ValueError('Choose a lesson and approve, veto or expire.')
    elif action == 'rollback':
        if args['version'] is not None and (type(args['version']) is not int or not 0 < args['version'] < 2**63):
            raise ValueError('Choose a saved learning-state version.')
        if type(args['weights']) is not bool:
            raise ValueError('Say whether the reward weights are restored too.')
    else:
        try:
            weights.validate(args['values'])
        except ValueError:
            raise ValueError('All four weights must be between 0 and 1 and add up to 1.') from None
        if type(args['version']) is not int or args['version'] < 0:
            raise ValueError('The weights need their current version.')
    return dict(args)


def change(store, args, *, now):
    """Small atomic changes; compare weight versions to avoid overwriting another page."""
    args = arguments(args)
    action = args['action']
    with store.transaction():
        conn = store.conn
        if action in ('freeze', 'family'):
            key = 'freeze' if action == 'freeze' else 'family:'+args['family']
            store.set_meta(PREFIX+key, json.dumps(args['frozen']))
            subject = 'Learning' if action == 'freeze' else args['family'].capitalize()+' arms'
            message = subject+(' frozen.' if args['frozen'] else ' unfrozen here. A freeze set in mind.toml still holds.')
        elif action == 'weights':
            if not weights.available(conn):
                raise ValueError('Reward weights are not set up in this database yet.')
            if weights.read(conn, at=now).version != args['version']:
                raise ValueError('The weights changed. Refresh and review their latest values first.')
            version = weights.change(conn, args['values'], now=now).version
            message = f'Reward weights saved as version {version}. Earlier scores keep their recorded version.'
        elif action == 'arm':
            if not arms.available(conn):
                raise ValueError('Arms are not set up yet.')
            arm = next((a for a in arms.read(conn) if a.id == args['id']), None)
            if arm is None or arm.status == 'control':
                raise ValueError('Choose a stored arm other than the control.')
            verb = args['verb']
            # A retired arm (the guards', the weekly cycle's or his own) can be accepted again.
            if arm.status not in {'retire': ('active',), 'reject': ('proposed',),
                                  'accept': ('proposed', 'retired')}[verb]:
                raise ValueError('The arm changed. Refresh its status first.')
            if verb == 'accept' and not arms.valid(arm.family, arm.params):
                raise ValueError('This arm does not pass the parameter bounds.')
            conn.execute('UPDATE learn_arm SET status=? WHERE id=?',
                         ('active' if verb == 'accept' else 'retired', arm.id))
            if verb in ('retire', 'reject'):
                store.set_meta(PREFIX+'veto:arm:'+arm.id, str(now))
            elif arm.status == 'retired':
                conn.execute('DELETE FROM meta WHERE key=?', (PREFIX+'veto:arm:'+arm.id,))
            decide(conn, 'arm', arm.id, verb, now=now)
            message = {'accept': 'Arm accepted again.' if arm.status == 'retired' else 'Arm accepted.',
                       'reject': 'Arm rejected.', 'retire': 'Arm retired.'}[verb]
        elif action == 'rollback':
            # The owner's explicit restore: it works while frozen and keeps his vetoes and freezes.
            from .cycle import rollback
            done = rollback(conn, args['version'], now=now, with_weights=args['weights'])
            restored, saved = done['restored'], done['saved']
            message = (f'Learning restored to version {restored}: arm statuses, promotions and lessons. '
                       f'{done["kept"]} later decisions of yours and the guards were applied again; '
                       'your vetoes and freezes still hold. Reward weights were '
                       + ('restored too. ' if done['weights'] else 'kept as they are. ')
                       + f'The state before it was saved as version {saved}.')
        else:
            if not lessons.available(conn):
                raise ValueError('Lessons are not set up yet.')
            row = conn.execute('SELECT guid,status,expires FROM talk_lesson WHERE id=?', (args['id'],)).fetchone()
            if row is None or row[2] <= now:
                raise ValueError('That lesson has expired or is no longer available.')
            verb = args['verb']
            if verb == 'approve':
                if row[1] != 'proposed' or vetoed(conn, 'lesson', args['id']):
                    raise ValueError('Only a proposed lesson without a veto can be approved.')
                count = conn.execute("SELECT count(*) FROM talk_lesson WHERE guid=? AND status='active' AND expires>?",
                                     (row[0], now)).fetchone()[0]
                if count >= 5:
                    raise ValueError('This member already has five active lessons. Expire one first.')
                conn.execute("UPDATE talk_lesson SET status='active' WHERE id=?", (args['id'],))
            else:
                conn.execute('UPDATE talk_lesson SET expires=? WHERE id=?', (now, args['id']))
                if verb == 'veto':
                    store.set_meta(PREFIX+f'veto:lesson:{args["id"]}', str(now))
            decide(conn, 'lesson', args['id'], verb, now=now)
            message = {'approve': 'Lesson approved.', 'veto': 'Lesson vetoed.', 'expire': 'Lesson expired.'}[verb]
    if action == 'rollback':
        from leeroy_mind.logs import log
        log('learn_rollback', restored=restored, saved=saved, kept=done['kept'], weights=int(done['weights']),
            source='lab')
    return {'ok': True, 'message': message}
