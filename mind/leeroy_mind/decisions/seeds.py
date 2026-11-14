"""Discussion seeds for the D2 engine: one per decision stage (proposed, poll open, settled). The
fact is the proposal and the data behind it, built from rows ("benched 2 of the last 3", "DKP 340,
rank 1 of 5"); `about` is its stakeholders, whom the engine's cast weighs three times. A member's
own position reaches her through the Decisions card part, so her words follow her side.
Officers' business (bank requests, ranks, polls in officers' channels) seeds officers' channels
only. SELECT-only."""
import json

from leeroy_mind.discord import layout
from leeroy_mind.server.seeds import FRESH_DEFAULT_S, Seed
from .apply import LABELS, OFFICER_KINDS, channels_of, officers_only, private_channel, stakeholders, summary
from .positions import facts_text, member_facts
from .polls import options_of, positions_of

CHANNELS = {'lineup': ('raid-planning',), 'loot_rule': ('loot-and-dkp',), 'bank': ('bank-requests',),
            'strategy': ('strategy',), 'rank': ('applications', 'officer-chat')}
MAX_POSITIONS = 5
MAX_FACT = 400


def _words(code: str) -> str:
    return code.replace('_', ' ')


def _positions(conn, positions, names, *, now) -> str:
    shown = positions[:MAX_POSITIONS]
    facts = member_facts(conn, [p['guid'] for p in shown], now=now)
    parts = []
    for p in shown:
        data = facts_text(p['why'], facts[p['guid']])
        parts.append(f"{names.get(p['guid'], '#' + str(p['guid']))} for {_words(p['side'])}"
                     + (f' ({data})' if data else f" ({_words(p['why'])})"))
    return '; '.join(parts)


def _fact(head: str, tail: str) -> str:
    text = head + (': ' + tail if tail else '')
    return text if len(text) <= MAX_FACT else text[:MAX_FACT - 1].rstrip() + '…'


def decision_seeds(conn, *, now, personas, officers) -> list[Seed]:
    names = {g: p.character_name for g, p in personas.items()}
    names.update({g: n for g, n in conn.execute('SELECT guid,name FROM cast_member') if g in personas})
    name = lambda g: names.get(g, f'#{g}')  # noqa: E731
    out = []
    for row in conn.execute("SELECT * FROM guild_decision WHERE status IN ('proposed','settled','in_effect') "
                            'AND proposed<=? ORDER BY id', (now,)).fetchall():
        message_channel, poll_channel = channels_of(conn, row)
        proposed = row['status'] == 'proposed'
        if not proposed and (row['settled'] is None or now - row['settled'] > FRESH_DEFAULT_S):
            continue
        # A proposal's fact carries its positions: the officers' own when voted in their channel
        private = officers_only(row, message_channel) or proposed and private_channel(poll_channel)
        channels = ('officer-chat',) if private and row['kind'] not in OFFICER_KINDS else CHANNELS[row['kind']]
        about = tuple(sorted(g for g in stakeholders(row) if g in personas and (not private or g in officers)))
        label = LABELS[row['kind']]
        if proposed:
            fact = _fact(f'{label} proposed, {summary(row, name)}',
                         _positions(conn, json.loads(row['positions']), names, now=now))
        else:
            by = 'by poll' if row['settled_by'] == 0 else 'by ' + name(row['settled_by'])
            fact = _fact(f'{label} settled {by}', summary(row, name))
        stage = 'proposed' if proposed else 'settled'
        out.append(Seed(f"decision:{row['id']}:{stage}", 'decision', channels, fact, about, private))
    for poll in conn.execute("SELECT * FROM guild_poll WHERE status='open' AND opened<=? AND closes>? ORDER BY id",
                             (now, now)).fetchall():
        channel = layout.channel(poll['channel'])
        if channel is None:
            continue
        positions = positions_of(poll)
        fact = _fact(f"Poll in #{poll['channel']}: {poll['question']} (options: "
                     + ', '.join(_words(o) for o in options_of(poll)) + ')',
                     _positions(conn, positions, names, now=now))
        about = tuple(sorted(p['guid'] for p in positions if p['guid'] in personas))
        out.append(Seed(f"decision:p{poll['id']}:poll", 'decision', (poll['channel'],), fact, about,
                        channel.officers_only))
    return out
