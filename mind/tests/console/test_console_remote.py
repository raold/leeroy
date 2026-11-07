"""The console behind `tailscale serve`, which keeps the browser's Host: an allowed tailnet name
(--allow-host, [console] allow_hosts) as Host, with its https Origin. X-Forwarded-Host never
counts. Everything else is refused as before, and without an allowed name nothing changes. A
real server on a free port; the mind, voice and probes are fakes. The name is a made-up one."""
import asyncio
import logging
import socket
from contextlib import asynccontextmanager

import pytest

from conftest import seed_test_cast
from leeroy_mind.config import load_config
from leeroy_mind.console import cli
from leeroy_mind.console.httpd import Request
from console.rig import KEY, http, make_app, running

NAME = "pc.example-tailnet.ts.net"
TAILNET = f"https://{NAME}"


@pytest.fixture
def data(store, tmp_path):
    seed_test_cast(store)
    (tmp_path / "mind.toml").write_text("[bridge]\nport = 17878\n", encoding="utf-8")
    return tmp_path


@asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def run(coro):
    return asyncio.run(coro)


def remote(data, **kw):
    return make_app(data, allow_hosts=(NAME,), **kw)


def free_port() -> str:
    """A port nothing listens on just now, for a console built from its options."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def request(app, *, host, origin=None, forwarded=None, method="GET", path="/api/state"):
    headers = [("host", host), ("x-console-key", KEY)]
    if origin is not None:
        headers.append(("origin", origin))
    for value in ([forwarded] if isinstance(forwarded, str) else forwarded or []):
        headers.append(("x-forwarded-host", value))
    if method == "POST":
        headers.append(("content-type", "application/json"))
    return Request(method, path, path, {}, headers, b"{}")


# -- the allowed name as Host --------------------------------------------------------------------

def test_the_tailnet_name_is_served_with_or_without_its_port(data):
    async def go():
        async with served(remote(data)) as app:
            for host in (NAME, f"{NAME}:443", NAME.upper()):
                r = await http(app, "GET", "/api/state", host=host)
                assert r.status == 200, host
                r = await http(app, "GET", "/api/state", host=host, origin=TAILNET)
                assert r.status == 200, host
            page = await http(app, "GET", "/", host=NAME, key=False)
            assert page.status == 200 and "default-src 'none'" in page.header(
                "content-security-policy")
            assert (await http(app, "GET", "/static/console.css", host=NAME,
                               key=False)).status == 200
            # the key is still needed for everything but the page and its files
            assert (await http(app, "GET", "/api/state", host=NAME, key=False)).status == 403
            assert (await http(app, "GET", "/api/state", host=NAME,
                               headers={"X-Console-Key": "x" * 32}, key=False)).status == 403
            # and the console's own address works as before
            assert (await http(app, "GET", "/api/state")).status == 200
            assert (await http(app, "GET", "/api/state", origin=True)).status == 200
    run(go())


def test_a_change_from_the_tailnet_needs_its_https_origin_and_the_key(data):
    mind = running(data)

    async def go():
        async with served(remote(data, mind=mind)) as app:
            body = {"service": "mind", "action": "restart", "confirm": True}
            for origin in (False, f"http://127.0.0.1:{app.s.port}", f"http://{NAME}",
                           f"{TAILNET}:8445", "https://evil.example", "null"):
                r = await http(app, "POST", "/api/control", host=NAME, body=body, origin=origin)
                assert r.status == 403, origin
            r = await http(app, "POST", "/api/control", host=NAME, body=body, origin=TAILNET,
                           key=False)
            assert r.status == 403
            assert mind.acts == []
            r = await http(app, "POST", "/api/control", host=NAME, body=body, origin=TAILNET)
            assert r.status == 200 and r.json()["ok"] and mind.acts == ["restart"]
    run(go())


def test_spoofed_hosts_are_refused(data):
    async def go():
        async with served(remote(data)) as app:
            for host in ("evil.example", f"{NAME}.evil.example", f"x{NAME}",
                         "example-tailnet.ts.net",
                         f"{NAME}:8445", f"{NAME}:80", f"{NAME}:", f"{NAME}.", f"evil.example:443",
                         f"{NAME}:443:443", f"{NAME},127.0.0.1:{app.s.port}",
                         f"localhost:{app.s.port}", f"127.0.0.1:{app.s.port + 1}"):
                r = await http(app, "GET", "/api/state", host=host, origin=TAILNET)
                assert r.status == 403 and b"served at" in r.body, host
                r = await http(app, "GET", "/api/state", host=host)
                assert r.status == 403, host
            # a second Host, whichever is first
            r = await http(app, "GET", "/api/state", host=NAME,
                           headers={"Host": f"127.0.0.1:{app.s.port}"})
            assert r.status == 403
            r = await http(app, "GET", "/api/state", headers={"Host": NAME})
            assert r.status == 403
    run(go())


def test_origins_do_not_cross_between_the_two_addresses(data):
    async def go():
        async with served(remote(data)) as app:
            local = f"http://127.0.0.1:{app.s.port}"
            r = await http(app, "GET", "/api/state", host=NAME, origin=local)
            assert r.status == 403 and b"another origin" in r.body
            r = await http(app, "GET", "/api/state", origin=TAILNET)  # the console's own Host
            assert r.status == 403 and b"another origin" in r.body
            r = await http(app, "POST", "/api/control", origin=TAILNET,
                           body={"service": "mind", "action": "start"})
            assert r.status == 403
            r = await http(app, "GET", "/api/state", host=NAME, origin=TAILNET,
                           headers={"Sec-Fetch-Site": "cross-site"})
            assert r.status == 403
            r = await http(app, "GET", "/api/state", host=NAME, origin=TAILNET,
                           headers={"Sec-Fetch-Site": "same-origin"})
            assert r.status == 200
    run(go())


def test_the_refusal_names_both_addresses(data):
    async def go():
        async with served(remote(data)) as app:
            r = await http(app, "GET", "/api/state", host="evil.example")
            assert f"http://127.0.0.1:{app.s.port}/".encode() in r.body
            assert f"{TAILNET}/".encode() in r.body
    run(go())


# -- X-Forwarded-Host ----------------------------------------------------------------------------

def test_a_forwarded_host_never_makes_the_tailnet(data):
    """serve keeps the Host, so X-Forwarded-Host has no use here: whatever it names, the request
    is what its Host says."""
    mind = running(data)

    async def go():
        async with served(remote(data, mind=mind)) as app:
            for forwarded in (NAME, f"{NAME}:443", "evil.example", f"{NAME}, evil.example", ""):
                r = await http(app, "GET", "/api/state", origin=TAILNET,
                               headers={"X-Forwarded-Host": forwarded})
                assert r.status == 403 and b"another origin" in r.body, forwarded
                r = await http(app, "POST", "/api/control", origin=TAILNET,
                               headers={"X-Forwarded-Host": forwarded},
                               body={"service": "mind", "action": "restart", "confirm": True})
                assert r.status == 403, forwarded
                r = await http(app, "GET", "/api/state", origin=True,
                               headers={"X-Forwarded-Host": forwarded})
                assert r.status == 200, forwarded  # ignored, as it always was
                r = await http(app, "GET", "/api/state", host="evil.example", origin=TAILNET,
                               headers={"X-Forwarded-Host": forwarded})
                assert r.status == 403, forwarded
            assert mind.acts == []
    run(go())


def test_the_gate_reads_the_host_and_nothing_else(data):
    app = remote(data)
    local = f"127.0.0.1:{app.s.port}"
    assert app.gate(request(app, host=NAME, origin=TAILNET, forwarded="evil.example")) is None
    assert app.gate(request(app, host=local, forwarded=NAME)) is None  # its own, as before
    for host, origin, forwarded in ((local, TAILNET, NAME), (local, TAILNET, [NAME, NAME]),
                                    (NAME, "https://evil.example", "evil.example")):
        refused = app.gate(request(app, host=host, origin=origin, forwarded=forwarded))
        assert refused is not None and refused.status == 403, (host, forwarded)
        refused = app.gate(request(app, host=host, origin=origin, forwarded=forwarded,
                                   method="POST", path="/api/control"))
        assert refused is not None and refused.status == 403, (host, forwarded)


# -- without an allowed name -------------------------------------------------------------------

def test_without_an_allowed_name_nothing_changes(data):
    async def go():
        async with served(make_app(data)) as app:
            assert app.s.allow_hosts == ()
            for host in (NAME, f"{NAME}:443"):
                assert (await http(app, "GET", "/api/state", host=host)).status == 403
            r = await http(app, "GET", "/api/state", origin=TAILNET,
                           headers={"X-Forwarded-Host": NAME})
            assert r.status == 403
            r = await http(app, "GET", "/api/state", origin=True,
                           headers={"X-Forwarded-Host": NAME})
            assert r.status == 200  # ignored, as it always was
            r = await http(app, "GET", "/api/state", host="evil.example")
            assert r.body == (f"Forbidden. The console is served at http://127.0.0.1:"
                              f"{app.s.port}/ only.\n").encode()
            assert app.tailnet_urls == []
    run(go())


def test_another_allowed_name_is_not_this_one(data):
    async def go():
        async with served(make_app(data, allow_hosts=("other.example.ts.net",))) as app:
            assert (await http(app, "GET", "/api/state", host=NAME)).status == 403
            assert (await http(app, "GET", "/api/state",
                               host="other.example.ts.net")).status == 200
    run(go())


# -- the options ---------------------------------------------------------------------------------

def args(*argv):
    return cli.parser().parse_args(list(argv))


def test_allow_host_is_repeatable_and_checked():
    assert args().allow_host == []
    a = args("--allow-host", NAME, "--allow-host", "Other.Example.ts.net")
    assert a.allow_host == [NAME, "other.example.ts.net"]
    for bad in ("", "evil", "127.0.0.1", "[::1]", f"{NAME}:443", f"https://{NAME}", f"{NAME}/",
                f"-{NAME}", "a..b.net", "*.ts.net", f"{NAME}.", "x" * 64 + ".ts.net",
                "pc.example-tailnet.ts.n\u212aet", "pc\u00e9.example.ts.net"):
        with pytest.raises(SystemExit):
            args("--allow-host", bad)


def test_build_takes_allowed_names_from_the_options_and_mind_toml(data, tmp_path):
    (data / "mind.toml").write_text(
        f'[bridge]\nport = 17878\n[console]\nallow_hosts = ["Second.Example.ts.net"]\n',
        encoding="utf-8")
    app, problem = cli.build(args("--data-dir", str(data), "--no-control", "--root",
                                  str(tmp_path / "c"), "--allow-host", NAME), platform="linux")
    assert problem is None and app.s.config_problem is None
    assert app.s.allow_hosts == (NAME, "second.example.ts.net")
    assert app.tailnet_urls == [f"{TAILNET}/#key={app.s.key}",
                                f"https://second.example.ts.net/#key={app.s.key}"]


def test_a_bad_console_table_allows_no_name_from_it_and_says_so(data, tmp_path, caplog):
    for bad in (f'allow_hosts = "{NAME}"', 'allow_hosts = ["127.0.0.1"]',
                'allow_hosts = ["evil/"]', "allow_hosts = [1]",
                f'allow_hosts = ["{NAME}", "{NAME}:443"]'):
        (data / "mind.toml").write_text(f"[bridge]\nport = 17878\n[console]\n{bad}\n",
                                        encoding="utf-8")
        app, problem = cli.build(args("--data-dir", str(data), "--no-control", "--root",
                                      str(tmp_path / "c"), "--allow-host", NAME),
                                 platform="linux")
        assert problem is None and app.s.allow_hosts == (NAME,), bad
        assert "console" in app.s.config_problem, bad
        assert app.s.bridge_port == 17878  # the rest of mind.toml still applies
    (data / "mind.toml").write_text("[console]\nallow_hosts = []\nother = 1\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        app, _ = cli.build(args("--data-dir", str(data), "--no-control", "--root",
                                str(tmp_path / "c")), platform="linux")
        load_config(data / "mind.toml")
    assert app.s.allow_hosts == () and app.s.config_problem is None
    assert "unknown key console.other" in caplog.text
    assert "unknown section [console]" not in caplog.text  # config.py leaves it to the console


def test_the_start_line_adds_the_tailnet_url(data, tmp_path, capsys):
    app, _ = cli.build(args("--data-dir", str(data), "--no-control", "--root",
                            str(tmp_path / "c"), "--port", free_port(), "--voice-ui-port", "1",
                            "--allow-host", NAME), platform="linux")
    printed = start_line(app, capsys)
    key = app.s.key
    assert printed[0] == f"console: http://127.0.0.1:{app.s.port}/#key={key}"
    assert printed[1] == f"  tailnet: {TAILNET}/#key={key}"
    assert printed[2].startswith("  process control off;")
    assert [line for line in printed if key in line] == printed[:2]


def test_without_an_allowed_name_the_start_line_is_as_before(data, tmp_path, capsys):
    app, _ = cli.build(args("--data-dir", str(data), "--no-control", "--root",
                            str(tmp_path / "c"), "--port", free_port(), "--voice-ui-port", "1"),
                       platform="linux")
    printed = start_line(app, capsys)
    assert printed[0] == f"console: http://127.0.0.1:{app.s.port}/#key={app.s.key}"
    assert printed[1].startswith("  process control off;") and len(printed) == 2


def start_line(app, capsys) -> list[str]:
    """What run() prints once it listens; then it is stopped as Ctrl+C would."""
    async def go():
        task = asyncio.create_task(cli.run(app))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if app._server is not None and app._tasks:
                break
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(go())
    return capsys.readouterr().out.splitlines()
