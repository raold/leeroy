"""Heat through a real MindApp (FakeLLM, no bridge): the switch, the tune op's set, undo, preview
and session action, and the world romance dial as a derived value. Nothing here loads a model."""
import pytest

from conftest import run
from leeroy_mind.heat import live as heat_live
from leeroy_mind.heat.table import P2
from leeroy_mind.tuning.preview import UNSET_BEFORE, UNSET_CARD, UNSET_OFFSET
from leeroy_mind.tuning.service import HEAT_OFF
from tuning.test_tuning_preview import TOML, _app, _written

TOML = TOML + "[romance]\ndial = 9\n"  # the owner's own dial, as the engine tests run at
HEAT_ON = TOML + "[heat]\nenabled = true\n"


def _heat_app(root, toml=HEAT_ON, **kw):
    """test_tuning_preview's app (a lust woman, 102, and the test cast) with [heat] in mind.toml;
    its helper writes TOML itself, so the table is put back before the app reads it."""
    import tuning.test_tuning_preview as base
    old, base.TOML = base.TOML, toml
    try:
        return _app(root, **kw)
    finally:
        base.TOML = old


def _tune(app, **args):
    return run(app.tuning.handle(args))


def _set(app, scope, target, value):
    return _tune(app, action="set", scope=scope, target=target, dial="heat", value=value,
                 previewed=False)


def test_off_nothing_is_attached_and_heat_is_refused_in_words(tmp_path):
    app = _heat_app(tmp_path, TOML + "[heat]\nenabled = false\n")
    try:
        assert app.heat is None and app.engine.heat is None and heat_live.current() is None
        out = _set(app, "world", "", 5)
        assert out == {"ok": False, "code": "off", "error": HEAT_OFF}
        assert _tune(app, action="heat", session="clean", guid=None)["code"] == "off"
        assert _tune(app, action="preview", scope="world", target="", dial="heat",
                     value=5)["code"] == "off"
        assert "heat" not in _tune(app, action="status")
        assert _written(app)["tuning_change"] == 0
    finally:
        run(app.stop())


def test_on_and_unset_heat_governs_nobody_and_the_romance_dial_is_in_force(tmp_path):
    app = _heat_app(tmp_path)
    try:
        assert app.heat is heat_live.current() is app.engine.heat
        dial = app.file_cfg.romance.dial
        status = _tune(app, action="status")["heat"]
        assert status["enabled"] and not status["public_cap"] and not status["set"]
        assert app.cfg.romance.dial == dial  # nothing derived until heat is set
        p = app.personas.get(102)
        assert app.heat.resolve(p).dial == dial  # what it would be, for the page
        # ...but it governs nobody until a world heat is set: every reader's old path
        assert app.heat.turn(p) is None and not app.heat.reaches(p)
        assert (app.heat.starts(p), app.heat.offers(p), app.heat.photo_tier(p),
                app.heat.never_starts(p), app.heat.member_dial(p, 7)) == (1.0, 1.0, 4, False, 7)
        assert _set(app, "member", "102", -5)["ok"]  # a stored offset waits for a world value
        assert app.heat.turn(app.personas.get(102)) is None
    finally:
        run(app.stop())
    assert heat_live.current() is None  # stopping detaches it


def test_setting_the_world_derives_the_romance_dial_and_clearing_gives_it_back(tmp_path):
    app = _heat_app(tmp_path)
    try:
        file_dial = app.file_cfg.romance.dial
        out = _set(app, "world", "", 6)
        assert out["ok"] and out["reach"] == 1  # the one woman in this cast
        assert app.cfg.romance.dial == 7 and app.engine.cfg.romance.dial == 7
        assert app.file_cfg.romance.dial == file_dial  # mind.toml's own is never written
        r = app.heat.resolve(app.personas.get(102), lead=False)
        assert (r.heat, r.tier, r.photo) == (6, "suggestive", P2)
        assert _tune(app, action="status")["heat"] == {
            "enabled": True, "public_cap": False, "world": 6, "set": True,
            "sessions": {"all": None, "members": {}}}
        assert _set(app, "member", "102", 3)["ok"]
        assert app.heat.resolve(app.personas.get(102), lead=False).heat == 9
        assert not _set(app, "member", "101", 1)["ok"]  # a man: refused in words
        assert not _set(app, "world", "", 11)["ok"]
    finally:
        run(app.stop())


def test_undo_puts_the_world_back(tmp_path):
    app = _heat_app(tmp_path)
    try:
        file_dial = app.file_cfg.romance.dial
        out = _set(app, "world", "", 3)
        assert app.cfg.romance.dial == 3
        assert _tune(app, action="undo", change=out["change"])["ok"]
        assert app.cfg.romance.dial == file_dial and app.personas.tuning.heat_world is None
        assert _set(app, "world", "", None)["change"] is None  # already unset
    finally:
        run(app.stop())


def test_the_session_action_for_everyone_and_for_one_member(tmp_path):
    app = _heat_app(tmp_path)
    try:
        out = _tune(app, action="heat", session="clean", guid=None)
        assert out["ok"] and out["sessions"]["all"] == {"mode": "clean", "ends": "morning"}
        assert "Clean for everyone" in out["message"]
        assert app.heat.resolve(app.personas.get(102), lead=False).heat == 2
        assert _tune(app, action="heat", session="clear", guid=None)["ended"] == 1
        one = _tune(app, action="heat", session="open", guid=102)
        assert one["ok"] and one["sessions"]["members"] == {"102": {"mode": "open",
                                                                    "ends": "morning"}}
        assert app.heat.resolve(app.personas.get(102), lead=False).heat == 10
        refused = _tune(app, action="heat", session="open", guid=101)  # a man: never his stance
        assert not refused["ok"] and "does not reach" in refused["error"]
        assert _written(app)["tuning_change"] == 0  # a mood of the evening, not a setting
    finally:
        run(app.stop())


def test_the_preview_explains_in_the_codes_own_words_and_writes_nothing(tmp_path):
    from leeroy_mind import heat as H
    app = _heat_app(tmp_path)
    try:
        before = _written(app)
        out = _tune(app, action="preview", scope="world", target="", dial="heat", value=6)
        assert out["ok"] and out["dial"] == "heat"
        heat = out["heat"]
        assert (heat["before"], heat["after"]) == (None, 6)  # not set: the dial is in force
        assert heat["explain"] == H.explain_row(6)
        assert heat["changes"] == UNSET_BEFORE + H.what_changes(9, 6)
        assert "string bikini" not in heat["photos"]["outfits"]
        assert "bodycon mini dress" in heat["photos"]["outfits"]
        member = out["members"][0]
        assert member["guid"] == 102 and member["after"]["title"].startswith("Heat 6 (world 6")
        assert member["before"]["title"] == UNSET_CARD
        assert out["kind"] == "lines"  # the live model is resident in this rig: a line each way
        line = out["subjects"][0]
        assert line["before"]["text"] and line["after"]["text"]
        assert _written(app) == before
        waits = _tune(app, action="preview", scope="member", target="102", dial="heat", value=-4)
        assert waits["note"] == UNSET_OFFSET and waits["members"][0]["after"]["title"] == UNSET_CARD
        assert _set(app, "world", "", 9)["ok"]
        one = _tune(app, action="preview", scope="member", target="102", dial="heat", value=-4)
        assert one["members"][0]["after"]["title"] == "Heat 5 (world 9, hers -4)"
        assert one["members"][0]["before"]["title"] == "Heat 9 (world 9, hers +0)"
    finally:
        run(app.stop())


def test_the_preview_without_the_model_still_explains(tmp_path):
    app = _heat_app(tmp_path, loaded=False)
    try:
        out = _tune(app, action="preview", scope="world", target="", dial="heat", value=4)
        assert out["ok"] and out["kind"] == "numbers" and out["note"]
        assert out["heat"]["after"] == 4 and out["members"]
    finally:
        run(app.stop())


@pytest.mark.parametrize("toml", [TOML + "[heat]\nenabled = 1\n",
                                  TOML + "[heat]\nenabled = true\nmore = true\n"])
def test_an_invalid_table_is_off(tmp_path, toml):
    app = _heat_app(tmp_path, toml)
    try:
        assert app.heat is None
    finally:
        run(app.stop())
