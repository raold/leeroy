"""One bad value in an optional mind.toml table never stops the mind: the app builds, the
feature that table switches is off (or runs on its defaults, for a table that only tunes one),
and the start says which table and key, never the value."""
import asyncio
import logging

import pytest

from selfies.fakes import mind_app
from leeroy_mind import features
from leeroy_mind.config import EXTENSION_SECTIONS

SECRET = "banana-7731"  # the bad value: it must never reach a log line
SHAPES = {
    "scalar": lambda table: f'{table} = "{SECRET}"\n',
    "enabled": lambda table: f'[{table}]\nenabled = "{SECRET}"\n',
    "unknown_key": lambda table: f'[{table}]\nbogus_key = "{SECRET}"\n',
}


def build(tmp_path, toml):
    app = mind_app(tmp_path, toml=toml)
    return app


def close(app):
    asyncio.run(app.stop())


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("table", sorted(EXTENSION_SECTIONS))
def test_a_bad_optional_table_never_stops_the_start(tmp_path, caplog, table, shape):
    with caplog.at_level(logging.DEBUG):
        app = build(tmp_path, SHAPES[shape](table))
    try:
        assert app.engine is not None and app.bridge is not None
        assert SECRET not in caplog.text
    finally:
        close(app)


# A value each table's own reader refuses, and the attribute of the app that is None while the
# feature is off. Every table whose reader can refuse a value is here.
REFUSED = [
    ("guild", "[guild]\nregear_to = 199\n", "regear_to", "guild"),
    ("guild", f'[guild]\noffline_raids_per_day = "{SECRET}"\n', "offline_raids_per_day", "guild"),
    ("dkp", "[dkp]\ndecay_hour = 99\n", "decay_hour", "guild"),
    ("training", f'[training]\nenabled = "{SECRET}"\n', "enabled", "training"),
    ("training", "[training]\nenabled = true\ngate_pulls = -1\n", "gate_pulls", "training"),
    ("realm", f'[realm]\nenabled = "{SECRET}"\n', "enabled", "realm"),
]


@pytest.mark.parametrize("table,toml,key,attribute", REFUSED)
def test_a_refused_value_turns_its_feature_off_and_names_the_key(tmp_path, caplog, table, toml, key,
                                                                 attribute):
    with caplog.at_level(logging.WARNING):
        app = build(tmp_path, toml)
    try:
        assert getattr(app, attribute) is None
        assert app.disabled_features[attribute] == {"table": table, "key": key, "reason": "invalid_config"}
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("feature_off")]
        assert lines == [f"feature_off feature={attribute} table=[{table}] key={key} reason=invalid_config"]
        assert SECRET not in caplog.text and "199" not in " ".join(lines)
    finally:
        close(app)


def test_a_valid_file_disables_nothing(tmp_path):
    app = build(tmp_path, "[guild]\nregear_to = 245\n[training]\nenabled = false\n")
    try:
        assert app.disabled_features == {} and app.guild is not None
    finally:
        close(app)


def test_an_attach_that_breaks_for_any_other_reason_is_off_too(tmp_path, monkeypatch, caplog):
    import leeroy_mind.app as app_module

    def broken(app):
        raise RuntimeError(f"a fault quoting {SECRET}")
    monkeypatch.setattr(app_module, "attach_claims", broken)
    with caplog.at_level(logging.WARNING):
        app = build(tmp_path, "")
    try:
        assert app.claims is None
        assert app.disabled_features["claims"] == {"table": "claims", "key": "", "reason": "attach_failed",
                                                   "code": "RuntimeError"}
        assert "feature_off feature=claims table=[claims] key=- reason=attach_failed code=RuntimeError" in caplog.text
        assert SECRET not in caplog.text
    finally:
        close(app)


def test_the_status_and_health_name_a_disabled_feature(tmp_path):
    app = build(tmp_path, "[guild]\nregear_to = 199\n")
    try:
        rows = features.report(app.disabled_features)
        assert rows == [{"feature": "guild", "table": "guild", "key": "regear_to", "reason": "invalid_config"}]
        assert features.words(rows[0]) == "guild is off: invalid [guild] regear_to in mind.toml"
        from leeroy_mind.endpoint import ControlEndpoint  # noqa: F401  (status carries the same rows)
    finally:
        close(app)


@pytest.mark.parametrize("table,toml,key,attribute", REFUSED)
def test_doctor_checks_the_optional_tables_without_a_mind(tmp_path, table, toml, key, attribute):
    path = tmp_path / "mind.toml"
    path.write_text(toml, encoding="utf-8")
    rows = [r for r in features.check_file(path) if r["feature"] == attribute]
    assert rows == [{"feature": attribute, "table": table, "key": key, "reason": "invalid_config"}]


def test_doctor_finds_nothing_in_a_valid_file(tmp_path):
    path = tmp_path / "mind.toml"
    path.write_text("[guild]\nregear_to = 245\n", encoding="utf-8")
    assert features.check_file(path) == []
    assert features.check_file(tmp_path / "absent.toml") == []
