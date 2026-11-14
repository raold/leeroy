"""A shared kindness gate; callers own feature switches and delivery receipts."""
from datetime import date, timedelta
from leeroy_mind.clock import local_at, night_of
from leeroy_mind.tuning.effective import dials_of

TEASED_KEY = 'culture:teased:{date}:{guid}'
OWNER_KEY = 'banter:owner-evening'


def tease_ok(conn, teaser: int, target: int, *, personas, human: int, now: float) -> tuple[bool, str]:
    return _tease_ok(conn, teaser, target, personas=personas, human=human, now=now)


def _tease_ok(conn, teaser, target, *, personas, human, now, allowance=False):
    """Only a thread which delivered its own mark may reuse its allowance."""
    from leeroy_mind.tuning.edits import apply_feelings, feeling_overlay, mood_overlay
    rel = conn.execute('SELECT affinity FROM relationship WHERE a=? AND b=?', (teaser,target)).fetchone()
    offsets = feeling_overlay(conn).get((teaser, target))
    if offsets:  # godmode feelings and moods count as the night's own do
        rel = (apply_feelings({'affinity': rel[0] if rel else 0.0}, offsets)['affinity'],)
    if rel is None or rel[0] < 30:
        return False, 'dislikes'
    p = personas.get(teaser)
    if p is None or (p.big_five.agreeableness.score <= 3 and dials_of(p).level('drama') >= 7):
        return False, 'cruel'
    mood = conn.execute('SELECT mood FROM persona_state WHERE guid=?', (target,)).fetchone()
    edited = mood_overlay(conn, target, now)
    if edited:
        mood = (max(-1.0, min(1.0, (mood[0] if mood else 0.0) + edited)),)
    if mood and mood[0] < -.3:
        return False, 'hurt_mood'
    local = local_at(now)
    night = night_of(local)
    previous = (date.fromisoformat(night) - timedelta(days=1)).isoformat()
    if conn.execute("SELECT 1 FROM emotion WHERE guid=? AND night IN (?,?) AND "
                    "((target=? AND type IN ('distress','shame','reproach','anger','fear','resentment','pity')) "
                    "OR (type IN ('shame','distress') AND intensity>=2)) LIMIT 1",
                    (target,night,previous,teaser)).fetchone():
        return False, 'hurt_emotion'
    a,b = sorted((teaser,target))
    if conn.execute('SELECT 1 FROM feud WHERE a=? AND b=? AND ended IS NULL', (a,b)).fetchone():
        return False, 'feud'
    if target == human:
        if conn.execute('SELECT 1 FROM attraction_state WHERE guid=? AND hurt_until>?', (teaser,now)).fetchone():
            return False, 'his_hurt'
        evening = conn.execute('SELECT value FROM meta WHERE key=?', (OWNER_KEY,)).fetchone()
        if not allowance and evening and evening[0] == night_of(local):
            return False, 'his_evening'
    key = TEASED_KEY.format(date=local.date().isoformat(),guid=target)
    if not allowance and conn.execute('SELECT 1 FROM meta WHERE key=?', (key,)).fetchone():
        return False, 'teased_today'
    return True, 'ok'


def mark_teased(store, target: int, *, now: float) -> None:
    store.set_meta(TEASED_KEY.format(date=local_at(now).date().isoformat(),guid=target), str(now))
