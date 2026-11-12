"""His raid consumables: SELECTs only, with no equipment or inventory changes."""
import re
from pathlib import Path

KIT = {46376:'flask',46377:'flask',46378:'flask',46379:'flask',
       40211:'potion',40212:'potion',33447:'potion',33448:'potion',40093:'potion',40087:'potion',40081:'potion',
       43015:'food',34753:'food',42999:'food',43000:'food',42995:'food',34767:'food',34769:'food',43001:'food',42994:'food',
       41119:'other'}
TARGETS = {'flask':2,'potion':10,'food':10}


def kit_sql(guid: int) -> str:
    if type(guid) is not int or not 0 < guid <= 0xFFFFFFFF:
        raise ValueError('invalid owner identity')
    return ('SELECT ii.itemEntry, SUM(ii.count) FROM character_inventory ci '
            'JOIN item_instance ii ON ii.guid = ci.item '
            f"WHERE ci.guid = {guid} AND ii.itemEntry IN ({','.join(map(str,KIT))}) GROUP BY ii.itemEntry")


def parse_kit(rows, log) -> dict[int,int]:
    counts, dropped = {}, 0
    for row in rows:
        try:
            if (not isinstance(row,(tuple,list)) or len(row) != 2 or
                    any(not isinstance(v,str) or re.fullmatch(r'[0-9]{1,10}',v) is None for v in row)):
                raise ValueError()
            item, count = map(int,row)
            if item not in KIT or not 0 <= count <= 0xFFFFFFFF or item in counts:
                raise ValueError()
            counts[item] = count
        except (ValueError,TypeError):
            dropped += 1
    if dropped:
        log.warning('kit_rows_dropped dropped=%d',dropped)
    return counts


def kit_view(counts: dict[int,int], *, library: Path | None = None) -> dict:
    from leeroy_mind.console.items import item_names
    counts = {i:n for i,n in counts.items() if i in KIT and type(n) is int and 0 <= n <= 0xFFFFFFFF}
    names = item_names(library,set(counts)) if library is not None and counts else {}
    groups = []
    for kind,target in TARGETS.items():
        have = sum(n for i,n in counts.items() if KIT[i] == kind)
        groups.append({'kind':kind,'have':have,'target':target,'short':max(0,target-have)})
    return {'groups':groups,'items':[{'id':i,'name':names.get(i,{}).get('name',f'Item {i}'),
                                    'count':n,'kind':KIT[i]} for i,n in sorted(counts.items()) if n]}
