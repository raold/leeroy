"""Static Wrath raid identifiers and validation for guild planning."""

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class Raid:
    key: str
    name: str
    map_id: int
    zone_id: int
    sizes: tuple[int, ...]
    heroic: bool
    min_tier: int
    bosses: int
    aliases: tuple[str, ...]


CATALOG: dict[str, Raid] = {
    "icc": Raid("icc", "Icecrown Citadel", 631, 4812, (10, 25), True, 16, 12,
                ("icc", "icecrown", "icecrown citadel", "lk", "lich king")),
    "toc": Raid("toc", "Trial of the Crusader", 649, 4722, (10, 25), True, 15, 5,
                ("toc", "togc", "trial", "trial of the crusader")),
    "ulduar": Raid("ulduar", "Ulduar", 603, 4273, (10, 25), False, 14, 14,
                   ("ulduar", "uld")),
    "naxx": Raid("naxx", "Naxxramas", 533, 3456, (10, 25), False, 13, 15,
                 ("naxx", "naxxramas")),
    "os": Raid("os", "The Obsidian Sanctum", 615, 4493, (10, 25), False, 13, 1,
               ("os", "obsidian sanctum", "sarth", "sartharion")),
    "eoe": Raid("eoe", "The Eye of Eternity", 616, 4500, (10, 25), False, 13, 1,
                ("eoe", "eye of eternity", "maly", "malygos")),
    # VoA emits no boss-state events and must not be used in offline rotations.
    "voa": Raid("voa", "Vault of Archavon", 624, 4603, (10, 25), False, 13, 4,
                ("voa", "vault", "vault of archavon", "archavon")),
    "ony": Raid("ony", "Onyxia's Lair", 249, 2159, (10, 25), False, 13, 1,
                ("ony", "onyxia")),
    "rs": Raid("rs", "The Ruby Sanctum", 724, 4987, (10, 25), True, 17, 1,
               ("rs", "ruby", "ruby sanctum", "halion")),
}


def validate(raid: Raid, size: int, heroic: bool, human_tier: int) -> str | None:
    """Check structural options before progression; booleans are not sizes."""
    if type(size) is not int or size not in raid.sizes:
        return "bad_size"
    if type(heroic) is not bool or (heroic and not raid.heroic):
        return "no_heroic"
    if type(human_tier) is not int or human_tier < raid.min_tier:
        return f"tier:{raid.min_tier}"
    return None


def split_key(key: str) -> tuple[Raid, int, bool]:
    """Decode a compact catalog key; reject aliases, spaces and unsupported modes.

    Case is ignored, but a size is mandatory. Malformed keys uniformly raise
    ValueError so configuration loaders can report them without leaking KeyError.
    """
    match = re.fullmatch(r"([a-z]+)(10|25)(h?)", key, re.IGNORECASE) if isinstance(key, str) else None
    if match is None:
        raise ValueError("invalid raid key")
    name, size_text, heroic_text = match.groups()
    raid = CATALOG.get(name.lower())
    if raid is None:
        raise ValueError("unknown raid key")
    size, heroic = int(size_text), bool(heroic_text)
    if validate(raid, size, heroic, raid.min_tier) is not None:
        raise ValueError("unsupported raid mode")
    return raid, size, heroic
