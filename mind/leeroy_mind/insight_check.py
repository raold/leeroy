"""One ephemeral claim check with an already-resident embedding model."""
import asyncio

from leeroy_mind.claims.check import check
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.nightly.gpu import norm


def arguments(value):
    if not isinstance(value, dict) or set(value) != {'guid', 'text'}:
        raise ValueError('guid and text are required')
    guid, text = value['guid'], value['text']
    if type(guid) is not int or not 0 < guid <= 0xFFFFFFFF:
        raise ValueError('guid must name a member')
    if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 500 or len(text) > 1000
            or any(not c.isprintable() and not c.isspace() for c in text)):
        raise ValueError('text must be a line of 1 to 500 characters')
    try:
        text.encode('utf-8')
    except UnicodeError:
        raise ValueError('text must be valid Unicode') from None
    return {'guid': guid, 'text': ' '.join(text.split())}


async def run(app, args, lock, *, timeout=15):
    if app.stopping:
        return {'ok': False, 'error': 'The mind is stopping.'}
    if lock.locked():
        return {'ok': False, 'error': 'Another claim check is running.'}
    async with lock:
        try:
            async with asyncio.timeout(timeout):
                conn = app.store.conn
                if not conn.execute('SELECT 1 FROM member_claim WHERE guid=? AND superseded_by IS NULL '
                                    'AND (expires IS NULL OR expires>?) LIMIT 1',
                                    (args['guid'], app.clock.now())).fetchone():
                    return {'ok': False, 'error': 'This member has no live claims to check yet.'}
                model = app.cfg.models.embedding
                meta = dict(conn.execute("SELECT key,value FROM meta WHERE key IN ('embed_model','embed_dim')"))
                if meta and (meta.get('embed_model') != model or meta.get('embed_dim') != '512'):
                    return {'ok': False, 'error': 'The stored claims use a different embedding model.'}
                arbiter = getattr(app, 'arbiter', None)
                if arbiter is None:
                    return {'ok': False, 'error': 'The mind cannot tell which embedding model is loaded.'}
                loaded = {norm(n) for n in await arbiter.admin.loaded_models()}
                if norm(model) not in loaded:
                    return {'ok': False, 'error': 'The embedding model is not loaded right now.'}
                vector = (await app.embedder.embed([args['text']], Priority.BACKGROUND))[0]
                conflicts = check(conn, args['guid'], args['text'], vector, now=app.clock.now())
                return {'ok': True, 'conflicts': [{'id': c.claim_id, 'subject': c.subject,
                        'score': c.score, 'reason': c.reason} for c in conflicts],
                        'why': 'Possible conflicts, not a verdict. No line was stored or sent.'}
        except TimeoutError:
            return {'ok': False, 'error': 'The claim check timed out. Try again later.'}
        except Exception:
            # Neither exception text nor request content belongs in any log/reply.
            return {'ok': False, 'error': 'Claims could not be checked right now.'}
