from pathlib import Path
import re
from types import SimpleNamespace as NS

import pytest

from leeroy_mind.library import mysql
from leeroy_mind.personas.roster import DbInfo
from leeroy_mind.realm import armory as realm_armory
from leeroy_mind.realm.armory import BotGuild, Candidate, RealmArmory, RealmArmoryUnavailable
from leeroy_mind.realm.ports import LivePorts, armory_from_config


def database(name="characters"):
    return DbInfo("localhost", 3306, "reader", "synthetic-secret", name)


def test_armory_is_inert_until_bounded_constant_rank_query(monkeypatch):
    calls = []
    def query(db, sql, exe, **kwargs):
        calls.append((db, sql, exe, kwargs))
        return [["4"], ["0"], ["1"]]
    monkeypatch.setattr(mysql, "query", query)
    armory = RealmArmory(database(), database("world"), database("auth"), Path("synthetic-mysql"))
    assert calls == []
    assert armory.guild_ranks(21) == [0, 1, 4]
    assert calls == [(armory.chars, "SELECT rid FROM guild_rank WHERE guildid=21 ORDER BY rid",
                      Path("synthetic-mysql"), dict(timeout=10, max_ms=5000))]


@pytest.mark.parametrize("guild_id", [True, 0, -1, 2**32, "21", 21.0])
def test_invalid_guild_id_never_queries(monkeypatch, guild_id):
    monkeypatch.setattr(mysql, "query", lambda *a, **k: pytest.fail("query must not start"))
    armory = RealmArmory(database(), database("world"), database("auth"), Path("synthetic-mysql"))
    with pytest.raises(ValueError):
        armory.guild_ranks(guild_id)


@pytest.mark.parametrize("rows", [[["4"], ["4"]], [["10"]], [["４"]], [[None]], [["1", "2"]], [["-1"]]])
def test_malformed_rank_evidence_is_unavailable(monkeypatch, rows):
    monkeypatch.setattr(mysql, "query", lambda *a, **k: rows)
    armory = RealmArmory(database(), database("world"), database("auth"), Path("synthetic-mysql"))
    with pytest.raises(RealmArmoryUnavailable, match="evidence unavailable"):
        armory.guild_ranks(21)


def test_query_error_suppresses_escaped_secrets_and_sql_details(monkeypatch):
    def fail(*args, **kwargs):
        raise mysql.QueryError("server stderr synthetic\\-secret SELECT configured-path")
    monkeypatch.setattr(mysql, "query", fail)
    armory = RealmArmory(database(), database("world"), database("auth"), Path("synthetic-mysql"))
    with pytest.raises(RealmArmoryUnavailable) as caught:
        armory.guild_ranks(21)
    assert str(caught.value) == "armory query unavailable"
    assert caught.value.__suppress_context__ is True


def test_invalid_database_identifier_is_rejected_before_io():
    with pytest.raises(RealmArmoryUnavailable, match="configuration unavailable"):
        RealmArmory(database("characters;DROP"), database("world"), database("auth"), Path("synthetic-mysql"))


def test_live_armory_reads_only_configured_credentials_lazily_and_caches(monkeypatch):
    from leeroy_mind.personas import roster
    calls = []
    def read(path, key="CharacterDatabaseInfo"):
        calls.append((path, key))
        return database(key)
    monkeypatch.setattr(roster, "read_db_info", read)
    cfg = NS(worldserver_conf=Path("synthetic-worldserver.conf"), mysql_exe=Path("synthetic-mysql"))
    app = NS(clock=object(), store=NS(conn=object()), human_guid=1, guild=NS(cfg=cfg))
    ports = LivePorts(app)
    assert calls == []
    backend = ports.armory()
    assert backend is not None and ports.armory() is backend
    assert calls == [(cfg.worldserver_conf, key) for key in
                     ("CharacterDatabaseInfo", "WorldDatabaseInfo", "LoginDatabaseInfo")]


def test_cli_config_error_is_unavailable_with_safe_diagnostic(tmp_path, caplog):
    path = tmp_path / "mind.toml"
    path.write_text('[guild]\nworldserver_conf = 3\n', encoding="utf-8")
    assert armory_from_config(path) is None
    assert "realm_armory_configuration_unavailable" in caplog.text
    assert str(path) not in caplog.text


# The realm's reads (Task 14 data, Task 16 H4): constant SELECTs only, parsed strictly.

WRITES = re.compile(r"\b(?:INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|TRUNCATE|GRANT|SET|CALL"
                    r"|LOCK|HANDLER|LOAD|RENAME|INTO)\b", re.IGNORECASE)


class FakeMysql:
    """Answers each query by its template name; records (db name, template name, sql)."""

    def __init__(self, monkeypatch, answers):
        self.answers, self.calls = answers, []
        monkeypatch.setattr(mysql, "query", self)

    def __call__(self, db, sql, exe, **kwargs):
        mysql.check_select(sql)
        assert kwargs == dict(timeout=10, max_ms=5000) and exe == Path("synthetic-mysql")
        name = template_of(sql)
        self.calls.append((db.name, name, sql))
        answer = self.answers[name]
        return answer(sql) if callable(answer) else answer


def template_of(sql):
    for name, template in realm_armory.QUERIES.items():
        pattern = re.escape(template)
        pattern = re.sub(r"\\\{[a-z_]+:d\\\}", r"-?[0-9]+", pattern)
        pattern = re.sub(r"\\\{[a-z_]+\\\}", r"[0-9]+(?:,[0-9]+)*", pattern)
        if re.fullmatch(pattern, sql):
            return name
    raise AssertionError(f"not a query constant: {sql}")


def in_list(sql):
    return [int(n) for n in re.search(r"IN \(([0-9,]+)\)", sql).group(1).split(",")]


def armory():
    return RealmArmory(database("characters"), database("world"), database("auth"), Path("synthetic-mysql"))


class _Sample(dict):
    def __missing__(self, key):
        return 7


def test_armory_queries_are_selects():
    assert realm_armory.QUERIES["guild_ranks"] == realm_armory._GUILD_RANKS
    for name, template in realm_armory.QUERIES.items():
        sql = template.format_map(_Sample())
        mysql.check_select(sql)
        assert sql.startswith("SELECT ") and WRITES.search(sql) is None, name
    from leeroy_mind import app
    assert realm_armory.GUILD_ID == app.GUILD_ID == 21


def test_bot_guilds_count_by_level_and_faction(monkeypatch):
    fake = FakeMysql(monkeypatch, {
        "guild_leaders": [["30", "Synthetic Horde", "501", "900", "2"],
                          ["31", "Synthetic Players", "502", "77", "1"],
                          ["32", "Synthetic Alliance", "503", "901", "4"]],
        "random_bot_accounts": lambda sql: [[str(a)] for a in in_list(sql) if a >= 900],
        "guild_member_levels": [["30", "80", "2", "12"], ["30", "80", "10", "3"], ["30", "79", "5", "4"],
                                ["32", "80", "4", "9"]],
    })
    guilds = armory().bot_guilds()
    assert [g.guild_id for g in guilds] == [30, 32]
    horde = guilds[0]
    assert horde == BotGuild(30, "Synthetic Horde", 501, "horde",
                             {(80, "horde"): 15, (79, "horde"): 4})
    assert horde.count() == 19 and horde.count(level=80) == 15 and horde.count(faction="alliance") == 0
    assert guilds[1].faction == "alliance" and guilds[1].count(level=80, faction="alliance") == 9
    assert [(db, name) for db, name, _ in fake.calls] == [
        ("characters", "guild_leaders"), ("auth", "random_bot_accounts"), ("characters", "guild_member_levels")]
    assert "<> 21 " in fake.calls[0][2] and in_list(fake.calls[1][2]) == [77, 900, 901]
    assert in_list(fake.calls[2][2]) == [30, 32]


def test_bot_guilds_without_bot_leaders_reads_nothing_more(monkeypatch):
    fake = FakeMysql(monkeypatch, {"guild_leaders": []})
    assert armory().bot_guilds() == [] and len(fake.calls) == 1


def test_guildless_80s_ilvl_parse(monkeypatch):
    fake = FakeMysql(monkeypatch, {
        "guildless_80s": [["601", "Synthbota", "2", "1", "900"], ["602", "Synthbotb", "8", "5", "900"],
                          ["603", "Synthalt", "5", "8", "77"], ["604", "Synthbotc", "6", "11", "901"]],
        "random_bot_accounts": lambda sql: [[str(a)] for a in in_list(sql) if a >= 900],
        # 601: head 200 and chest 220, never the shirt or tabard; 602: nothing equipped;
        # 604: one item the world database does not know
        "equipped": [["601", "0", "5001"], ["601", "3", "5003"], ["601", "4", "5002"],
                     ["601", "18", "5004"], ["604", "15", "5999"]],
        "item_levels": [["5001", "200"], ["5002", "220"], ["5003", "1"], ["5004", "1"]],
    })
    found = armory().guildless_80s("horde")
    assert found == [Candidate(601, "Synthbota", 2, 1, "horde", 210.0),
                     Candidate(602, "Synthbotb", 8, 5, "horde", 0.0),
                     Candidate(604, "Synthbotc", 6, 11, "horde", 0.0)]
    names = [(db, name) for db, name, _ in fake.calls]
    assert names == [("characters", "guildless_80s"), ("auth", "random_bot_accounts"),
                     ("characters", "equipped"), ("world", "item_levels")]
    assert in_list(fake.calls[0][2]) == [2, 5, 6, 8, 10]
    assert in_list(fake.calls[2][2]) == [601, 602, 604]
    assert in_list(fake.calls[3][2]) == [5001, 5002, 5003, 5004, 5999]
    with pytest.raises(ValueError):
        armory().guildless_80s("neutral")


def test_guildless_80s_chunks_long_lists(monkeypatch):
    guids = range(1000, 2201)
    fake = FakeMysql(monkeypatch, {
        "guildless_80s": [[str(g), f"Synth{g}", "1", "1", "900"] for g in guids],
        "random_bot_accounts": [["900"]], "equipped": [], "item_levels": [],
    })
    assert len(armory().guildless_80s("alliance")) == len(guids)
    sizes = [len(in_list(sql)) for _, name, sql in fake.calls if name == "equipped"]
    assert sizes == [500, 500, 201]
    assert in_list(fake.calls[0][2]) == [1, 3, 4, 7, 11]


@pytest.mark.parametrize("answers", [
    {"guildless_80s": [["601", "Synthbota", "2", "1"]]},
    {"guildless_80s": [["601", "Synthbota", "9", "1", "900"]]},
    {"guildless_80s": [["601", "", "2", "1", "900"]]},
    {"guildless_80s": [["601", "Synthbota", "2", "1", "900"]], "random_bot_accounts": [["900"]],
     "equipped": [["601", "19", "5001"]], "item_levels": []},
    {"guildless_80s": [["601", "Synthbota", "2", "1", "900"]], "random_bot_accounts": [["900"]],
     "equipped": [["601", "0", "5001"]], "item_levels": [["5001", None]]},
])
def test_guildless_80s_malformed_evidence_is_unavailable(monkeypatch, answers):
    FakeMysql(monkeypatch, answers)
    with pytest.raises(RealmArmoryUnavailable, match="evidence unavailable"):
        armory().guildless_80s("horde")


def test_guild_name_and_size(monkeypatch):
    fake = FakeMysql(monkeypatch, {"guild_name": lambda sql: [["Synthetic Guild"]] if "=21" in sql else [],
                                   "guild_size": [["106"]]})
    assert armory().guild_name(21) == "Synthetic Guild" and armory().guild_name(22) == ""
    assert armory().guild_size(21) == 106
    assert {db for db, _, _ in fake.calls} == {"characters"}
    for bad in (0, True, "21"):
        with pytest.raises(ValueError):
            armory().guild_name(bad)
        with pytest.raises(ValueError):
            armory().guild_size(bad)


def test_achievement_holders_parse(monkeypatch):
    fake = FakeMysql(monkeypatch, {"achievement_holders": [
        ["3117", "601", "Synthbota", "30"], ["3117", "602", "Synthbotb", None], ["3117", "603", None, None]]})
    assert armory().achievement_holders([4078, 3117, 3117]) == {
        3117: [(601, "Synthbota", 30), (602, "Synthbotb", 0), (603, "", 0)], 4078: []}
    assert in_list(fake.calls[0][2]) == [3117, 4078]
    assert armory().achievement_holders([]) == {} and len(fake.calls) == 1
    with pytest.raises(ValueError):
        armory().achievement_holders([70000])


def test_gm_level_and_perm8_read_only(monkeypatch):
    perms = {8: [["1"]], 9: [["1"], ["0"]], 10: []}
    fake = FakeMysql(monkeypatch, {
        "character_account": lambda sql: [["4242"]] if sql.endswith("=1") else [],
        "gm_level": [["3"]],
        "account_permission": lambda sql: perms[int(re.search(r"permissionId=([0-9]+)", sql).group(1))],
    })
    reads = armory()
    assert reads.account_gm_level(1) == 3
    assert reads.account_has_permission(1, 8) is True
    assert reads.account_has_permission(1, 9) is False  # a denial wins, as in the core's RBAC
    assert reads.account_has_permission(1, 10) is False
    assert [(db, name) for db, name, _ in fake.calls[:2]] == [("characters", "character_account"),
                                                              ("auth", "gm_level")]
    assert all(db == "auth" for db, name, _ in fake.calls if name in ("gm_level", "account_permission"))
    assert all(sql.startswith("SELECT ") and WRITES.search(sql) is None for _, _, sql in fake.calls)
    assert "accountId=4242 AND permissionId=8" in fake.calls[3][2]
    with pytest.raises(RealmArmoryUnavailable, match="character unavailable"):
        reads.account_gm_level(2)
    for bad in (0, -1, True, 2**32):
        with pytest.raises(ValueError):
            reads.account_has_permission(1, bad)
        with pytest.raises(ValueError):
            reads.account_gm_level(bad)


def test_gm_level_without_access_rows_is_zero(monkeypatch):
    FakeMysql(monkeypatch, {"character_account": [["4242"]], "gm_level": [[None]]})
    assert armory().account_gm_level(1) == 0


def test_realm_reads_suppress_query_errors(monkeypatch):
    def fail(*args, **kwargs):
        raise mysql.QueryError("server stderr synthetic\\-secret SELECT configured-path")
    monkeypatch.setattr(mysql, "query", fail)
    for call in (lambda a: a.bot_guilds(), lambda a: a.guildless_80s("horde"), lambda a: a.guild_name(21),
                 lambda a: a.guild_size(21), lambda a: a.achievement_holders([3117]),
                 lambda a: a.account_gm_level(1), lambda a: a.account_has_permission(1, 8)):
        with pytest.raises(RealmArmoryUnavailable) as caught:
            call(armory())
        assert str(caught.value) == "armory query unavailable" and caught.value.__suppress_context__


@pytest.mark.mysql
def test_armory_against_the_live_db_read_only():
    # Explicit opt-in only (pytest -m mysql): every read runs in mysql.query's read-only session.
    from leeroy_mind.personas import roster
    if not roster.DEFAULT_CONF.exists() or not roster.DEFAULT_MYSQL.exists():
        pytest.skip("no worldserver.conf or mysql.exe")
    reads = RealmArmory(*(roster.read_db_info(roster.DEFAULT_CONF, key) for key in
                          ("CharacterDatabaseInfo", "WorldDatabaseInfo", "LoginDatabaseInfo")),
                        roster.DEFAULT_MYSQL)
    assert all(isinstance(g, BotGuild) for g in reads.bot_guilds())
    assert reads.guild_name(21) and reads.guild_size(21) > 0
    assert all(c.avg_ilvl >= 0 for c in reads.guildless_80s("horde"))
    assert isinstance(reads.achievement_holders([3117]), dict)
