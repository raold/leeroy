"""Small, shared dial and audience rules; no content or names in diagnostics."""
from leeroy_mind.personas.stances import is_woman
from leeroy_mind.tuning.effective import dials_of


def eligible(persona, dial):
    return (persona is not None and is_woman(persona)
            and persona.identity.age >= 25
            and persona.romance.stance in {'fond', 'drawn', 'lust'}
            and dial >= 1 and dials_of(persona).level('forward') > 0)


def whisper_context(conn, guid, human, now, *, jealousy):
    """Only the caller's exact game whisper pair may request this fixed wording."""
    row = conn.execute('SELECT * FROM bond_mood WHERE guid=? AND human=? AND updated<=? '
                       'AND expires>?', (guid, human, now, now)).fetchone()
    if row is None or row['strength'] <= 0:
        return []
    if row['mood'] == 'jealous' and jealousy <= 0:
        return []
    return {'missing': ['You have missed his company after time apart; no blame or pressure.'],
            'jealous': ['You feel a little jealous after attention you actually observed; keep it gentle.'],
            'playful': ['You feel playful after a good evening together.'],
            'quiet': []}[row['mood']]
