"""AzerothCore .conf files: render a training config from its .dist and the dojo's training rules, and check one."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

KEY_LINE = re.compile(r"^(\s*)([A-Za-z0-9_.]+)(\s*=\s*)(.*?)\s*$")


class ConfError(Exception):
    pass


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        m = KEY_LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            values[m.group(2)] = unquote(m.group(4))
    return values


def unquote(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == '"' and value[-1] == '"' else value


def render(dist_text: str, overrides: dict[str, str]) -> str:
    """The .dist with each overridden key's value replaced in place (quoting kept); every override must exist."""
    seen: set[str] = set()
    out = []
    for line in dist_text.splitlines():
        m = KEY_LINE.match(line)
        if m and not line.lstrip().startswith("#") and m.group(2) in overrides:
            key = m.group(2)
            if key in seen:
                raise ConfError(f"{key} appears twice in the .dist")
            seen.add(key)
            value = overrides[key]
            if any(c in value for c in '"\r\n'):
                raise ConfError(f"refusing a value for {key} with a quote or line break")
            quoted = m.group(4).startswith('"') or value == "" or " " in value or "," in value
            out.append(f"{m.group(1)}{key}{m.group(3)}" + (f'"{value}"' if quoted else value))
        else:
            out.append(line)
    missing = sorted(set(overrides) - seen)
    if missing:
        raise ConfError(f"the .dist has no {', '.join(missing)}; the rules or the .dist are out of date")
    return "\n".join(out) + "\n"


@dataclass(frozen=True)
class FileRules:
    name: str
    dist: str
    isolation: frozenset[str]
    training: dict[str, str]
    ordinary: dict[str, str]


def load_rules(path: Path) -> dict[str, FileRules]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("kind") != "leeroy-dojo-training-rules" or data.get("format") != 1:
        raise ConfError(f"{path} is not a dojo training rules file")
    rules = {}
    for name, spec in data["files"].items():
        training = {k: str(v["value"]) for k, v in spec.get("training", {}).items()}
        ordinary = {k: str(v) for k, v in spec.get("ordinary", {}).items()}
        isolation = frozenset(spec.get("isolation", []))
        overlap = (set(training) & set(ordinary)) | (set(training) & isolation) | (set(ordinary) & isolation)
        if overlap:
            raise ConfError(f"{name}: {', '.join(sorted(overlap))} listed twice in the rules")
        rules[name] = FileRules(name, spec["dist"], isolation, training, ordinary)
    return rules


def check(rules: FileRules, conf: dict[str, str], dist: dict[str, str]) -> list[str]:
    """Every way conf breaks the rules (empty when it follows them). Values are never echoed for isolation keys."""
    problems = []
    for key, want in rules.ordinary.items():
        if dist.get(key) != want:
            problems.append(f"{key}: the .dist says {dist.get(key)!r}, the rules expect the stock {want!r}")
        if conf.get(key) != want:
            problems.append(f"{key} is {conf.get(key)!r}, not the ordinary {want!r}")
    for key, want in rules.training.items():
        if conf.get(key) != want:
            problems.append(f"{key} is {conf.get(key)!r}, not the training {want!r}")
    for key in sorted(set(dist) - set(conf)):
        problems.append(f"{key} is missing, so a compiled-in default would apply")
    for key in sorted(set(conf) - set(dist) - set(rules.training) - rules.isolation):
        problems.append(f"{key} is not in the .dist (an unreviewed setting)")
    for key in sorted(set(conf) & set(dist)):
        if key in rules.training or key in rules.isolation or key in rules.ordinary:
            continue
        if conf[key] != dist[key]:
            problems.append(f"{key} is {conf[key]!r}, not the stock {dist[key]!r}")
    return problems
