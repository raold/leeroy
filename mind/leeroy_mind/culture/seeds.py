"""Running jokes rebuilt from their surviving, public factual provenance."""
import json
from leeroy_mind.clock import SystemClock
from leeroy_mind.culture.origins import origin_text, term_origin
from leeroy_mind.memory.privacy import derive_scope, restricted_channels
from leeroy_mind.memory.recall import hidden_ok
from leeroy_mind.nightly import lexicon
from leeroy_mind.storage.store import Store
from leeroy_mind.server.seeds import Seed


def culture_seeds(conn, *, now, local, personas, human) -> list[Seed]:
    return _culture_seeds(conn, now=now, personas=personas, human=human)


def _culture_seeds(conn, *, now, personas, human, include_recent=False):
    store = Store(conn, clock=SystemClock())
    names = {g: p.character_name for g, p in personas.items()}
    names.update({g: name for g, name in conn.execute('SELECT guid,name FROM cast_member')
                  if g in personas or g == human})
    result = []
    for row in conn.execute("SELECT * FROM lexicon WHERE kind='joke' AND status='active' "
                            'AND retired IS NULL AND (? OR last_used<?) ORDER BY id', (include_recent, now-3*86400)).fetchall():
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
            text = origin_text(conn, term_origin(row), names)
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
