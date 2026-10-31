"""A small, tolerant reader for the data half of Lua addon files: it finds assignments such as
AtlasLoot_Data["Key"] = { ... } and turns their table constructors into Python values, skipping
any code it does not understand.

Values: str, int, float, bool, None, LuaTable. An index into a global with a string key
(AL["Normal Mode"], BabbleBoss["Sindragosa"]) evaluates to that key, which is the enUS text in
AceLocale and Babble tables; ".." concatenates; calls and arithmetic evaluate to None."""
import re
from dataclasses import dataclass, field


class LuaSyntaxError(ValueError):
    pass


@dataclass
class LuaTable:
    array: list = field(default_factory=list)
    hash: dict = field(default_factory=dict)

    def get(self, key, default=None):
        if isinstance(key, int) and 1 <= key <= len(self.array):
            return self.array[key - 1]
        return self.hash.get(key, default)


@dataclass(frozen=True)
class Name:
    """An unresolved global or field reference, e.g. Name('AL')."""
    path: str


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_LONG_OPEN = re.compile(r"\[(=*)\[")
_SYMBOLS = ("...", "..", "==", "~=", "<=", ">=", "::", "//", "<<", ">>")
_ESC = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
        "\\": "\\", '"': '"', "'": "'", "\n": "\n"}
_KEYWORDS = frozenset({"and", "break", "do", "else", "elseif", "end", "false", "for",
                       "function", "goto", "if", "in", "local", "nil", "not", "or", "repeat",
                       "return", "then", "true", "until", "while"})


def _long_bracket(src: str, i: int) -> tuple[str, int] | None:
    m = _LONG_OPEN.match(src, i)
    if not m:
        return None
    close = "]" + m.group(1) + "]"
    end = src.find(close, m.end())
    if end < 0:
        raise LuaSyntaxError(f"unclosed long bracket at {i}")
    body = src[m.end():end]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return body, end + len(close)


def _quoted(src: str, i: int) -> tuple[str, int]:
    q = src[i]
    out: list[str] = []
    i += 1
    while i < len(src):
        ch = src[i]
        if ch == q:
            return "".join(out), i + 1
        if ch == "\n":
            raise LuaSyntaxError(f"unfinished string at {i}")
        if ch == "\\" and i + 1 < len(src):
            nx = src[i + 1]
            if nx in _ESC:
                out.append(_ESC[nx])
                i += 2
            elif nx.isdigit():
                m = re.match(r"\d{1,3}", src[i + 1:])
                out.append(chr(int(m.group(0)) % 256))
                i += 1 + len(m.group(0))
            elif nx == "x" and re.match(r"[0-9a-fA-F]{2}", src[i + 2:i + 4]):
                out.append(chr(int(src[i + 2:i + 4], 16)))
                i += 4
            elif nx == "z":
                i += 2
                while i < len(src) and src[i].isspace():
                    i += 1
            else:
                out.append(nx)
                i += 2
            continue
        out.append(ch)
        i += 1
    raise LuaSyntaxError("unfinished string at end of file")


def tokenize(src: str) -> list[tuple[str, object]]:
    """Tokens as (kind, value): kinds are name, keyword, number, string and op."""
    toks: list[tuple[str, object]] = []
    i, n = 0, len(src)
    if src.startswith("﻿"):
        i = 1
    while i < n:
        ch = src[i]
        if ch.isspace():
            i += 1
            continue
        if src.startswith("--", i):
            lb = _long_bracket(src, i + 2)
            if lb is not None:
                i = lb[1]
            else:
                nl = src.find("\n", i)
                i = n if nl < 0 else nl + 1
            continue
        if ch in "\"'":
            s, i = _quoted(src, i)
            toks.append(("string", s))
            continue
        if ch == "[" and _LONG_OPEN.match(src, i):
            s, i = _long_bracket(src, i)
            toks.append(("string", s))
            continue
        m = _NAME.match(src, i)
        if m:
            word = m.group(0)
            toks.append(("keyword" if word in _KEYWORDS else "name", word))
            i = m.end()
            continue
        m = _NUMBER.match(src, i)
        if m and (ch.isdigit() or (ch == "." and i + 1 < n and src[i + 1].isdigit())):
            t = m.group(0)
            toks.append(("number", int(t, 16) if t[:2].lower() == "0x"
                         else float(t) if any(c in t for c in ".eE") else int(t)))
            i = m.end()
            continue
        for sym in _SYMBOLS:
            if src.startswith(sym, i):
                toks.append(("op", sym))
                i += len(sym)
                break
        else:
            toks.append(("op", ch))
            i += 1
    return toks


class _Parser:
    def __init__(self, toks: list[tuple[str, object]], pos: int = 0):
        self.t = toks
        self.i = pos

    def peek(self, k: int = 0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else ("eof", None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def accept(self, kind: str, value=None) -> bool:
        k, v = self.peek()
        if k == kind and (value is None or v == value):
            self.i += 1
            return True
        return False

    def expect(self, kind: str, value=None):
        if not self.accept(kind, value):
            raise LuaSyntaxError(f"expected {value or kind}, got {self.peek()} at token {self.i}")

    # expressions

    def expr(self):
        left = self.unary()
        while True:
            k, v = self.peek()
            if k == "op" and v == "..":
                self.take()
                right = self.expr()
                if isinstance(left, (str, int, float)) and isinstance(right, (str, int, float)):
                    left = _tostr(left) + _tostr(right)
                else:
                    left = None
                return left
            if (k == "op" and v in ("+", "-", "*", "/", "%", "^", "==", "~=", "<", ">", "<=",
                                    ">=", "//", "&", "|", "<<", ">>")) or \
                    (k == "keyword" and v in ("and", "or")):
                self.take()
                self.unary()
                left = None
                continue
            return left

    def unary(self):
        k, v = self.peek()
        if (k == "op" and v in ("-", "#", "~")) or (k == "keyword" and v == "not"):
            self.take()
            val = self.unary()
            if v == "-" and isinstance(val, (int, float)):
                return -val
            return None
        return self.suffixed()

    def suffixed(self):
        k, v = self.take()
        if k in ("number", "string"):
            val = v
        elif k == "keyword" and v in ("true", "false", "nil"):
            val = {"true": True, "false": False, "nil": None}[v]
        elif k == "keyword" and v == "function":
            self._skip_function()
            return None
        elif k == "op" and v == "{":
            val = self.table()
        elif k == "op" and v == "(":
            val = self.expr()
            self.expect("op", ")")
        elif k == "name":
            val = Name(v)
        elif k == "op" and v == "...":
            return None
        else:
            raise LuaSyntaxError(f"unexpected {(k, v)} at token {self.i - 1}")
        while True:
            k, v = self.peek()
            if k == "op" and v == ".":
                self.take()
                _, attr = self.take()
                val = Name(f"{val.path}.{attr}") if isinstance(val, Name) else None
            elif k == "op" and v == "[":
                self.take()
                key = self.expr()
                self.expect("op", "]")
                # AL["text"], BabbleBoss["Name"]: the enUS value is the key itself.
                val = key if isinstance(val, Name) and isinstance(key, str) else None
            elif k == "op" and v == ":":
                self.take()
                self.take()
                self._call_args()
                val = None
            elif (k == "op" and v in ("(", "{")) or k == "string":
                self._call_args()
                val = None
            else:
                return val

    def _call_args(self):
        k, v = self.peek()
        if k == "string":
            self.take()
            return
        if k == "op" and v == "{":
            self.take()
            self.table()
            return
        self.expect("op", "(")
        depth = 1
        while depth:
            k, v = self.take()
            if k == "eof":
                raise LuaSyntaxError("unclosed call")
            if k == "op" and v == "(":
                depth += 1
            elif k == "op" and v == ")":
                depth -= 1

    def _skip_function(self):
        """Skip a function body: every block opener (function, do, if, repeat; while and for
        open through their do) is closed by end or until."""
        depth = 1
        while depth:
            k, v = self.take()
            if k == "eof":
                raise LuaSyntaxError("unclosed function")
            if k == "keyword" and v in ("function", "do", "if", "repeat"):
                depth += 1
            elif k == "keyword" and v in ("end", "until"):
                depth -= 1

    def table(self) -> LuaTable:
        t = LuaTable()
        while True:
            if self.accept("op", "}"):
                return t
            k, v = self.peek()
            if k == "op" and v == "[":
                self.take()
                key = self.expr()
                self.expect("op", "]")
                self.expect("op", "=")
                t.hash[key] = self.expr()
            elif k == "name" and self.peek(1) == ("op", "="):
                self.take()
                self.take()
                t.hash[v] = self.expr()
            else:
                t.array.append(self.expr())
            if not (self.accept("op", ",") or self.accept("op", ";")):
                self.expect("op", "}")
                return t


def _tostr(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def parse_value(src: str):
    """One Lua expression (a table constructor, a string, a number...)."""
    p = _Parser(tokenize(src))
    val = p.expr()
    return val


def assignments(src: str, globals_: set[str]) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Every `G["key"] = expr` (or `G.key = expr`) for G in globals_, as {G: {key: value}}, in file
    order, plus a note for each one that could not be parsed. All other code is skipped."""
    toks = tokenize(src)
    out: dict[str, dict[str, object]] = {g: {} for g in globals_}
    errors: list[str] = []
    i = 0
    while i < len(toks):
        k, v = toks[i]
        if k == "name" and v in globals_:
            j = i + 1
            key = None
            if toks[j:j + 1] == [("op", "[")] and j + 2 < len(toks) and toks[j + 1][0] == "string" \
                    and toks[j + 2] == ("op", "]"):
                key, j = toks[j + 1][1], j + 3
            elif toks[j:j + 1] == [("op", ".")] and j + 1 < len(toks) and toks[j + 1][0] == "name":
                key, j = toks[j + 1][1], j + 2
            if key is not None and j < len(toks) and toks[j] == ("op", "="):
                p = _Parser(toks, j + 1)
                try:
                    out[v][key] = p.expr()
                    i = p.i
                    continue
                except LuaSyntaxError as e:
                    errors.append(f"{v}[{key!r}]: {e}")
        i += 1
    return out, errors
