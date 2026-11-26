from dataclasses import dataclass
import tomllib


@dataclass(frozen=True)
class BanterConfig:
    enabled: bool = True
    per_hour: int = 2


OFF = BanterConfig(enabled=False)  # an invalid table or an unreadable file


def load(path):
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("banter", {})
        if (type(table) is not dict or set(table) - {"enabled", "per_hour"}
                or type(table.get("enabled", True)) is not bool
                or type(table.get("per_hour", 2)) is not int
                or not 0 <= table.get("per_hour", 2) <= 12):
            return OFF
        return BanterConfig(**table)
    except FileNotFoundError:
        return BanterConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return OFF
