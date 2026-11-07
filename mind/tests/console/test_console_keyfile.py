"""--key-file: a stable console key the owner bookmarks once. Made when missing (private to
this user, as control.json is), by default under %LOCALAPPDATA%, refused when he does not own
it, anyone else may read it, it is not a plain file or it holds a weak key, replaced by
`console rotate-key`, and never logged. Without it each start makes its own key, as before.
Every file here is a scratch one (LOCALAPPDATA is a tmp dir); the keys are fakes or made by the
test, and the tailnet name is a made-up one."""
import asyncio
import logging
import os
import re
import socket
import stat
import sys

import pytest

from conftest import seed_test_cast
from leeroy_mind import __main__ as mind_main
from leeroy_mind import endpoint
from leeroy_mind.console import cli, keyfile
from console.rig import http

FAKE = "fake-console-key-for-tests-only-0123456789ab"  # 44 URL-safe characters: not a real key
NAME = "pc.example-tailnet.ts.net"
KEYISH = re.compile(r"^[A-Za-z0-9_-]{43,}$")


def problem(path) -> str | None:
    """endpoint.private_problem for the file at path, read from the open file as keyfile does."""
    with open(path, "rb") as f:
        return endpoint.private_problem(f.fileno())


def private(path) -> bool:
    return problem(path) is None


@pytest.fixture(autouse=True)
def appdata(tmp_path_factory, monkeypatch):
    """LOCALAPPDATA is a scratch dir of its own: the default key file is never the owner's."""
    folder = tmp_path_factory.mktemp("appdata")
    monkeypatch.setenv("LOCALAPPDATA", str(folder))
    return folder


def loosen(path) -> None:
    """Make the file readable by others: on Windows a plain write already is (it inherits the
    folder's entries for SYSTEM and the Administrators); elsewhere its mode is opened up."""
    if os.name != "nt":
        os.chmod(path, 0o644)


def write_private(path, text: str) -> None:
    endpoint.write_private(path, text.encode("ascii"))


# -- the file ------------------------------------------------------------------------------------

def test_a_missing_key_file_is_made_private_with_a_fresh_key(tmp_path):
    path = tmp_path / "console.key"
    key = keyfile.load(path)
    assert KEYISH.match(key) and path.read_text("ascii").strip() == key
    assert private(path)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert keyfile.load(path) == key  # stable: the same key at every start
    assert sorted(p.name for p in tmp_path.iterdir()) == ["console.key"]


def test_two_new_files_get_different_keys(tmp_path):
    assert keyfile.load(tmp_path / "a.key") != keyfile.load(tmp_path / "b.key")


def test_a_private_file_with_a_key_is_read_as_it_is(tmp_path):
    path = tmp_path / "console.key"
    write_private(path, FAKE + "\n")
    assert keyfile.load(path) == FAKE
    write_private(path, FAKE)
    assert keyfile.load(path) == FAKE


def test_a_file_others_may_read_is_refused(tmp_path):
    path = tmp_path / "console.key"
    path.write_text(FAKE + "\n", encoding="ascii")
    loosen(path)
    assert not private(path)
    with pytest.raises(keyfile.KeyFileError) as e:
        keyfile.load(path)
    words = str(e.value)
    assert "readable by others" in words and "rotate-key" in words and FAKE not in words
    assert path.read_text("ascii") == FAKE + "\n"  # left as it was


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_group_readable_file_is_refused(tmp_path):
    path = tmp_path / "console.key"
    write_private(path, FAKE)
    os.chmod(path, 0o640)
    with pytest.raises(keyfile.KeyFileError, match="readable by others"):
        keyfile.load(path)


def test_what_is_not_a_regular_file_is_refused(tmp_path):
    folder = tmp_path / "console.key"
    folder.mkdir()
    with pytest.raises(keyfile.KeyFileError, match="not a regular file"):
        keyfile.load(folder)
    with pytest.raises(keyfile.KeyFileError, match="not a regular file"):
        keyfile.rotate(folder)
    target = tmp_path / "target.key"
    write_private(target, FAKE)
    link = tmp_path / "link.key"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        return  # no symlink privilege here
    with pytest.raises(keyfile.KeyFileError, match="not a regular file"):
        keyfile.load(link)
    with pytest.raises(keyfile.KeyFileError, match="not a regular file"):
        keyfile.rotate(link)
    assert cli.main(["rotate-key", "--key-file", str(link)]) == 1  # the link is not followed
    assert target.read_text("ascii") == FAKE and link.is_symlink()


def test_a_file_that_holds_no_key_is_refused_without_showing_it(tmp_path):
    path = tmp_path / "console.key"
    for bad in ("", "\n", "short", FAKE + " extra", "secret value with spaces " + FAKE,
                "é" * 40, FAKE[:31], FAKE[:42], "x" * 200):
        endpoint.write_private(path, bad.encode("utf-8"))
        with pytest.raises(keyfile.KeyFileError) as e:
            keyfile.load(path)
        assert "not a console key" in str(e.value)
        if bad.strip():
            assert bad.strip() not in str(e.value)


def test_a_key_file_in_a_missing_folder_is_refused(tmp_path):
    with pytest.raises(keyfile.KeyFileError, match="cannot"):
        keyfile.load(tmp_path / "nowhere" / "console.key")
    assert not (tmp_path / "nowhere").exists()


# -- rotate-key ----------------------------------------------------------------------------------

def test_rotate_replaces_the_key_and_keeps_the_file_private(tmp_path):
    path = tmp_path / "console.key"
    first = keyfile.load(path)
    second = keyfile.rotate(path)
    assert second != first and KEYISH.match(second) and keyfile.load(path) == second
    assert private(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["console.key"]  # no temporary left


def test_rotate_makes_a_loose_file_private_and_a_missing_one(tmp_path):
    loose = tmp_path / "loose.key"
    loose.write_text(FAKE, encoding="ascii")
    loosen(loose)
    key = keyfile.rotate(loose)
    assert key != FAKE and private(loose) and keyfile.load(loose) == key
    missing = tmp_path / "missing.key"
    key = keyfile.rotate(missing)
    assert private(missing) and keyfile.load(missing) == key


def test_the_rotate_key_command_writes_a_new_key_and_prints_the_bookmarks(tmp_path, capsys,
                                                                          caplog):
    path = tmp_path / "console.key"
    write_private(path, FAKE)
    with caplog.at_level(logging.DEBUG):
        assert mind_main.main(["console", "rotate-key", "--key-file", str(path),
                               "--allow-host", NAME]) == 0
    key = keyfile.load(path)
    assert key != FAKE
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"console: a new key is in {path}")
    assert f"  http://127.0.0.1:7881/#key={key}" in out
    assert f"  tailnet: https://{NAME}/#key={key}" in out
    assert key not in caplog.text and FAKE not in caplog.text
    assert cli.main(["rotate-key", "--key-file", str(path), "--port", "17881"]) == 0
    key = keyfile.load(path)
    out = capsys.readouterr().out.splitlines()
    assert f"  http://127.0.0.1:17881/#key={key}" in out and not any("tailnet" in o for o in out)


def test_rotate_key_refuses_a_folder(tmp_path, capsys):
    (tmp_path / "dir.key").mkdir()
    assert cli.main(["rotate-key", "--key-file", str(tmp_path / "dir.key")]) == 1
    err = capsys.readouterr().err
    assert "not a regular file" in err


# -- the console with --key-file -----------------------------------------------------------------

@pytest.fixture
def data(store, tmp_path):
    seed_test_cast(store)
    (tmp_path / "mind.toml").write_text("[bridge]\nport = 17878\n", encoding="utf-8")
    return tmp_path


def args(*argv):
    return cli.parser().parse_args(list(argv))


def free_port() -> str:
    """A port nothing listens on just now, for a console built from its options."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def test_the_console_uses_the_files_key_at_every_start(data, tmp_path):
    path = tmp_path / "keys" / "console.key"
    path.parent.mkdir()
    a = args("--data-dir", str(data), "--no-control", "--root", str(tmp_path / "c"),
             "--key-file", str(path))
    one, problem = cli.build(a, platform="linux")
    assert problem is None and one.s.key == keyfile.load(path) and private(path)
    two, _ = cli.build(a, platform="linux")
    assert two.s.key == one.s.key


def test_the_console_refuses_a_loose_key_file(data, tmp_path, capsys):
    path = tmp_path / "console.key"
    path.write_text(FAKE, encoding="ascii")
    loosen(path)
    app, problem = cli.build(args("--data-dir", str(data), "--no-control", "--root",
                                  str(tmp_path / "c"), "--key-file", str(path)),
                             platform="linux")
    assert app is None and "readable by others" in problem and FAKE not in problem
    assert cli.main(["--data-dir", str(data), "--no-control", "--root", str(tmp_path / "c"),
                     "--key-file", str(path)]) == 1
    captured = capsys.readouterr()
    assert "readable by others" in captured.err and FAKE not in captured.err + captured.out


def test_a_refused_data_dir_makes_no_key_file(tmp_path):
    path = tmp_path / "console.key"
    app, problem = cli.build(args("--data-dir", str(tmp_path / "nothing"), "--key-file",
                                  str(path)), platform="linux")
    assert app is None and "no mind.toml" in problem and not path.exists()


def test_without_a_key_file_each_start_makes_its_own_key(data, tmp_path):
    a = args("--data-dir", str(data), "--no-control", "--root", str(tmp_path / "c"))
    assert a.key_file is None
    one, _ = cli.build(a, platform="linux")
    two, _ = cli.build(a, platform="linux")
    assert one.s.key != two.s.key and len(one.s.key) >= 32
    assert not any(p.suffix == ".key" for p in data.iterdir())


def test_the_key_is_never_logged(data, tmp_path, caplog, capsys):
    path = tmp_path / "console.key"

    async def go(app):
        await app.start()
        try:
            for headers in ({"X-Console-Key": app.s.key}, {"X-Console-Key": "wrong" * 8}):
                await http(app, "GET", "/api/state", key=False, headers=headers)
                await http(app, "GET", "/api/personas", key=False, headers=headers, host=NAME)
                await http(app, "GET", "/api/feed?after=x", key=False, headers=headers)
                await http(app, "POST", "/api/control", key=False, headers=headers,
                           origin=f"https://{NAME}", host=NAME, body={"service": "x"})
            await asyncio.sleep(0.05)
        finally:
            await app.close()

    with caplog.at_level(logging.DEBUG):
        app, problem = cli.build(args("--data-dir", str(data), "--no-control", "--root",
                                      str(tmp_path / "c"), "--key-file", str(path),
                                      "--allow-host", NAME, "--port", free_port(),
                                      "--voice-ui-port", "1"), platform="linux")
        assert problem is None
        asyncio.run(go(app))
        keyfile.rotate(path)
        keyfile.load(path)
    key = app.s.key
    assert key not in caplog.text and keyfile.load(path) not in caplog.text
    assert key not in capsys.readouterr().out  # only the start line prints it (run(), not here)


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows access list")
def test_the_windows_check_reads_the_access_list(tmp_path):
    path = tmp_path / "console.key"
    write_private(path, FAKE)
    assert problem(path) is None
    plain = tmp_path / "plain.key"
    plain.write_text(FAKE, encoding="ascii")  # inherits SYSTEM's and the Administrators' entries
    assert "others" in problem(plain)


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows owner")
def test_a_file_another_user_owns_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "console.key"
    write_private(path, FAKE)
    # As another user would see it: the file's owner (this user) is not the one reading it
    monkeypatch.setattr(endpoint, "_user_sid", lambda: "S-1-5-18")
    assert problem(path) == "it belongs to another user"
    with pytest.raises(keyfile.KeyFileError, match="readable by others .it belongs to another"):
        keyfile.load(path)


def test_the_check_reads_the_open_file_not_its_name(tmp_path, monkeypatch):
    path = tmp_path / "console.key"
    write_private(path, FAKE)
    seen = []
    real = endpoint.private_problem

    def check(fd):
        seen.append(fd)
        assert isinstance(fd, int)
        return real(fd)
    monkeypatch.setattr(endpoint, "private_problem", check)
    assert keyfile.load(path) == FAKE and len(seen) == 1


# -- the default location ------------------------------------------------------------------------

def test_the_default_key_file_is_under_localappdata(appdata, capsys):
    path = keyfile.default_path()
    assert path == appdata / "leeroy" / "console.key" and path.parent.is_dir()
    assert cli.main(["rotate-key", "--allow-host", NAME]) == 0
    key = keyfile.load(path)
    assert private(path) and f"  tailnet: https://{NAME}/#key={key}" in capsys.readouterr().out
    assert cli.main(["rotate-key", "--key-file"]) == 0 and keyfile.load(path) != key


def test_without_localappdata_a_key_file_must_be_named(monkeypatch, capsys):
    monkeypatch.delenv("LOCALAPPDATA")
    with pytest.raises(keyfile.KeyFileError, match="LOCALAPPDATA"):
        keyfile.default_path()
    assert cli.main(["rotate-key"]) == 1 and "LOCALAPPDATA" in capsys.readouterr().err
