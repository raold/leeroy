"""Read-only profession ranks from the characters database."""
import logging

PRIMARY = {164: "blacksmithing", 165: "leatherworking", 171: "alchemy", 197: "tailoring",
           202: "engineering", 333: "enchanting", 755: "jewelcrafting", 773: "inscription", 185: "cooking"}


def skills_sql(guids: list[int]) -> str:
    if not guids or any(type(g) is not int or not 0 < g <= 0xffffffff for g in guids):
        raise ValueError("invalid profession guids")
    ids = ",".join(map(str, sorted(set(guids))))
    skills = ",".join(map(str, sorted(PRIMARY)))
    return f"SELECT guid, skill, value FROM character_skills WHERE guid IN ({ids}) AND skill IN ({skills})"


def professions(rows) -> dict[int, dict[int, int]]:
    result, dropped = {}, 0
    for row in rows:
        try:
            if len(row) != 3 or not all(isinstance(s, str) and s.isascii() and s.isdecimal() for s in row):
                raise ValueError
            guid, skill, value = map(int, row)
            if not 0 < guid <= 0xffffffff or skill not in PRIMARY or not 0 < value <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            dropped += 1
            continue
        result.setdefault(guid, {})[skill] = value
    if dropped:
        logging.getLogger(__name__).warning("decisions_skills_invalid dropped=%d", dropped)
    return result
