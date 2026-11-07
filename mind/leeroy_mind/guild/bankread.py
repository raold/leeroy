"""The guild bank as the game database holds it: four read-only SELECTs for one guild and the
strict parse of their rows into what the console draws. Core constants are from Guild.h."""
import re

TABS = 6
SLOTS = 98  # GUILD_BANK_MAX_SLOTS
MONEY_TAB = 100  # GUILD_BANK_MONEY_LOGS_TAB
LOG_MAX = 50
MONEY_EVENTS = {4: "deposit", 5: "withdraw", 6: "repair", 9: "buy tab"}
NAME_MAX = 32

_DIGITS = re.compile(r"[0-9]{1,20}")


def bank_sql(guild_id: int) -> tuple[str, str, str, str]:
    """(money, tabs, items, money log) for one guild."""
    if type(guild_id) is not int or not 0 < guild_id <= 0xFFFFFFFF:
        raise ValueError("guild id must be a positive uint32 integer")
    g = f"{guild_id:d}"
    kinds = ",".join(str(k) for k in MONEY_EVENTS)
    return (
        f"SELECT BankMoney FROM guild WHERE guildid={g}",
        f"SELECT TabId, TabName FROM guild_bank_tab WHERE guildid={g} ORDER BY TabId",
        "SELECT b.TabId, b.SlotId, i.itemEntry, i.count FROM guild_bank_item b "
        f"JOIN item_instance i ON i.guid=b.item_guid WHERE b.guildid={g} "
        "ORDER BY b.TabId, b.SlotId",
        "SELECT TimeStamp, EventType, PlayerGuid, ItemOrMoney FROM guild_bank_eventlog "
        f"WHERE guildid={g} AND TabId={MONEY_TAB} AND EventType IN ({kinds}) "
        f"ORDER BY LogGuid DESC LIMIT {LOG_MAX}")


def _int(field, what: str) -> int:
    if not isinstance(field, str) or _DIGITS.fullmatch(field) is None:
        raise ValueError(f"{what} is not a number")
    return int(field)


def _fields(row, n: int, what: str) -> list:
    if len(row) != n:
        raise ValueError(f"{what} row has {len(row)} fields")
    return row


def _name(text: str) -> str:
    return "".join(c for c in text if c.isprintable())[:NAME_MAX]


def parse_bank(money, tabs, items, log) -> dict:
    """{"money", "tabs": [{"tab", "name", "slots": [[slot, item_id, count]]}], "log":
    [[ts, type, guid, amount]]} from the four queries' rows (lists of string fields)."""
    if len(money) != 1:
        raise ValueError("the guild's money row is missing")
    out_money = _int(_fields(money[0], 1, "money")[0], "money")
    by_tab: dict[int, dict] = {}
    for row in tabs:
        if len(row) not in (1, 2):
            raise ValueError("tab row has the wrong number of fields")
        t = _int(row[0], "tab")
        if t >= TABS or t in by_tab:
            raise ValueError("tab out of range or twice")
        by_tab[t] = {"tab": t, "name": _name(row[1]) if len(row) == 2 else "", "slots": []}
    seen = set()
    for row in items:
        t, s, item, count = (_int(f, "item field") for f in _fields(row, 4, "item"))
        if t >= TABS or s >= SLOTS or (t, s) in seen:
            raise ValueError("slot out of range or twice")
        seen.add((t, s))
        by_tab.setdefault(t, {"tab": t, "name": "", "slots": []})["slots"].append([s, item, count])
    for tab in by_tab.values():
        tab["slots"].sort()
    out_log = [[_int(f, "log field") for f in _fields(row, 4, "log")] for row in log]
    return {"money": out_money, "tabs": [by_tab[t] for t in sorted(by_tab)], "log": out_log}
