"""Running jokes rebuilt from their surviving, public factual provenance."""
from dataclasses import replace
import json
import math
import re
from leeroy_mind.culture.origins import origin_text, term_origin
from leeroy_mind.memory.privacy import derive_scope, restricted_channels
from leeroy_mind.memory.recall import hidden_ok
from leeroy_mind.nightly import lexicon
from leeroy_mind.server.seeds import Seed


def culture_seeds(store, *, now, local, personas, human, library=None, ident=None) -> list[Seed]:
    return _culture_seeds(store, now=now, personas=personas, human=human, library=library, ident=ident)


def _culture_seeds(store, *, now, personas, human, include_recent=False, only=None, library=None, ident=None):
    conn = store.conn
    names = {g: p.character_name for g, p in personas.items()}
    names.update({g: name for g, name in conn.execute('SELECT guid,name FROM cast_member')
                  if g in personas or g == human})
    result = []
    # The live conversation rechecks just its own indexed row, never every joke.
    query = "SELECT * FROM lexicon WHERE kind='joke' AND status='active' AND retired IS NULL AND (? OR last_used<?)"
    args = [include_recent, now-3*86400]
    if only is not None:
        query += ' AND id=?'
        args.append(only)
    for row in conn.execute(query + ' ORDER BY id', args).fetchall():
        if not include_recent:
            try:
                reserved = float(store.get_meta(f"banter:used:joke:{row['id']}") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(reserved) or reserved > now - 86400:
                continue
        # Walking the joke root also checks retained sources, paired output memories,
        # freeze/veto markers and nested evidence. Missing proof never becomes public.
        refs = lexicon.public_sources(store, (f"joke:{row['id']}",))
        if refs is None:
            continue
        snapshot = lexicon.source_snapshot(store, refs)
        if snapshot is None:
            continue
        evidence = tuple(ref for ref, _ in snapshot)
        if derive_scope(conn, row['originator'], evidence).scope != 'open':
            continue
        hidden = restricted_channels(conn, {row['id']: evidence}).get(row['id'], ())
        if hidden and not all(hidden_ok(hidden, 'discord:'+channel) for channel in ('general', 'memes')):
            continue
        if row['origin_kind'] == 'event':
            text = origin_text(store, term_origin(row), names, library=library, ident=ident)
        else:
            ref = row['origin_ref']
            if not isinstance(ref, str) or not ref.startswith('chat:') or not ref[5:].isdigit():
                continue
            line = lexicon.public_line(store, int(ref[5:]), human=human)
            text = (None if line is None else
                    f"{names.get(line['from_guid'], 'A guild member')} said: {json.dumps(line['text'])}")
        if text is not None:
            result.append(Seed(f"joke:{row['id']}", 'joke', ('general', 'memes'),
                               f"Running joke: '{row['term']}' — from {text}",
                               (row['butt'], row['originator'])))
    return result


# -- Part B: the server's own calendar and the guild's loot. Every fact is built from rows. --

def _library_rows(conn, sql, args):
    """The library attached read-only by the caller (runtime.snapshot); none: no rows."""
    import sqlite3
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        if not str(exc).startswith(('no such table', 'no such column')):
            raise
        return []


_DATED = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:(?:am|pm)\b|a\.m\.|p\.m\.)|\bo'clock\b|\bnoon\b|\bmidnight\b"
                    r"|\bserver time\b|\b(?:mon|tues|wednes|thurs|fri|satur|sun)days?\b"
                    r"|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
                    re.IGNORECASE)


def _first_sentence(text, cap=140):
    from leeroy_mind.text import has_meta, strip_wow_markup
    if not isinstance(text, str):
        return None
    clean = ' '.join(''.join(c for c in strip_wow_markup(text[:2000]) if c.isprintable()).split())
    found = re.match(r'(.+?[.!?])(?:\s|$)', clean)
    sentence = found[1] if found else None
    # A longer sentence is left out whole, never cut into something the page did not say. One
    # that names a time or a date is left out too: only the computed local time is stated.
    if not sentence or len(sentence) > cap or '|' in sentence or has_meta(sentence) or _DATED.search(sentence):
        return None
    return sentence


def _real_name(text, ident) -> bool:
    from leeroy_mind.personas.screens import names_real_name
    return ident is not None and names_real_name(text, ident)


def wow_event_seeds(conn, *, now, local, ident=None) -> list[Seed]:
    """One subject an occurrence: its "starts" and "on" share the key, and a run the bridge
    reports keeps its first start (or the table's), so a worldserver boot is no new subject."""
    from leeroy_mind.culture import wowcal
    from leeroy_mind.culture.origins import _safe_name
    values = dict(conn.execute('SELECT key,value FROM meta WHERE key IN (SELECT value FROM json_each(?))',
                               (json.dumps(wowcal.META_KEYS),)).fetchall())
    out = []
    for event, on in wowcal.from_meta(values, now=now, ahead_days=wowcal.SEED_AHEAD_DAYS):
        if not on and event.starts > now + wowcal.SEED_AHEAD_DAYS * 86400:
            continue
        if _safe_name(event.name, ident=ident) != event.name:
            continue
        fact = wowcal.describe(event, on, local)
        page = _library_rows(conn, "SELECT text FROM library.chunks WHERE kind='event' AND title=? "
                             'COLLATE NOCASE ORDER BY id LIMIT 1', (wowcal.library_title(event.name),))
        if page and (sentence := _first_sentence(page[0][0])) and not _real_name(sentence, ident):
            fact += '. ' + sentence
        channels = (('professions-and-gold', 'achievements-and-mounts') if 'Fishing' in event.name
                    else ('pvp-arena', 'general') if event.name.startswith('Call to Arms')
                    else ('achievements-and-mounts', 'general'))
        out.append(Seed(f'wow_event:{event.entry}:{int(event.starts)}', 'wow_event', channels, fact))
    return out


LOOT_CLOSE_S = 7 * 86400.0


def loot_close_seeds(conn, *, now, cast, names, human, ident=None) -> list[Seed]:
    """Close awards from charged receipts with their unique matching debit (origins.loot_record),
    in the last seven days, as raid chat announced them: the price paid over the runner-up's
    bid. A drop already talked about as plain loot, or already a running joke, is not raised
    again; who may speak about the runner-up is the teasing gate's call (rhythm.choose_cast)."""
    from leeroy_mind.culture.origins import _item_name, loot_record
    out = []
    for lid, note in conn.execute("SELECT id,note FROM dkp_ledger WHERE reason='item' AND ts BETWEEN ? AND ? "
                                  "AND note GLOB 'guild:loot:*' ORDER BY ts DESC,id DESC LIMIT 64",
                                  (now - LOOT_CLOSE_S, now)).fetchall():
        ref = 'loot:close_loot:' + note
        found = loot_record(conn, ref)
        if found is None:
            continue
        record, _ = found
        award = record['award']
        winner, runner = award['guid'], award.get('runner_up')
        if (award.get('close') is not True or type(runner) is not int or runner == winner
                or human in (winner, runner) or winner not in cast or runner not in cast):
            continue
        if conn.execute('SELECT 1 FROM meta WHERE key=?', (f'server:seed:loot:{lid}',)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM lexicon WHERE origin_ref=?", (ref,)).fetchone():
            continue  # Part A made this receipt a running joke: one subject, not two
        item = _item_name(conn, record, ident=ident)
        if item == 'an item':
            continue
        paid, runner_bid = award.get('paid'), award.get('runner_up_bid')
        margin = (round(paid - runner_bid, 1) if all(type(v) in (int, float) and math.isfinite(v)
                                                     for v in (paid, runner_bid)) else None)
        fact = (f"{names.get(winner, 'A guild member')} beat {names.get(runner, 'a guild member')} to [{item}]"
                + (f' by {margin:g} DKP' if margin is not None and margin > 0 else ''))
        out.append(Seed(f'loot_close:{lid}', 'loot_close', ('loot-and-dkp',), fact, (winner, runner)))
    return out


def loot_wish_seeds(conn, *, now, cast, names, human, ident=None) -> list[Seed]:
    """One wish a raider, for the raid night still posted ahead; item names from the library."""
    from leeroy_mind.culture.loot import WISH_KEY, load_wishes, next_raid
    from leeroy_mind.culture.origins import _safe_name
    from leeroy_mind.discord import layout
    row = conn.execute('SELECT value FROM meta WHERE key=?', (WISH_KEY,)).fetchone()
    cached = load_wishes(row[0]) if row else None
    if cached is None or not cached['wishes'] or next_raid(conn, now) != cached['event']:
        return []
    classes = dict(conn.execute('SELECT guid,class FROM cast_member').fetchall())
    out = []
    for wish in cached['wishes']:
        guid = wish['guid']
        if guid == human or guid not in cast:
            continue
        found = _library_rows(conn, 'SELECT name FROM library.item WHERE id=?', (wish['item_id'],))
        item = _safe_name(found[0][0], ident=ident) if found else None
        if item is None:
            continue
        channels = ('loot-and-dkp',) + ((layout.CLASS_CHANNEL[classes[guid]],)
                                        if classes.get(guid) in layout.CLASS_CHANNEL else ())
        out.append(Seed(f"loot_wish:{cached['event']}:{guid}:{wish['item_id']}", 'loot_wish', channels,
                        f"{names.get(guid, 'A guild member')} needs [{item}] from {wish['boss']}", (guid,)))
    # Together the wishes weigh about three ordinary subjects, however many raiders there are.
    weight = min(1.0, 3 / len(out)) if out else 1.0
    return [replace(s, weight=weight) for s in out]


# -- Part C: memes. Text memes ride the jokes' own provenance; image memes are rendered at night. --

MEME_RAID_S = 3 * 86400.0


def meme_seeds(store, *, now, local, personas, human, library=None, ident=None) -> list[Seed]:
    """Text memes and copypastas for #memes: one subject an active, public running joke (the
    same rebuilt fact and butt as its joke seed, culture_seeds), and the guild's last raid with
    wipes. Together they weigh about one ordinary subject."""
    from leeroy_mind.guild.raids import CATALOG
    out = [Seed(f"meme:joke:{s.key[5:]}", "meme", ("memes",), s.fact, s.about)
           for s in _culture_seeds(store, now=now, personas=personas, human=human, library=library, ident=ident)]
    row = store.conn.execute("SELECT raid_id,raid_key,size,kills,wipes FROM guild_raid WHERE ended BETWEEN ? AND ? "
                             "AND wipes>0 ORDER BY ended DESC,raid_id DESC LIMIT 1", (now - MEME_RAID_S, now)).fetchone()
    if row is not None and row[1] in CATALOG:
        rid, key, size, kills, wipes = row
        out.append(Seed(f"meme:raid:{rid}", "meme", ("memes",),
                        f"Last raid: {CATALOG[key].name} {size}, {wipes} wipes and {kills} kills"))
    weight = min(1.0, 1 / len(out)) if out else 1.0
    return [replace(s, weight=weight) for s in out]


def meme_image_seeds(store, *, now, local, personas, human, library=None, ident=None) -> list[Seed]:
    """A ready image meme (culture.memes) for #memes, while its joke still rebuilds as public:
    the joke's own fact and butt, so the kindness gate casts it as it casts the joke."""
    from leeroy_mind.culture.memes import ready_memes
    out = []
    for meme in ready_memes(store.conn, now):
        jokes = [s for s in _culture_seeds(store, now=now, personas=personas, human=human, include_recent=True,
                                           only=meme["joke"], library=library, ident=ident)
                 if s.key == f"joke:{meme['joke']}"]
        if jokes:
            out.append(Seed(f"meme_image:{meme['id']}:{meme['joke']}", "meme_image", ("memes",),
                            jokes[0].fact, jokes[0].about))
    return out


def joke_of(seed) -> int | None:
    """The running joke a joke or meme seed rests on (None: a raid-wipe meme, any other kind)."""
    parts = seed.key.split(":")
    try:
        if seed.kind == "joke":
            return int(parts[1])
        if seed.kind == "meme" and parts[1] == "joke":
            return int(parts[2])
        if seed.kind == "meme_image":
            return int(parts[2])
    except (IndexError, ValueError):
        return None
    return None
