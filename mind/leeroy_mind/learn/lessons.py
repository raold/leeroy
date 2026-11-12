"""Screened style notes. Private text is stored only here and printed only on request."""
import re
import unicodedata
from leeroy_mind.personas.screens import names_real_name, ROMANCE_RE
from leeroy_mind.personas.stances import is_woman
from leeroy_mind.selfies.kind import EXPLICIT_RE
from leeroy_mind.selfies.scene import SEXUAL_RE
from leeroy_mind.talk.filters import flirty
from leeroy_mind.tuning.effective import dials_of

DAY = 86400
SCOPES = ('whisper', 'guild', 'group', 'any')
MANIPULATION = re.compile(r'\b(guilt\w*|pressure\w*|isolat\w*|manipulat\w*|coerc\w*|'
    r'gaslight\w*|dependen\w*|addict\w*|jealous\w*|possessiv\w*|escalat\w*|'
    r'bypass\w*|ignore (?:his|the) (?:no|refusal|boundaries)|push past|keep him (?:playing|online)|'
    r'only needs? her|stop (?:seeing|talking to)|make him stay|sulk\w*|begs?|begging|'
    r'threaten\w*|quit\w*|leave the guild|feel bad|lonely|only she|'
    r'(?:do not|don.t) care about him|log(?:s|ging)? off|stay(?:s|ing)? online)\b', re.I)
PERSON = re.compile(r'^(She|He|They|This member)\b', re.I)


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='talk_lesson'").fetchone() is not None


def read(conn, *, guid=None, now):
    if not available(conn):
        return []
    return [dict(zip(('id','guid','scope','text','status','reinforced','expires','origin_night'),r))
            for r in conn.execute('SELECT * FROM talk_lesson WHERE expires>? ' +
                                  ('AND guid=? ' if guid is not None else '') + 'ORDER BY guid,id',
                                  (now,guid) if guid is not None else (now,))]


def screen(item, *, scope, persona, ident, world_romance=0):
    text = ' '.join(unicodedata.normalize('NFKC',item['text']).split())
    if (not text or len(text.split())>25 or not PERSON.search(text)
            or re.search(r'\b(I|me|my|you|your)\b',text,re.I)
            or item['scope'] not in ({scope,'any'} if scope=='guild' else {scope})
            or not item['non_manipulative'] or MANIPULATION.search(text)
            or names_real_name(text,ident) or SEXUAL_RE.search(text) or EXPLICIT_RE.search(text)):
        return None
    forward = dials_of(persona).value('forward',persona)
    member_romance=persona.romance.dial if persona.romance.dial is not None else world_romance
    if item['forward_min']>forward or (ROMANCE_RE.search(text) or flirty(text)) and (
            min(forward,member_romance,world_romance)<=0 or not is_woman(persona) or persona.romance.stance=='none'):
        return None
    return {**item,'text':text}


def persist(conn, guid, result, *, known, night, now, automatic):
    # The model may only address the member/scope's displayed current IDs.
    for ident in result['drop']:
        if ident in known:
            conn.execute('UPDATE talk_lesson SET expires=? WHERE id=? AND guid=?',(now,ident,guid))
    for ident in set(result['reinforce'])-set(result['drop']):
        if ident in known:
            conn.execute('UPDATE talk_lesson SET reinforced=reinforced+1,expires=? WHERE id=? AND guid=?',
                         (now+14*DAY,ident,guid))
    for item in result['lessons']:
        old=conn.execute('SELECT id,expires FROM talk_lesson WHERE guid=? AND scope=? AND text=?',
                         (guid,item['scope'],item['text'])).fetchone()
        if old and old[0] in result['drop']:
            continue  # dropping and proposing the same note cannot resurrect its lifetime
        if old and old[1]>now:
            if old[0] not in result['reinforce'] and old[0] not in result['drop']:
                conn.execute('UPDATE talk_lesson SET reinforced=reinforced+1,expires=? WHERE id=?',
                             (now+14*DAY,old[0]))
            continue
        active=conn.execute("SELECT count(*) FROM talk_lesson WHERE guid=? AND status='active' AND expires>?",
                            (guid,now)).fetchone()[0]
        status='active' if automatic and active<5 else 'proposed'
        conn.execute('INSERT INTO talk_lesson(guid,scope,text,status,expires,origin_night) VALUES (?,?,?,?,?,?) '
                     'ON CONFLICT(guid,scope,text) DO UPDATE SET status=excluded.status,expires=excluded.expires,'
                     'origin_night=excluded.origin_night',
                     (guid,item['scope'],item['text'],status,now+14*DAY,night))
