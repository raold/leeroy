"""Validate the bounded switchboard snapshot without re-reading a local config file."""
from dataclasses import fields
import io
import math
import re
from types import SimpleNamespace


def valid_field(name, table, key):
    """One row's own verdict, for a table whose loader falls back field by field: [life] keeps
    every well-formed field when another is malformed (config.py), so only the malformed field's
    row is invalid. None: this table is judged whole, by valid_table."""
    if name != 'life' or type(table) is not dict:
        return None
    from leeroy_mind.config import _RULES
    return key not in table or key not in _RULES['life'] or bool(_RULES['life'][key][0](table[key]))


def valid_table(name, table, raw):
    """True: the table is valid. False: it is not, so its loader turns it off (or refuses it).
    None: no rule here for this table, so the console cannot say (shown as unknown, never as
    invalid)."""
    if type(table) is not dict:
        return False
    simple = {'claims':{'enabled'},'signals':{'enabled'},'fingerprint':{'enabled'},
              'callbacks':{'enabled'},'looks':{'diversity'},'personas':{'auto_newcomers'},
              'playstyle':{'enabled'},'photos':{'enabled','send','requests','scene_from_words'},
              'raidlead':{'enabled','briefs'},
              'raidplay':{'respond'},'drama':{'member_romance'},'bakeoff':{'capture'},
              'outreach':{'enabled'},'godmode':{'enabled'},'tuning':{'nudge_memories'},
              'heat':{'enabled','public_cap'},'geography':{'enabled'},'watchdog':{'enabled'}}
    if name == 'drama':
        # `threads` is a mode, not a boolean; a bad one turns only itself off (drama.config)
        table = {k:v for k,v in table.items() if k != 'threads'}
    if name in simple:
        return not set(table)-simple[name] and all(type(v) is bool for v in table.values())
    if name in ('talk','romance','life','selfies','control','voice'):
        if name in ('talk','romance','life'):
            from leeroy_mind.config import _RULES
            rules = _RULES[name]
        elif name == 'selfies':
            from leeroy_mind.selfies.config import _RULES as rules
        elif name == 'voice':
            from leeroy_mind.voice.config import _RULES as rules
        else:
            from leeroy_mind.endpoint import _RULES as rules
        return all(k not in rules or rules[k][0](v) for k,v in table.items())
    if name == 'guards':
        from leeroy_mind.guards.config import GuardsCfg, parse_config
        if set(table)-{f.name for f in fields(GuardsCfg)}:
            return False
        if type(table.get('enabled',False)) is not bool:
            return False
        return parse_config({**table,'enabled':True}).enabled
    if name == 'learn':
        from leeroy_mind.learn.arms import all_families
        FAMILIES = all_families()
        flags = {'enabled','coach','auto_lessons','bandit','freeze','capture_preferences','apply','cycle'}
        return (not set(table)-flags-{'frozen_families','frozen_members'}
                and all(type(table.get(k,False)) is bool for k in flags)
                and type(table.get('frozen_families',[])) is list
                and all(type(f) is str and f in FAMILIES for f in table.get('frozen_families',[]))
                and type(table.get('frozen_members',[])) is list
                and all(type(g) is int and g>0 for g in table.get('frozen_members',[])))
    if name == 'lab':
        from leeroy_mind.lab.config import KEYS, parse
        return not set(table)-KEYS and (table == {} or parse(table) != parse({}) or all(v == getattr(parse({}), k) for k, v in table.items()))
    if name == 'banter':
        return (not set(table)-{'enabled','per_hour'} and type(table.get('enabled',False)) is bool
                and type(table.get('per_hour',2)) is int and 0 <= table.get('per_hour',2) <= 12)
    if name == 'world':
        from leeroy_mind.world.config import WorldCfg
        return (not set(table)-{f.name for f in fields(WorldCfg)} and all(
            type(v) is bool if k in ('enabled','realm','selfies') else type(v) is int and 1<=v<=50
            for k,v in table.items()))
    if name == 'server':
        from leeroy_mind.server.config import ServerConfig
        def valid(k,v):
            if k in ('enabled','reactions'):
                return type(v) is bool
            if k == 'dms_per_day':
                return type(v) is int and 0<=v<=12
            if k in ('conversations_per_hour','in_game_share'):
                return type(v) in (int,float) and 0<=v<=(12 if k=='conversations_per_hour' else 1)
            return (type(v) is list and len(v)==2 and v[0]!=v[1] and
                    all(type(t) is str and re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',t) for t in v))
        return not set(table)-{f.name for f in fields(ServerConfig)} and all(valid(k,v) for k,v in table.items())
    if name == 'realm':
        from leeroy_mind.realm.config import RealmConfig, _month_day
        def valid(k,v):
            if k in ('enabled','gank_human'):
                return type(v) is bool
            if k == 'trial_rank':
                return type(v) is int and 2<=v<=9
            if k == 'member_cap':
                return type(v) is int and 1<=v<=1000
            return _month_day(v)
        return not set(table)-{f.name for f in fields(RealmConfig)} and all(valid(k,v) for k,v in table.items())
    if name == 'training':
        from leeroy_mind.training.config import load_training_config
        try:
            load_training_config(SimpleNamespace(open=lambda *_:io.BytesIO(raw)))
            return True
        except (ValueError,TypeError):
            return False
    if name == 'decisions':
        from leeroy_mind.decisions.config import BOOLS, RANGES, DecisionsConfig
        return (not set(table)-{f.name for f in fields(DecisionsConfig)} and all(
            type(v) is bool if k in BOOLS else type(v) is int and RANGES[k][0] <= v <= RANGES[k][1]
            for k,v in table.items()))
    if name == 'culture':
        from leeroy_mind.culture.config import CultureConfig
        return (not set(table)-{f.name for f in fields(CultureConfig)} and all(
            type(v) is int and 0 <= v <= 14 if k == 'memes_per_week' else type(v) is bool
            for k,v in table.items()))
    if name == 'groups':
        ranges = {'max_open':(1,12),'member_starts_per_week':(0,3)}
        return (not set(table)-{'enabled',*ranges} and all(
            type(v) is bool if k == 'enabled' else type(v) is int and ranges[k][0] <= v <= ranges[k][1]
            for k,v in table.items()))
    if name == 'voice_notes':
        ranges = {'per_member_day':(0,10),'guild_day':(0,50),'max_words':(5,60)}
        def valid(k,v):
            if k == 'enabled':
                return type(v) is bool
            if k == 'share':
                return type(v) in (int,float) and math.isfinite(v) and 0 <= v <= .5
            return type(v) is int and ranges[k][0] <= v <= ranges[k][1]
        return not set(table)-{'enabled','share',*ranges} and all(valid(k,v) for k,v in table.items())
    if name == 'prune':
        from leeroy_mind.nightly.prune import ROWS_MAX
        return (not set(table)-{'enabled','rows_per_night'} and type(table.get('enabled',False)) is bool
                and type(table.get('rows_per_night',1)) is int and 1 <= table.get('rows_per_night',1) <= ROWS_MAX)
    if name == 'turing':
        from leeroy_mind.turing.config import DAYS_MAX, WINDOWS_MAX
        ranges = {'windows':(0,WINDOWS_MAX),'days':(1,DAYS_MAX)}
        return (not set(table)-{'enabled',*ranges} and all(
            type(v) is bool if k == 'enabled' else type(v) is int and ranges[k][0] <= v <= ranges[k][1]
            for k,v in table.items()))
    return None
