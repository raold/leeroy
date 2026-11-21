import pytest

from leeroy_mind.guild.skills import professions, skills_sql


def test_professions_parse_and_drop_malformed_rows(caplog):
    rows = [["101", "333", "375"], ["101", "185", "450"], ["101", "164", "no"], ["x"], ["1", "42", "5"]]
    assert professions(rows) == {101: {333: 375, 185: 450}}
    assert "dropped=3" in caplog.text
    sql = skills_sql([102, 101, 102])
    assert "guid IN (101,102)" in sql and "skill IN (" in sql
    with pytest.raises(ValueError):
        skills_sql(["101"])
