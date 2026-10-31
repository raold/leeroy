import logging
from types import SimpleNamespace

import pytest

from leeroy_mind.config import ConfigError, load_config
from leeroy_mind.human import HumanIdentity, human_identity
from leeroy_mind.protocol import Member
from leeroy_mind.wow import faction_known


def _cfg(nickname="", real_name=""):
    return SimpleNamespace(human=SimpleNamespace(nickname=nickname, real_name=real_name))


def _member(guid, name, rank):
    return Member(guid=guid, name=name, race=10, class_=8, gender=0, level=83, rank=rank,
                  online=False, map=0, zone=0, spec_tab=-1)


def test_config_defaults_are_empty_and_validated(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert (cfg.human.nickname, cfg.human.real_name) == ("", "")
    p = tmp_path / "m.toml"
    p.write_text('[human]\nnickname = "Testy"\nreal_name = "Pat"\n', encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.human.nickname, cfg.human.real_name) == ("Testy", "Pat")


@pytest.mark.parametrize("snippet,key", [
    ('[human]\nnickname = "' + "x" * 25 + '"', "human.nickname"),
    ('[human]\nreal_name = "' + "x" * 25 + '"', "human.real_name"),
    ("[human]\nnickname = 5", "human.nickname")])
def test_config_rejects_bad_human_values(tmp_path, snippet, key):
    p = tmp_path / "m.toml"
    p.write_text(snippet, encoding="utf-8")
    with pytest.raises(ConfigError, match=key):
        load_config(p)


def test_human_section_is_not_warned_about(tmp_path, caplog):
    p = tmp_path / "m.toml"
    p.write_text('[human]\nnickname = "Testy"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        load_config(p)
    assert "human" not in caplog.text


def test_called_falls_back_to_the_character_name():
    assert HumanIdentity(1, "Testmage", "", "", True).called == "Testmage"
    assert HumanIdentity(1, "Testmage", "Testy", "", True).called == "Testy"


def test_describe_mentions_guild_master_only_for_rank_zero():
    gm = HumanIdentity(1, "Testmage", "Testy", "", True).describe()
    assert gm == "Testy (character Testmage), the guild master"
    other = HumanIdentity(1, "Testmage", "Testy", "", False).describe()
    assert "guild master" not in other and other == "Testy (character Testmage)"
    assert HumanIdentity(1, "Testmage", "", "", True).describe() == "Testmage, the guild master"


def test_describe_mentions_the_real_name_only_when_set():
    assert "real name" not in HumanIdentity(1, "Testmage", "Testy", "", True).describe(
        include_real_name=True)
    d = HumanIdentity(1, "Testmage", "Testy", "Pat", True).describe(include_real_name=True)
    assert d.endswith("; close friends know their real name, Pat")  # unset: they
    he = HumanIdentity(1, "Testmage", "Testy", "Pat", True, pronouns="he").describe(
        include_real_name=True)
    assert he.endswith("; close friends know his real name, Pat")


def test_human_identity_reads_name_and_rank_from_the_roster(store):
    store.upsert_cast([_member(1, "Testmage", 0), _member(2, "Arkon", 3)], 1000.0)
    h = human_identity(_cfg("Testy", "Pat"), store, 1)
    assert h == HumanIdentity(1, "Testmage", "Testy", "Pat", True, race=10)
    assert human_identity(_cfg(), store, 2).is_guild_master is False
    missing = human_identity(_cfg(), store, 99)
    assert missing.called and not missing.is_guild_master and missing.race == 0


def test_his_faction_is_his_races_and_unknown_without_one():
    """Fix wave part E, round 2: the facts are for his faction (a Blood Elf's is the Horde's)."""
    assert [faction_known(r) for r in (2, 5, 6, 8, 10)] == ["horde"] * 5
    assert [faction_known(r) for r in (1, 3, 4, 7, 11)] == ["alliance"] * 5
    assert faction_known(0) is None and faction_known(9) is None


def test_human_identity_takes_the_name_he_chats_under(store):
    """Pre-flight ruling 6: meta human_name (from his chat) first, then the roster."""
    store.upsert_cast([_member(1, "Testmage", 0), _member(2, "Arkon", 3)], 1000.0)
    store.set_meta("human_name", "Testmagey")
    assert human_identity(_cfg(), store, 1).character_name == "Testmage"  # not his guid yet
    store.set_meta("human_guid", "1")
    h = human_identity(_cfg(), store, 1)
    assert h.character_name == "Testmagey" and h.is_guild_master
    assert human_identity(_cfg(), store, 2).character_name == "Arkon"
    store.conn.execute("DELETE FROM cast_member")
    store.conn.commit()
    assert human_identity(_cfg(), store, 1).character_name == "Testmagey"


def test_describe_can_leave_the_real_name_out():
    h = HumanIdentity(1, "Testmage", "Testy", "Pat", True)
    assert h.describe(include_real_name=False) == "Testy (character Testmage), the guild master"


def test_config_strips_and_rejects_blank_names(tmp_path):
    p = tmp_path / "m.toml"
    p.write_text('[human]\nnickname = "  Testy "\n', encoding="utf-8")
    assert load_config(p).human.nickname == "Testy"
    p.write_text('[human]\nreal_name = "   "\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="human.real_name"):
        load_config(p)


def test_describe_includes_pronouns_only_when_set():
    assert HumanIdentity(1, "Testmage", "Testy", "", True, "he").describe() \
        == "Testy (character Testmage), the guild master, he/him"
    assert HumanIdentity(1, "Testmage", "Testy", "", False, "she").describe() \
        == "Testy (character Testmage), she/her"
    assert HumanIdentity(1, "Testmage", "Testy", "", True, "xe/xem").describe() \
        .endswith("the guild master, xe/xem")
    assert HumanIdentity(1, "Testmage", "Testy", "", True).describe() \
        == "Testy (character Testmage), the guild master"
    assert HumanIdentity(1, "Testmage", "Testy", "Pat", True, "she").describe(
        include_real_name=True) == ("Testy (character Testmage), the guild master, she/her; "
                                    "close friends know her real name, Pat")
    assert HumanIdentity(1, "Testmage", "Testy", "Pat", True, "she") \
        .describe(include_real_name=False) == "Testy (character Testmage), the guild master, she/her"


def test_config_pronouns_default_empty_and_validated(tmp_path):
    assert load_config(tmp_path / "missing.toml").human.pronouns == ""
    p = tmp_path / "m.toml"
    p.write_text('[human]\npronouns = " he "\n', encoding="utf-8")
    assert load_config(p).human.pronouns == "he"
    for bad in ('"' + "x" * 25 + '"', '"   "', "5"):
        p.write_text(f"[human]\npronouns = {bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="human.pronouns"):
            load_config(p)


def test_human_identity_reads_pronouns_from_config(store):
    store.upsert_cast([_member(1, "Testmage", 0)], 1000.0)
    cfg = SimpleNamespace(human=SimpleNamespace(nickname="Testy", real_name="", pronouns="he"))
    assert human_identity(cfg, store, 1).pronouns == "he"
    assert human_identity(_cfg("Testy"), store, 1).pronouns == ""  # older configs: none


def test_pronoun_forms_for_prompt_text():
    def forms(p):
        return HumanIdentity(1, "Testmage", "Testy", "", True, pronouns=p).forms
    assert forms("he") == forms("He/Him") == ("he", "him", "his")
    assert forms("she") == ("she", "her", "her")
    assert forms("they/them") == forms("") == forms("xe/xem") == ("they", "them", "their")


def test_describe_leaves_the_real_name_out_unless_asked():
    h = HumanIdentity(1, "Testmage", "Testy", "Pat", True, "he")
    assert h.describe() == "Testy (character Testmage), the guild master, he/him"
    assert h.describe(include_real_name=True).endswith("; close friends know his real name, Pat")
