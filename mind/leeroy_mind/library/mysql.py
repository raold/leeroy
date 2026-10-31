"""Read-only SELECTs through mysql.exe, the roster's way: credentials from worldserver.conf go to a
temporary option file (0600, deleted afterwards), never onto a command line, into a log or into
an error message.

The game's database is live, so each session is read-only on the server and every SELECT has a
server-side time limit: killing a slow mysql.exe would leave its query running."""
import os
import subprocess
import tempfile
from pathlib import Path

from leeroy_mind.personas.roster import DbInfo, _quote

_ESCAPES = {"n": "\n", "t": "\t", "0": "\0", "\\": "\\", "r": "\r", "b": "\b", "Z": "\x1a"}
MAX_EXECUTION_MS = 30_000
_INIT = "SET SESSION transaction_read_only = ON, SESSION max_execution_time = {ms}"


class QueryError(Exception):
    pass


def unescape_field(s: str) -> str | None:
    """mysql --batch escapes \\, newline, tab and NUL in values and prints NULL for NULL."""
    if s == "NULL":
        return None
    if "\\" not in s:
        return s
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            out.append(_ESCAPES.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_tsv(text: str) -> list[list[str | None]]:
    """Rows of mysql --batch output. mysql.exe on Windows ends each row with CRLF: one trailing
    CR per row goes before the split, or the last column keeps it (and NULL reads "NULL\\r")."""
    rows = []
    for line in text.split("\n"):
        line = line.removesuffix("\r")
        if line != "":
            rows.append([unescape_field(f) for f in line.split("\t")])
    return rows


def _mask(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def check_select(sql: str) -> None:
    """One SELECT or SHOW statement, or QueryError: outside quotes there may be no ';' but a
    final one, no comment and no backslash (a mysql client command)."""
    s = sql.strip()
    if not s.upper().startswith(("SELECT", "SHOW", "(SELECT")):
        raise QueryError("only SELECT and SHOW statements run here")
    s = s.removesuffix(";")
    quote, i = None, 0
    while i < len(s):
        ch = s[i]
        if quote:
            if ch == "\\" and quote != "`":
                i += 2
                continue
            if ch == quote:
                if s[i + 1:i + 2] == quote:  # a doubled quote stays inside
                    i += 2
                    continue
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == ";" or ch == "#" or ch == "\\" or s.startswith(("--", "/*"), i):
            raise QueryError("one statement only, without comments or client commands")
        i += 1
    if quote:
        raise QueryError("unterminated quote in the statement")


def query(db: DbInfo, sql: str, mysql_exe: Path, *, timeout: float = 120,
          max_ms: int = MAX_EXECUTION_MS) -> list[list[str | None]]:
    """Rows of one SELECT as lists of str (None for NULL). The session is read-only on the
    server and the SELECT stops there after max_ms."""
    check_select(sql)
    tmp = tempfile.mkdtemp(prefix="leeroy-mysql-")
    try:
        cnf = Path(tmp) / "client.cnf"
        fd = os.open(cnf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("[client]\n"
                     f"user={_quote(db.user)}\npassword={_quote(db.password)}\n"
                     f"host={_quote(db.host)}\nport={db.port}\n")
        try:
            # The statement goes in on stdin: long IN lists would overflow a Windows command line.
            # Bytes, not text mode: universal newlines would turn a raw \r inside a value into a
            # row break.
            r = subprocess.run(
                [str(mysql_exe), f"--defaults-extra-file={cnf}", "--batch", "--skip-column-names",
                 "--default-character-set=utf8mb4", f"--init-command={_INIT.format(ms=int(max_ms))}",
                 db.name],
                input=sql.encode("utf-8"), capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise QueryError(f"mysql did not run: {e.__class__.__name__}") from None
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace")
            raise QueryError(f"mysql failed ({r.returncode}): "
                             f"{_mask(err, db.password).strip()[:300]}")
        return parse_tsv(r.stdout.decode("utf-8", "replace"))
    finally:
        for p in Path(tmp).glob("*"):
            p.unlink(missing_ok=True)
        os.rmdir(tmp)


__all__ = ["DbInfo", "QueryError", "check_select", "parse_tsv", "query", "unescape_field"]
