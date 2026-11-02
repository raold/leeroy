"""Conservative, pure source export of AzerothCore ChatCommandTable definitions.

The caller supplies the installed source inventory and database rows. Nothing here
reads files, executes C++, expands macros, queries SQL, or verifies a running server.
``complete`` means the supplied inventory was processed, not that all runtime
commands were discovered. Unsupported constructs remain explicit coverage gaps.

The supported builders and override precedence follow ChatCommand.h/.cpp: an
empty child installs its invoker on the parent; names containing spaces create
intermediate nodes; SQL permission overrides apply only to invokers; SQL help
fills monostate help but cannot replace a nonzero AcoreStrings help identifier.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import re
from typing import Any


_IDENT = re.compile(r"[A-Za-z_]\w*", re.ASCII)
_INTEGER = re.compile(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)[uUlL]*\Z")
_SEC = {"SEC_PLAYER": 0, "SEC_MODERATOR": 1, "SEC_GAMEMASTER": 2,
        "SEC_ADMINISTRATOR": 3, "SEC_CONSOLE": 4}


@dataclass(frozen=True)
class _Token:
    text: str
    line: int
    conditional: bool = False


def _include_guard(source: str) -> str | None:
    match = re.match(
        r"\A\s*(?:(?:/\*[\s\S]*?\*/|//[^\n]*\n)\s*)*"
        r"#\s*ifndef\s+([A-Za-z_]\w*)\s*\n\s*#\s*define\s+\1\s*(?:\n|$)", source)
    if not match:
        return None
    depth = 1
    for directive in re.finditer(r"(?m)^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b[^\n]*", source[match.end():]):
        kind = directive.group(1)
        if kind in ("if", "ifdef", "ifndef"):
            depth += 1
        elif kind in ("else", "elif") and depth == 1:
            return None
        elif kind == "endif":
            depth -= 1
            if depth == 0:
                tail = source[match.end() + directive.end():]
                tail = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", tail)
                return match.group(1) if not tail.strip() else None
    return None


def _lex(source: str) -> tuple[list[_Token], set[str]]:
    """Keep literals opaque, line locations exact, and conditional code marked."""
    tokens: list[_Token] = []
    macros: set[str] = set()
    pos, line, depth = 0, 1, 0
    # A conventional whole-header include guard protects repeated inclusion, not
    # an optional feature. Other preprocessor branches are deliberately unknown.
    guard = _include_guard(source)
    guard_open = False
    while pos < len(source):
        ch = source[pos]
        if ch.isspace():
            line += ch == "\n"
            pos += 1
            continue
        if source.startswith("//", pos):
            end = source.find("\n", pos)
            pos = len(source) if end < 0 else end
            continue
        if source.startswith("/*", pos):
            end = source.find("*/", pos + 2)
            if end < 0:
                raise ValueError(f"unterminated comment at line {line}")
            end += 2
            line += source[pos:end].count("\n")
            pos = end
            continue
        if ch == "#" and not source[source.rfind("\n", 0, pos) + 1:pos].strip():
            end = source.find("\n", pos)
            end = len(source) if end < 0 else end
            while end < len(source) and source[pos:end].rstrip().endswith("\\"):
                nxt = source.find("\n", end + 1)
                end = len(source) if nxt < 0 else nxt
            directive = source[pos:end]
            words = re.match(r"#\s*(\w+)(?:\s+([A-Za-z_]\w*))?", directive)
            if words:
                kind, name = words.groups()
                if kind in ("if", "ifdef", "ifndef"):
                    if kind == "ifndef" and name == guard and not guard_open and depth == 0:
                        guard_open = True
                    else:
                        depth += 1
                elif kind == "endif":
                    depth = max(0, depth - 1)
                elif kind == "define" and name:
                    macros.add(name)
            # Retain directives to detect an otherwise-empty conditional table.
            tokens.append(_Token(directive, line, True))
            line += directive.count("\n")
            pos = end
            continue
        start = pos
        raw = re.match(r'(?:u8|u|U|L)?R"([^ ()\\\t\r\n]{0,16})\(', source[pos:])
        quoted = re.match(r'(?:u8|u|U|L)?["\']', source[pos:])
        if raw:
            tail = ")" + raw.group(1) + '"'
            end = source.find(tail, pos + raw.end())
            if end < 0:
                raise ValueError(f"unterminated raw string at line {line}")
            pos = end + len(tail)
        elif quoted:
            quote = source[pos + quoted.end() - 1]
            pos += quoted.end()
            while pos < len(source):
                if source[pos] == "\\":
                    pos += 2
                elif source[pos] == quote:
                    pos += 1
                    break
                else:
                    pos += 1
            else:
                raise ValueError(f"unterminated string at line {line}")
        elif match := _IDENT.match(source, pos):
            pos = match.end()
        elif ch.isdigit():
            match = re.match(r"[0-9][A-Za-z0-9_]*", source[pos:])
            pos += len(match.group())
        elif source.startswith("::", pos):
            pos += 2
        else:
            pos += 1
        text = source[start:pos]
        tokens.append(_Token(text, line, depth > 0))
        line += text.count("\n")
    return tokens, macros


def _matching(tokens: list[_Token], start: int) -> int:
    pairs = {"{": "}", "(": ")", "[": "]", "<": ">"}
    opening = tokens[start].text
    stack = [pairs[opening]]
    for i in range(start + 1, len(tokens)):
        text = tokens[i].text
        if text in pairs:
            stack.append(pairs[text])
        elif text in pairs.values():
            if text != stack[-1]:
                raise ValueError("unbalanced initializer")
            stack.pop()
            if not stack:
                return i
    raise ValueError("unclosed initializer")


def _brace_end(tokens: list[_Token], start: int) -> int:
    # Outside command initializers '<' may be an ordinary comparison operator.
    depth = 0
    for i in range(start, len(tokens)):
        if tokens[i].text == "{":
            depth += 1
        elif tokens[i].text == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unclosed function/class")


def _parts(tokens: list[_Token]) -> list[list[_Token]]:
    result: list[list[_Token]] = []
    start = i = 0
    while i < len(tokens):
        if tokens[i].text in ("{", "(", "[", "<"):
            i = _matching(tokens, i)
        elif tokens[i].text == ",":
            if tokens[start:i]:
                result.append(tokens[start:i])
            start = i + 1
        i += 1
    if tokens[start:]:
        result.append(tokens[start:])
    return result


def _expr(tokens: list[_Token]) -> str:
    return "".join(t.text for t in tokens)


def _string(tokens: list[_Token]) -> str | None:
    """Decode only C++ string literals, never expressions or source macros."""
    result = ""
    for token in tokens:
        text = token.text
        raw = re.fullmatch(r'(?:u8|u|U|L)?R"([^ ()\\\t\r\n]{0,16})\((.*)\)\1"', text, re.S)
        if raw:
            result += raw.group(2)
            continue
        normal = re.fullmatch(r'(?:u8|u|U|L)?"(.*)"', text, re.S)
        if not normal:
            return None
        content, pos = normal.group(1), 0
        while pos < len(content):
            if content[pos] != "\\":
                result += content[pos]
                pos += 1
                continue
            pos += 1
            if pos == len(content):
                return None
            escaped = content[pos]
            simple = {"n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b",
                      "f": "\f", "v": "\v", "\\": "\\", '"': '"', "'": "'", "?": "?"}
            if escaped in simple:
                result += simple[escaped]
                pos += 1
            elif escaped == "\n":
                pos += 1
            else:
                match = re.match(r"[0-7]{1,3}|x[0-9a-fA-F]+|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}", content[pos:])
                if not match:
                    return None
                value = match.group()
                try:
                    result += chr(int(value if value[0].isdigit() else value[1:],
                                      8 if value[0].isdigit() else 16))
                except ValueError:
                    return None
                pos += len(value)
    return result if tokens else None


def _number(expression: str, symbols: Mapping[str, int]) -> int | None:
    if expression in symbols:
        return symbols[expression]
    if _INTEGER.fullmatch(expression):
        literal = expression.rstrip("uUlL")
        try:
            return int(literal, 16 if literal.lower().startswith("0x") else
                       8 if len(literal) > 1 and literal.startswith("0") else 10)
        except ValueError:
            return None
    return None


def _permission(expression: str, symbols: Mapping[str, int]) -> dict:
    number = _number(expression, symbols)
    if number is not None:
        if not 0 <= number <= 0xFFFFFFFF:
            return {"kind": "unknown", "value": None}
        threshold = symbols.get("rbac::RBAC_PERM_COMMAND_RBAC", 200)
        return {"kind": "rbac" if number >= threshold else "security", "value": number}
    if re.fullmatch(r"(?:rbac::)?RBAC_PERM_[A-Z0-9_]+", expression):
        return {"kind": "rbac", "value": expression}
    if re.fullmatch(r"SEC_[A-Z0-9_]+", expression):
        return {"kind": "security", "value": expression}
    return {"kind": "unknown", "value": None}


def _syntax(help_text: str) -> str:
    # No signature inference: export the literal, labeled syntax line only.
    match = re.search(r"(?im)^\s*syntax\s*:\s*([^\r\n]+)", help_text)
    return match.group(1).strip() if match else ""


class _Export:
    def __init__(self, symbols: Mapping[str, int], strings: Mapping[int | str, str]):
        self.symbols = {**_SEC, **symbols}
        self.strings = strings
        self.nodes: dict[str, dict] = {}
        self.unresolved: list[dict] = []
        self.sources: list[dict] = []
        self.scripts: dict[str, list[tuple[str, _Token]]] = {}
        self.external: set[str] = set()
        self.constructions: dict[str, list[_Token]] = {}
        self.macros: set[str] = set()
        self.base_declarations: set[tuple[str, int]] = set()
        self.non_definitions: list[dict] = []
        self.diagnostics: list[dict] = []

    def index(self, all_tokens: Mapping[str, list[_Token]]) -> None:
        for path, tokens in all_tokens.items():
            for i, token in enumerate(tokens):
                if macro := re.match(r"#\s*define\s+([A-Za-z_]\w*)", token.text):
                    self.macros.add(macro.group(1))
                if token.text == "new" and i + 1 < len(tokens):
                    self.constructions.setdefault(tokens[i + 1].text, []).append(token)
                if token.text == "GetCommands" and i >= 2 and tokens[i - 1].text == "::":
                    self.external.add(tokens[i - 2].text)
                if token.text not in ("class", "struct") or i + 2 >= len(tokens):
                    continue
                start = i + 2
                while start < len(tokens) and tokens[start].text not in ("{", ";"):
                    start += 1
                if (tokens[i + 1].text == "CommandScript" and start < len(tokens)
                        and tokens[start].text == "{"
                        and any(t.text == "ScriptObject" for t in tokens[i + 2:start])):
                    try:
                        end = _brace_end(tokens, start)
                    except ValueError:
                        continue
                    for j in range(start + 1, end):
                        if _expr(tokens[j:j + 7]) == "GetCommands()const=0;":
                            self.base_declarations.add((path, tokens[j].line))
                if start < len(tokens) and tokens[start].text == "{" and any(
                        t.text == "CommandScript" for t in tokens[i + 2:start]):
                    self.scripts.setdefault(tokens[i + 1].text, []).append((path, token))

    def gap(self, path: str, line: int | None, reason: str, detail: str) -> None:
        item = {"source_path": path, "source_line": line, "reason": reason, "detail": detail}
        if item not in self.unresolved:
            self.unresolved.append(item)

    def node(self, name: str, path: str, token: _Token, script: str) -> dict:
        key = name.casefold()
        if key not in self.nodes:
            self.nodes[key] = {
                "name": name, "syntax": "", "help": "", "handler": "",
                "permission": {"kind": "unknown", "value": None}, "allow_console": None,
                "source_path": path, "source_line": token.line, "verification": "source-observed",
                "metadata": {"namespace": True, "executable": False, "scripts": [],
                             "declarations": [], "help_source": "unknown"},
            }
        node = self.nodes[key]
        declaration = {"source_path": path, "source_line": token.line, "spelling": name}
        if declaration not in node["metadata"]["declarations"]:
            node["metadata"]["declarations"].append(declaration)
        if script not in node["metadata"]["scripts"]:
            node["metadata"]["scripts"].append(script)
        return node

    def source(self, path: str, text: str, all_tokens: Mapping[str, list[_Token]]) -> None:
        tokens = all_tokens[path]
        inventory = {"source_path": path, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                     "definitions": 0, "observed_nodes": 0}
        self.sources.append(inventory)
        before = set(self.nodes)
        seen: set[int] = set()
        for i, token in enumerate(tokens):
            if token.text not in ("class", "struct") or i + 2 >= len(tokens):
                continue
            start = i + 2
            while start < len(tokens) and tokens[start].text not in ("{", ";"):
                start += 1
            if start == len(tokens) or tokens[start].text != "{":
                continue
            if "CommandScript" not in [t.text for t in tokens[i + 2:start]]:
                continue
            try:
                end = _brace_end(tokens, start)
            except ValueError as exc:
                self.gap(path, token.line, "malformed_class", str(exc))
                continue
            script = tokens[i + 1].text
            construction = self.constructions.get(script, [])
            if not construction or all(t.conditional for t in construction):
                self.gap(path, token.line, "registration_unverified",
                         f"No unconditional construction of CommandScript {script} in supplied sources")
            for j in range(start + 1, end):
                if tokens[j].text != "GetCommands" or j in seen or tokens[j + 1].text != "(":
                    continue
                if tokens[j - 1].text == "." or (j >= 2 and _expr(tokens[j - 2:j]) == "->"):
                    continue
                seen.add(j)
                body_start = j + 1
                while body_start < end and tokens[body_start].text not in ("{", ";"):
                    body_start += 1
                if body_start == end or tokens[body_start].text == ";":
                    if script not in self.external:
                        self.gap(path, tokens[j].line, "external_definition", script)
                    continue
                inventory["definitions"] += 1
                try:
                    body_end = _brace_end(tokens, body_start)
                except ValueError as exc:
                    self.gap(path, tokens[j].line, "malformed_function", str(exc))
                    continue
                if token.conditional or tokens[j].conditional:
                    self.gap(path, tokens[j].line, "conditional_definition", script)
                    continue
                try:
                    self.function(path, script, tokens[body_start + 1:body_end], self.macros)
                except (ValueError, RecursionError) as exc:
                    self.gap(path, tokens[j].line, "malformed_registration", str(exc))
        inventory["observed_nodes"] = len(set(self.nodes) - before)
        for j, token in enumerate(tokens):
            if token.text == "GetCommands" and j not in seen:
                if j > 0 and (tokens[j - 1].text == "." or (j >= 2 and _expr(tokens[j - 2:j]) == "->")):
                    self.non_definitions.append({"source_path": path, "source_line": token.line,
                                                 "reason": "method_invocation"})
                    continue
                if (path, token.line) in self.base_declarations:
                    self.non_definitions.append({"source_path": path, "source_line": token.line,
                                                 "reason": "abstract_command_script_interface"})
                    continue
                script = tokens[j - 2].text if j >= 2 and tokens[j - 1].text == "::" else ""
                declarations = self.scripts.get(script, [])
                if len(declarations) != 1:
                    self.gap(path, token.line, "unrecognized_definition",
                             "GetCommands outside a unique recognized CommandScript")
                    continue
                if token.conditional or declarations[0][1].conditional:
                    self.gap(path, token.line, "conditional_definition", script)
                    continue
                body_start = j + 1
                while body_start < len(tokens) and tokens[body_start].text not in ("{", ";"):
                    body_start += 1
                if body_start == len(tokens) or tokens[body_start].text == ";":
                    self.gap(path, token.line, "external_definition", script)
                    continue
                inventory["definitions"] += 1
                try:
                    body_end = _brace_end(tokens, body_start)
                    self.function(path, script, tokens[body_start + 1:body_end], self.macros)
                except (ValueError, RecursionError) as exc:
                    self.gap(path, token.line, "malformed_registration", str(exc))
        inventory["observed_nodes"] = len(set(self.nodes) - before)

    def function(self, path: str, script: str, tokens: list[_Token], macros: set[str]) -> None:
        tables: dict[str, list[_Token]] = {}
        aliases: dict[str, str] = {}
        returns: list[list[_Token]] = []
        i = 0
        bad = False
        while i < len(tokens):
            token = tokens[i]
            if token.text.startswith("#"):
                if re.match(r"#\s*(?:if|elif|else|endif)", token.text):
                    self.gap(path, token.line, "conditional_registration", token.text.strip())
                i += 1
                continue
            start = i
            while i < len(tokens) and tokens[i].text in ("static", "const", "constexpr"):
                i += 1
            if i >= len(tokens):
                bad = True
                break
            if tokens[i].text == "ChatCommandTable":
                i += 1
            elif _expr(tokens[i:i + 5]) == "Acore::ChatCommands::ChatCommandTable":
                i += 5
            elif _expr(tokens[i:i + 6]) in ("std::vector<ChatCommand>", "std::vector<ChatCommandBuilder>"):
                i += 6
            elif tokens[i].text == "return":
                end = i + 1
                while end < len(tokens) and tokens[end].text != ";":
                    end += 1
                returns.append(tokens[i + 1:end])
                i = end + 1
                continue
            elif tokens[i].text == "using":
                while i < len(tokens) and tokens[i].text != ";":
                    i += 1
                i += 1
                continue
            else:
                self.gap(path, token.line, "dynamic_registration", _expr(tokens[start:start + 12]))
                bad = True
                break
            while i < len(tokens) and tokens[i].text in ("const", "&"):
                i += 1
            if i >= len(tokens) or not _IDENT.fullmatch(tokens[i].text):
                bad = True
                break
            name = tokens[i].text
            i += 1
            if i < len(tokens) and tokens[i].text == "=":
                i += 1
            if i < len(tokens) and tokens[i].text == "{":
                try:
                    end = _matching(tokens, i)
                except ValueError as exc:
                    self.gap(path, token.line, "malformed_table", f"{name}: {exc}")
                    return
                value = tokens[i + 1:end]
                i = end + 1
                if token.conditional:
                    self.gap(path, token.line, "conditional_table", name)
                elif name in tables or name in aliases:
                    self.gap(path, token.line, "duplicate_table", name)
                    bad = True
                else:
                    tables[name] = value
            elif i < len(tokens) and _IDENT.fullmatch(tokens[i].text):
                if token.conditional:
                    self.gap(path, token.line, "conditional_table", name)
                elif name in tables or name in aliases:
                    self.gap(path, token.line, "duplicate_table", name)
                    bad = True
                else:
                    aliases[name] = tokens[i].text
                i += 1
            else:
                self.gap(path, token.line, "unknown_table_initializer", name)
                bad = True
                break
            if i >= len(tokens) or tokens[i].text != ";":
                self.gap(path, token.line, "dynamic_table_initializer", name)
                bad = True
                break
            i += 1
        if bad or len(returns) != 1 or any(t.conditional for t in returns[0]):
            self.gap(path, tokens[0].line if tokens else None, "unresolved_return", script)
            return

        def visit(value: list[_Token], prefix: str, chain: tuple[str, ...]) -> None:
            if len(value) == 1 and _IDENT.fullmatch(value[0].text):
                name = value[0].text
                if name in chain:
                    self.gap(path, value[0].line, "table_cycle", " -> ".join((*chain, name)))
                elif name in aliases:
                    visit([_Token(aliases[name], value[0].line)], prefix, (*chain, name))
                elif name in tables:
                    visit(tables[name], prefix, (*chain, name))
                else:
                    self.gap(path, value[0].line, "unknown_table", name)
                return
            if value and value[0].text == "{" and _matching(value, 0) == len(value) - 1:
                # Direct return {{...}, {...}} has one additional outer pair.
                if len(value) > 1 and value[1].text == "{":
                    value = value[1:-1]
            try:
                entries = _parts(value)
            except ValueError as exc:
                self.gap(path, value[0].line if value else None, "malformed_entries", str(exc))
                return
            for entry in entries:
                directives = [t for t in entry if t.text.startswith("#")]
                for directive in directives:
                    self.gap(path, directive.line, "conditional_registration", directive.text.strip())
                entry = [t for t in entry if not t.text.startswith("#")]
                if not entry:
                    continue
                if any(t.conditional for t in entry):
                    self.gap(path, entry[0].line, "conditional_entry", _expr(entry[:12]))
                    continue
                try:
                    if entry[0].text != "{" or _matching(entry, 0) != len(entry) - 1:
                        raise ValueError("entry is not a literal builder")
                    fields = _parts(entry[1:-1])
                except ValueError as exc:
                    self.gap(path, entry[0].line, "unknown_entry", f"{_expr(entry[:12])}: {exc}")
                    continue
                if len(fields) not in (2, 4, 5) or (name := _string(fields[0])) is None:
                    self.gap(path, entry[0].line, "unknown_builder", _expr(entry[:12]))
                    continue
                if not name and not prefix:
                    self.gap(path, entry[0].line, "invalid_top_level_default", script)
                    continue
                # COMMAND_DELIMITER is ASCII space; don't reinterpret tabs or punctuation.
                names = [part for part in name.split(" ") if part]
                if name and (not names or any(not re.fullmatch(r"[^\s\x00-\x1f]+", p) for p in names)):
                    self.gap(path, entry[0].line, "invalid_name", name)
                    continue
                full = prefix
                for part in names:
                    full = f"{full} {part}".lstrip()
                    self.node(full, path, entry[0], script)
                node = self.node(full, path, entry[0], script)
                if len(fields) == 2:
                    visit(fields[1], full, chain)
                    continue
                handler = _expr(fields[1])
                if not re.fullmatch(r"&?(?:::)?[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*(?:<[^{}();]+>)?", handler):
                    node["metadata"].update(namespace=False, unresolved_handler=True)
                    self.gap(path, entry[0].line, "unknown_handler", handler)
                    continue
                if any(t.text in macros for t in fields[1]) or handler in ("nullptr", "NULL"):
                    node["metadata"].update(namespace=False, unresolved_handler=True)
                    self.gap(path, entry[0].line, "unknown_handler", handler)
                    continue
                meta = node["metadata"]
                if node["handler"] or meta.get("ambiguous_handler"):
                    self.gap(path, entry[0].line, "duplicate_handler", full)
                    node.update(handler="", syntax="", help="", allow_console=None,
                                permission={"kind": "unknown", "value": None})
                    meta.update(ambiguous_handler=True, executable=False, help_source="unknown")
                    continue
                permission_expression = _expr(fields[-2])
                console = _expr(fields[-1])
                allow = {"Console::Yes": True, "Console::No": False,
                         "Acore::ChatCommands::Console::Yes": True,
                         "Acore::ChatCommands::Console::No": False}.get(console)
                permission = _permission(permission_expression, self.symbols)
                if permission["kind"] == "unknown":
                    self.gap(path, entry[0].line, "unknown_permission", permission_expression)
                if allow is None:
                    self.gap(path, entry[0].line, "unknown_console", console)
                node.update(handler=handler, permission=permission, allow_console=allow,
                            source_path=path, source_line=entry[0].line)
                meta.update(namespace=False, executable=True, default_handler=not name,
                            permission_expression=permission_expression,
                            source_permission=dict(permission), console_expression=console)
                help_key = _expr(fields[2]) if len(fields) == 5 else "0"
                help_number = _number(help_key, self.symbols)
                if help_key in ("AcoreStrings()", "AcoreStrings{}"):
                    help_number = 0
                meta["help_key"] = help_key
                meta["help_id"] = help_number
                if help_number == 0:
                    meta["help_source"] = "unknown"
                else:
                    meta["help_source"] = "acore_string"
                    help_text = self.strings.get(help_number, self.strings.get(help_key))
                    if help_text is None:
                        self.gap(path, entry[0].line, "unresolved_help", help_key)
                    else:
                        node["help"], node["syntax"] = help_text, _syntax(help_text)

        visit(returns[0], "", ())

    def overrides(self, rows: Iterable[Mapping[str, Any]]) -> list[dict]:
        unmatched = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
                raise ValueError("command override requires a string name")
            name, help_text, level = row["name"], row.get("help", ""), row.get("security")
            if not isinstance(help_text, str) or type(level) is not int or not 0 <= level <= 255:
                raise ValueError("command override requires uint8 security and string help")
            key = " ".join(part for part in name.split(" ") if part).casefold()
            node = self.nodes.get(key)
            if node is None:
                unmatched.append({"name": name, "security": level, "row": index})
                continue
            meta = node["metadata"]
            if meta.get("ambiguous_handler"):
                self.gap(node["source_path"], node["source_line"], "ambiguous_override", name)
                continue
            receipt = {"name": name, "security": level, "permission_applied": bool(node["handler"]),
                       "help_applied": meta["help_source"] == "unknown"}
            meta.setdefault("command_overrides", []).append(receipt)
            if node["handler"]:
                node["permission"] = _permission(str(level), self.symbols)
            if meta["help_source"] == "unknown":
                node["help"], node["syntax"] = help_text, _syntax(help_text)
                meta["help_source"] = "command"
            elif meta["help_source"] == "command":
                self.gap(node["source_path"], node["source_line"], "duplicate_override", name)
        return unmatched


def export_commands(sources: Mapping[str, str], *,
                    command_overrides: Iterable[Mapping[str, Any]] = (),
                    acore_strings: Mapping[int | str, str] | None = None,
                    symbols: Mapping[str, int] | None = None) -> dict:
    """Export a caller-selected installed source inventory without any I/O.

    Keys are relative provenance paths, not paths to open. ``acore_strings`` holds
    the caller-selected locale (ID or exact source symbol -> text); ``symbols``
    contains observed integer constants, e.g. from Language.h/RBAC.h. SQL rows
    retain supplied order: core applies each permission, but only the first
    legacy help. There is no inferred syntax from handler parameter types.
    """
    symbols, acore_strings = symbols or {}, acore_strings or {}
    if any(not isinstance(k, str) or type(v) is not int for k, v in symbols.items()):
        raise ValueError("symbols require string names and integer values")
    if any(type(k) not in (int, str) or not isinstance(v, str) for k, v in acore_strings.items()):
        raise ValueError("acore_strings requires integer/string keys and string values")
    normalized = {}
    for path, source in sources.items():
        if not isinstance(path, str) or not isinstance(source, str):
            raise ValueError("sources require string relative paths and source text")
        path = path.replace("\\", "/")
        if (not path or path.startswith("/") or ":" in path
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise ValueError("source paths must be normalized relative provenance paths")
        if path in normalized:
            raise ValueError("duplicate normalized source path")
        normalized[path] = source
    result = _Export(symbols, acore_strings)
    lexed = {}
    for path, source in sorted(normalized.items()):
        try:
            lexed[path], _ = _lex(source)
        except ValueError as exc:
            result.gap(path, None, "lexical_error", str(exc))
            lexed[path] = []
    result.index(lexed)
    for path, source in sorted(normalized.items()):
        result.source(path, source, lexed)
    unmatched = result.overrides(command_overrides)
    records = sorted(result.nodes.values(), key=lambda row: (row["name"].casefold(), row["name"]))
    # Shared handlers are source aliases, not inferred abbreviations. Preserve all names.
    handlers: dict[tuple, list[str]] = {}
    for record in records:
        record["metadata"]["syntax_source"] = record["metadata"]["help_source"] if record["syntax"] else "unknown"
        if record["syntax"] and record["handler"]:
            spelled = record["syntax"].split()
            if spelled:
                spelled[0] = spelled[0].removeprefix(".").removeprefix("!")
            expected = record["name"].split(" ")
            matches = [part.casefold() for part in spelled[:len(expected)]] == [p.casefold() for p in expected]
            record["metadata"]["syntax_name_matches"] = matches
            if not matches:
                result.diagnostics.append({"source_path": record["source_path"],
                                           "source_line": record["source_line"],
                                           "reason": "syntax_name_mismatch",
                                           "detail": record["name"] + ": " + record["syntax"]})
        if record["handler"]:
            key = (tuple(record["metadata"]["scripts"]), record["handler"])
            handlers.setdefault(key, []).append(record["name"])
    for record in records:
        if record["handler"]:
            key = (tuple(record["metadata"]["scripts"]), record["handler"])
            record["metadata"]["same_handler_names"] = handlers[key]
    gaps = sorted(result.unresolved,
                  key=lambda row: (row["source_path"], row["source_line"] or 0, row["reason"], row["detail"]))
    return {"format": "ac-command-export-v1", "complete": True, "expected_records": len(records),
            "coverage": {"verification": "source-observed", "registered": len(records),
                         "parsed_definitions": sum(s["definitions"] for s in result.sources),
                         "unresolved_count": len(gaps), "unresolved": gaps,
                         "unmatched_overrides": unmatched, "sources": result.sources,
                         "non_definitions": result.non_definitions, "diagnostics": result.diagnostics},
            "records": records}
