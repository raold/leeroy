"""Prompt variants: named transforms of a captured turn's messages.

- `as_is`: the messages exactly as the live pipeline sent them.
- `situation`: the system prompt's "How you talk here:" list keeps only the three hard rules (no
  talk of AI; write only your line; no other people's lines) and drops the built-in soft rules
  (2009 slang, react to what happened, moody or wrong, no guilt-trip, answer (nothing), and the
  length rule). Everything else stays: the persona card, the flirting rule (a content
  invariant), the owner's tuning and nudge lines, the chat, the memories and the turn's own
  instruction. A bullet it does not recognise as a built-in soft rule (an owner overlay's
  wording) stays too. It is a stand-in for the design's section 9 prompt, which will also
  spend the freed tokens on what she is in the middle of; here they are only left unused."""
from leeroy_mind.talk.prompt import COMMS_RULES

RULES_HEAD = "How you talk here:"
NO_OTHERS = "- Don't narrate actions or write other people's lines."


def _is_hard(bullet: str) -> bool:
    return ("Never mention AI" in bullet or bullet.startswith("- Write only your line"))


def _soft_prefixes() -> tuple[str, ...]:
    """The opening words of each built-in rule bullet that is not a hard one (COMMS_RULES as
    this code has it), up to the first placeholder."""
    out = []
    for line in COMMS_RULES.split("\n"):
        if not line.startswith("- ") or _is_hard(line) or "other people's lines" in line:
            continue
        words = line.split("{", 1)[0].split()[:5]
        out.append(" ".join(words))
    return tuple(out)


SOFT = _soft_prefixes()


def _situation_system(text: str) -> str:
    lines = text.split("\n")
    try:
        head = lines.index(RULES_HEAD)
    except ValueError:
        return text
    end = head + 1
    while end < len(lines) and lines[end].startswith("- "):
        end += 1
    kept = []
    for bullet in lines[head + 1:end]:
        if _is_hard(bullet):
            kept.append(bullet)
        elif "other people's lines" in bullet:
            kept.append(NO_OTHERS)  # the length rule goes, the hard half stays
        elif any(" ".join(bullet.split()[:len(p.split())]) == p for p in SOFT):
            continue
        else:
            kept.append(bullet)
    return "\n".join(lines[:head + 1] + kept + lines[end:])


def situation(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        m = dict(m)
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            m["content"] = _situation_system(m["content"])
        out.append(m)
    return out


def as_is(messages: list[dict]) -> list[dict]:
    return [dict(m) for m in messages]


VARIANTS = {"as_is": as_is, "situation": situation}


def apply(name: str, messages: list[dict]) -> list[dict]:
    return VARIANTS[name](messages)
