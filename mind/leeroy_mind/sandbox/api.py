"""The sandbox's HTTP API on 127.0.0.1: one JSON request per connection.

GET  /status                      the sandbox: him, the model, what is switched off
GET  /members[?online=1]          the cast: guid, name, online, stance, a summary line
GET  /log[?since=N&limit=M]       the sandbox's mind.log (since: the `next` of an earlier read)
POST /say    {"channel", "to", "text"[, "wait"]}   his line, and what came back (runtime.say)
POST /login  {["wait"]}           he enters the game (members may greet him; wait collects them)
POST /logout {}                   he leaves the game
POST /group  {"members": [...]}   his group (empty: none)
POST /reset  {}                   the copy as it was at the start, a new mind on it
POST /stop   {}                   the sandbox stops

The copy is private, so the API answers only what a local program asks directly: the Host must
be this address, a request with an Origin (a browser page) is refused, and a POST must carry
application/json (a browser cannot send that cross-site without a preflight, which is never
answered)."""
import asyncio
import json
import logging
import urllib.parse

from .runtime import SandboxError

log = logging.getLogger("leeroy_mind")
MAX_HEAD = 16 * 1024
MAX_BODY = 64 * 1024
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large",
           415: "Unsupported Media Type", 500: "Internal Server Error"}


class Api:
    def __init__(self, sandbox, *, host: str = "127.0.0.1"):
        self.sandbox = sandbox
        self.host = host
        self.port = 0
        self._server: asyncio.Server | None = None
        self.stop_requested = asyncio.Event()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, body = 500, {"ok": False, "error": "internal error"}
        try:
            status, body = await self._request(reader)
        except SandboxError as e:
            status, body = e.status, {"ok": False, "error": str(e)}
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            writer.close()
            return
        except Exception as e:
            log.exception("sandbox_api_failed: error=%s", type(e).__name__)
            status, body = 500, {"ok": False, "error": f"{type(e).__name__}"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        head = (f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(data)}\r\nCache-Control: no-store\r\n"
                "Connection: close\r\n\r\n").encode("ascii")
        try:
            writer.write(head + data)
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def _request(self, reader: asyncio.StreamReader) -> tuple[int, dict]:
        raw = await reader.readuntil(b"\r\n\r\n")
        if len(raw) > MAX_HEAD:
            raise SandboxError("request head too large", 413)
        lines = raw.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            raise SandboxError("bad request line") from None
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if headers.get("host") not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            raise SandboxError("wrong Host", 403)
        if "origin" in headers:
            raise SandboxError("browser requests are refused", 403)
        url = urllib.parse.urlsplit(target)
        query = dict(urllib.parse.parse_qsl(url.query))
        body: dict = {}
        if method == "POST":
            if headers.get("content-type", "").split(";")[0].strip() != "application/json":
                raise SandboxError("POST needs Content-Type: application/json", 415)
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError:
                raise SandboxError("bad Content-Length") from None
            if not 0 <= length <= MAX_BODY:
                raise SandboxError("body too large", 413)
            data = await reader.readexactly(length) if length else b"{}"
            try:
                body = json.loads(data or b"{}")
            except ValueError:
                raise SandboxError("the body is not JSON") from None
            if not isinstance(body, dict):
                raise SandboxError("the body is a JSON object")
        elif method != "GET":
            raise SandboxError("GET or POST", 405)
        return await self._route(method, url.path.rstrip("/") or "/", query, body)

    async def _route(self, method, path, query, body) -> tuple[int, dict]:
        sb = self.sandbox
        if method == "GET":
            if path in ("/", "/status"):
                return 200, sb.status()
            if path == "/members":
                online = query.get("online", "") in ("1", "true", "yes")
                return 200, {"ok": True, "members": sb.members(online_only=online)}
            if path == "/log":
                try:
                    since = int(query["since"]) if "since" in query else None
                    limit = int(query.get("limit", 200))
                except ValueError:
                    raise SandboxError("since and limit are numbers") from None
                return 200, {"ok": True, **sb.read_log(since, limit)}
            if path in ("/say", "/login", "/logout", "/group", "/reset", "/stop"):
                raise SandboxError("POST", 405)
            raise SandboxError("not found", 404)
        if path == "/say":
            return 200, await sb.say(body.get("channel"), body.get("to"), body.get("text"),
                                     wait=body.get("wait", 120))
        if path == "/login":
            wait = body.get("wait", 0)
            if type(wait) not in (int, float) or not 0 <= wait <= 600:
                raise SandboxError("wait: 0 to 600 seconds")
            return 200, await sb.login(wait=float(wait))
        if path == "/logout":
            return 200, await sb.logout()
        if path == "/group":
            return 200, await sb.group(body.get("members"))
        if path == "/reset":
            return 200, await sb.reset()
        if path == "/stop":
            self.stop_requested.set()
            return 200, {"ok": True, "stopping": True}
        raise SandboxError("not found", 404)
