"""GPU status and the stopped-mind fallback; never write the mind's database."""
import ipaddress
from urllib.parse import urlsplit
import urllib.request

from ..gpucontrol import KEY, loaded, since_value
from ..nightly.gpu import HttpOllamaAdmin


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalAdmin(HttpOllamaAdmin):
    def __init__(self, url, **kwargs):
        parts = urlsplit(url)
        if parts.hostname == 'localhost':
            url = parts._replace(netloc='127.0.0.1' + (f':{parts.port}' if parts.port else '')).geturl()
        super().__init__(url, **kwargs)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def stored(conn):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (KEY,)).fetchone()
    since = since_value(row[0] if row else None)
    return {'paused': since is not None, 'since': since}


def loopback(url):
    try:
        parts = urlsplit(url)
        _ = parts.port  # validate a malformed or out-of-range configured port
        return (parts.scheme in ('http', 'https') and not parts.username and not parts.password
                and not parts.query and not parts.fragment
                and (parts.hostname == 'localhost' or ipaddress.ip_address(parts.hostname).is_loopback))
    except (ValueError, TypeError):
        return False


async def stopped(url, action, *, admin_factory=LocalAdmin):
    if not loopback(url):
        return {'ok': False, 'error': 'gpu_nonlocal',
                'message': 'Free GPU only contacts Ollama on this computer.'}
    if action == 'resume':
        return {'ok': False, 'error': 'mind_unavailable',
                'message': 'Start the mind to resume its paused GPU use.'}
    admin = admin_factory(url, timeout=5)
    names, freed = [], 0
    try:
        # A pause waits the admin's own timeout; the status strip keeps its short one.
        models = await loaded(admin, timeout=None) if action == 'pause' else await loaded(admin)
        if action == 'pause':
            for model in models:
                await admin.unload(model['name'])
                names.append(model['name'])
                freed += model['size_vram']
        return {'ok': True, 'loaded': [] if action == 'pause' else models,
                'unloaded': names, 'freed_bytes': freed,
                'message': f'The mind is stopped; unloaded {len(names)} models.' if action == 'pause' else None}
    except Exception:
        return {'ok': False, 'error': 'gpu_pause_failed', 'unloaded': names, 'freed_bytes': freed,
                'message': 'Ollama did not confirm the unload. Try Free GPU again.'}
