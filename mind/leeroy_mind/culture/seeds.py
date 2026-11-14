"""Running jokes rebuilt from their surviving, public factual provenance."""
import json
import math
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
