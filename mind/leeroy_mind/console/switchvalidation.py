"""Validate the bounded switchboard snapshot without re-reading a local config file."""
from dataclasses import fields
import io
import re
from types import SimpleNamespace


def valid_table(name, table, raw):
    if type(table) is not dict:
        return False
    simple = {'claims':{'enabled'},'signals':{'enabled'},'fingerprint':{'enabled'},
              'callbacks':{'enabled'},'looks':{'diversity'},'personas':{'auto_newcomers'},
              'playstyle':{'enabled'},'photos':{'enabled','send'},'raidlead':{'enabled','briefs'}}
    if name in simple:
        return not set(table)-simple[name] and all(type(v) is bool for v in table.values())
    if name in ('talk','romance','selfies','control'):
        if name in ('talk','romance'):
            from leeroy_mind.config import _RULES
            rules = _RULES[name]
        elif name == 'selfies':
            from leeroy_mind.selfies.config import _RULES as rules
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
        from leeroy_mind.learn.arms import FAMILIES
        flags = {'enabled','coach','auto_lessons','bandit','freeze','capture_preferences','apply','cycle'}
        return (not set(table)-flags-{'frozen_families','frozen_members'}
                and all(type(table.get(k,False)) is bool for k in flags)
                and type(table.get('frozen_families',[])) is list
                and all(type(f) is str and f in FAMILIES for f in table.get('frozen_families',[]))
                and type(table.get('frozen_members',[])) is list
                and all(type(g) is int and g>0 for g in table.get('frozen_members',[])))
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
    return False
