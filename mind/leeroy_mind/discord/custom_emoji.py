"""The guild's own emoji ([culture] emoji): original role and class icons, plain geometric SVGs
made for this repo (console/static/emoji/<name>.svg, under the repo's licence), named by a
shortcode such as `:tank:`. No third-party emoji set or emote art is copied.

A shortcode counts as one emoji only when it is registered here and the switch is on (the
caller says so: discord.text.is_one_emoji(custom=...)); an unregistered one is refused exactly as
a non-emoji is. The page mirrors this registry in console/static/js/comms/emoji.mjs, and a test
fails on any drift between the two."""
import re

SHORTCODE_RE = re.compile(r":[a-z0-9_]{2,14}:")
ROLES = ("tank", "healer", "dps")
CLASSES = ("warrior", "paladin", "hunter", "rogue", "priest", "deathknight", "shaman", "mage",
           "warlock", "druid")
NAMES = ROLES + CLASSES
CUSTOM: frozenset[str] = frozenset(f":{name}:" for name in NAMES)
assert all(SHORTCODE_RE.fullmatch(code) for code in CUSTOM)


def is_custom(text) -> bool:
    """A registered shortcode, exactly (`:tank:`), never one that merely looks like one."""
    return isinstance(text, str) and text in CUSTOM


def role_code(raid_role: str) -> str:
    """A persona's raid role (tank, healer, melee, ranged) as its role shortcode."""
    return {"tank": ":tank:", "healer": ":healer:"}.get(raid_role, ":dps:")
