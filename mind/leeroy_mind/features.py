"""Optional features and their mind.toml tables: one policy for every attach.

An invalid optional table turns its feature off, with one WARNING naming the table and the key
(never the value), and the mind starts. Only the core tables that `config.load_config` checks
(models, bridge, talk, ...) and the data directory may stop a start. An attach that breaks for
any other reason is off too, named by its exception type.

`guard` is what `MindApp.__init__` wraps each attach in; `check_file` is the same check without a
mind, for `doctor`; `report` is what the control endpoint's `status` and `health` show."""
import importlib
import logging
import re
import tomllib
from pathlib import Path

from leeroy_mind.config import ConfigError

log = logging.getLogger("leeroy_mind")
# "guild.regear_to: expected ..." / "dkp: expected a table": the readers name the key only
_NAMED = re.compile(r"^([a-z_]+)(?:\.([A-Za-z0-9_.]+))?:")
# The readers that refuse a value (the others fall back to off by themselves): feature -> the
# reader of its tables, called with the path of mind.toml.
READERS = (
    ("guild", "leeroy_mind.guild.config:load_guild_config"),
    ("training", "leeroy_mind.training.config:load_training_config"),
    ("realm", "leeroy_mind.realm.config:load_realm_config"),
)


def named(error: BaseException, feature: str) -> tuple[str, str]:
    """The table and key a reader's refusal names; the feature's own table and no key when it
    names none. Nothing else of the message is ever used."""
    found = _NAMED.match(str(error))
    if found is None:
        return feature, ""
    return found.group(1), found.group(2) or ""


def disable(disabled: dict, feature: str, error: BaseException) -> None:
    """Record why `feature` is off and say so once."""
    if isinstance(error, ConfigError):
        table, key = named(error, feature)
        disabled[feature] = {"table": table, "key": key, "reason": "invalid_config"}
        log.warning("feature_off feature=%s table=[%s] key=%s reason=invalid_config",
                    feature, table, key or "-")
    else:
        code = type(error).__name__
        disabled[feature] = {"table": feature, "key": "", "reason": "attach_failed", "code": code}
        log.warning("feature_off feature=%s table=[%s] key=- reason=attach_failed code=%s",
                    feature, feature, code)


def guard(disabled: dict, feature: str, attach, off=None):
    """`attach()`, or `off` (a value, or a callable building one) with the feature recorded as
    disabled when it raised."""
    try:
        return attach()
    except Exception as error:
        disable(disabled, feature, error)
        return off() if callable(off) else off


_LINE = re.compile(r"^feature_off feature=([a-z_]+) table=\[([a-z_]+)\] key=(\S+) "
                   r"reason=(invalid_config|attach_failed)(?: code=([A-Za-z_][A-Za-z0-9_]*))?$")


def from_log(message: str) -> dict | None:
    """The `report` row a start's `feature_off` line stands for; None for any other line."""
    found = _LINE.match(message) if isinstance(message, str) else None
    if found is None:
        return None
    feature, table, key, reason, code = found.groups()
    row = {"feature": feature, "table": table, "key": "" if key == "-" else key, "reason": reason}
    return {**row, "code": code} if code else row


def report(disabled: dict) -> list[dict]:
    """The disabled features as plain rows (feature, table, key, reason[, code]), by feature."""
    return [{"feature": feature, **disabled[feature]} for feature in sorted(disabled)]


def check_file(path: Path) -> list[dict]:
    """What a start would turn off for this mind.toml's optional tables: `report` rows, without
    building a mind. A missing file disables nothing."""
    disabled: dict = {}
    try:
        tomllib.loads(Path(path).read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []  # absent, or not TOML at all: the core check's finding, not a table's
    logger_was = log.disabled
    log.disabled = True  # doctor prints its own line
    try:
        for feature, reader in READERS:
            module, name = reader.split(":")
            guard(disabled, feature, lambda: getattr(importlib.import_module(module), name)(Path(path)))
    finally:
        log.disabled = logger_was
    return report(disabled)


def words(row: dict) -> str:
    """One row as doctor and health print it."""
    if row["reason"] == "invalid_config":
        where = f"[{row['table']}]" + (f" {row['key']}" if row["key"] else "")
        return f"{row['feature']} is off: invalid {where} in mind.toml"
    return f"{row['feature']} is off: its attach failed ({row.get('code', 'error')})"
