import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import seed_test_cast
from leeroy_mind.endpoint import BadRequest, tune_args
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import PersonaCache, load_persona, save_persona
from leeroy_mind.talk.prompt import comms_rules
from leeroy_mind.text import estimate_tokens
from leeroy_mind.tuning import changes as C, nudges as N
from leeroy_mind.tuning.state import Tuning

HUMAN = HumanIdentity(1, "Testmage", "Zan", "Quillon", True)
TEXT = "Feel excited about tonight's raid"
ARGS = dict(action="nudge", verb="set", target_kind="member", target="102", text=TEXT,
            strength="light", scope="everywhere", seconds=3600)


@pytest.fixture
def tuned(store, monkeypatch):
    monkeypatch.setattr("leeroy_mind.tuning.state.time.time", lambda: 2000.0)
    seed_test_cast(store)
    store.set_meta("human_guid", "1")
    store.conn.commit()
    return store


def picture(store):
    return C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})


def add(store, **kw):
    return N.set_nudge(store, {**ARGS, **kw}, picture(store), HUMAN, source="console", now=2000)


def prompt(store, *, now=2000, channel="guild", recipient=None, guid=102):
    cache = PersonaCache(store)
    return comms_rules(HUMAN, cache.get(guid), now=now, channel=channel, recipient=recipient)


def test_no_nudge_is_byte_identical(tuned):
    p = load_persona(tuned, 102)
    assert prompt(tuned) == comms_rules(HUMAN, p)


def test_active_nudge_once_expiry_boundary_and_cache(tuned):
    base = load_persona(tuned, 102)
    add(tuned)
    p = PersonaCache(tuned).get(102)
    assert comms_rules(HUMAN, p, now=2000, channel="guild").count(TEXT) == 1
    assert TEXT not in comms_rules(HUMAN, p, now=5600, channel="guild")
    assert TEXT not in comms_rules(HUMAN, p, now=1999, channel="guild")
    assert TEXT not in comms_rules(HUMAN, p)  # no channel context: no private overlay
    assert load_persona(tuned, 102) == base
    assert TEXT not in prompt(tuned, guid=101)


@pytest.mark.parametrize("scope,channel,to,expected", [
    ("game", "guild", None, True), ("game", "whisper", 101, True),
    ("game", "discord:general", None, False), ("game", "dm:102", 1, False),
    ("discord", "discord:general", None, True), ("discord", "dm:102", 1, True),
    ("discord", "guild", None, False), ("discord", "voice:room", None, False),
    ("whispers", "whisper", 1, True), ("whispers", "dm:102", 1, True),
    ("whispers", "whisper", 101, False), ("whispers", "dm:102", 101, False),
    ("whispers", "guild", 1, False), ("whispers", "discord:officer", 1, False),
])
def test_scope_is_exact(tuned, scope, channel, to, expected):
    add(tuned, scope=scope)
    assert (TEXT in prompt(tuned, channel=channel, recipient=to)) is expected


def test_group_reaches_only_matches_and_retirement_stops_it(tuned):
    p = load_persona(tuned, 102)
    save_persona(tuned, replace(p, identity=replace(p.identity, player_gender="female")),
                 source="owner", ts=1800, race="Night Elf", class_name="Druid")
    C.save_group(tuned, None, "women", {"gender": ["female"]}, source="console", now=1900)
    add(tuned, target_kind="group", target="1")
    assert TEXT in prompt(tuned)
    assert TEXT not in prompt(tuned, guid=101)
    C.retire_group(tuned, 1, source="console", now=2100)
    assert TEXT not in prompt(tuned)


def test_undo_retirement_revert_and_redo_share_dial_history(tuned):
    r = add(tuned)
    N.retire(tuned, r.change, source="console", now=2100)
    assert TEXT not in prompt(tuned)
    u = C.undo(tuned, 2, picture(tuned), source="cli", now=2200)
    assert TEXT in prompt(tuned)
    C.undo(tuned, u.change, picture(tuned), source="console", now=2300)
    assert TEXT not in prompt(tuned)
    C.apply_sets(tuned, [C.Set("world", "", "chattiness", 9)], picture(tuned),
                 source="console", now=2400)
    C.revert_to(tuned, r.change, picture(tuned), source="console", now=2500)
    assert TEXT in prompt(tuned) and not Tuning.load(tuned.conn).world
    C.revert_to(tuned, 0, picture(tuned), source="cli", now=2600)
    assert not N.values(tuned.conn)
    assert len(N.view(tuned.conn, now=2600)["history"]) == 6


def test_undo_set_refuses_after_retire_without_partial_write(tuned):
    add(tuned)
    N.retire(tuned, 1, source="console", now=2100)
    with pytest.raises(C.TuneError, match="changed since"):
        C.undo(tuned, 1, picture(tuned), source="console", now=2200)
    assert tuned.conn.execute("SELECT count(*) FROM tuning_change").fetchone()[0] == 2


@pytest.mark.parametrize("text", ["Quillon is great", "quillon is great", "Q" * 201, "|\x00", "\ud800"])
def test_private_name_and_text_refusals_leave_no_rows(tuned, text, caplog):
    with pytest.raises(C.TuneError):
        add(tuned, text=text)
    assert not N.values(tuned.conn)
    assert not caplog.text


def test_cleaned_text_and_append_only_receipt(tuned):
    add(tuned, text="  Be |keen\x00\n tonight  ")
    assert N.values(tuned.conn)[1]["text"] == "Be keen tonight"
    receipt = tuned.get_meta(N.RECEIPT + "1")
    N.retire(tuned, 1, source="console", now=2100)
    assert tuned.get_meta(N.RECEIPT + "1") == receipt
    assert N.values(tuned.conn)[1]["retired"] == 2100


@pytest.mark.parametrize("change", [{"seconds": 3599}, {"seconds": 1209601},
    {"seconds": True}, {"strength": "system"}, {"scope": "all"},
    {"text": "x" * 1025}, {"target": []}, {"extra": "private"}])
def test_endpoint_shape_refusals(change):
    with pytest.raises(BadRequest):
        tune_args({**ARGS, **change})


def test_endpoint_accepts_all_nudge_verbs():
    assert tune_args(ARGS) == ARGS
    assert tune_args({**ARGS, "verb": "preview"})["verb"] == "preview"
    assert tune_args(dict(action="nudge", verb="list"))["verb"] == "list"
    assert tune_args(dict(action="nudge", verb="retire", id=1))["id"] == 1


def test_whole_nudge_lines_fit_prompt_budget(tuned):
    for i in range(12):
        add(tuned, text=f"Think about raid detail {i} " + "carefully " * 16)
    p = PersonaCache(tuned).get(102)
    lines = N.prompt_lines(p, now=2000, channel="guild", recipient=None, human=1)
    assert 0 < len(lines) < 12
    assert sum(estimate_tokens(s) for s in lines) <= N.PROMPT_TOKENS
    assert all(s.endswith("direction.]") for s in lines)


def test_night_mood_is_optional_and_never_crosses_a_limited_scope(tuned, tmp_path):
    from leeroy_mind.tuning.nudge_memories import past_moods
    add(tuned)
    add(tuned, text="Keep the private topic to yourself", scope="whispers")
    ctx = SimpleNamespace(store=tuned, settings=SimpleNamespace(data_dir=tmp_path),
                          tuning=Tuning.load(tuned.conn), human=1, window=(5500, 5700),
                          persona=lambda g: load_persona(tuned, g))
    hint = past_moods(ctx, 102)
    assert TEXT in hint and "private topic" not in hint
    assert "not evidence" in hint and "no longer directions" in hint
    (tmp_path / "mind.toml").write_text("[tuning]\nnudge_memories = false\n")
    assert past_moods(ctx, 102) == ""


@pytest.mark.parametrize("toml", ['[tuning]\nnudge_memories="yes"', 'tuning=4', '[tuning]\nunknown=true'])
def test_invalid_memory_settings_disable_hints(tmp_path, toml):
    from leeroy_mind.tuning.nudge_memories import enabled
    (tmp_path / "mind.toml").write_text(toml)
    assert not enabled(tmp_path)


def test_game_scope_covers_every_pipeline_game_channel(tuned):
    from leeroy_mind.talk.pipeline import COMMS_CHANNELS, GROUP_CHANNELS
    assert (COMMS_CHANNELS | GROUP_CHANNELS | {"say", "yell", "whisper"}) <= N.GAME_CHANNELS
    add(tuned, scope="game")
    for channel in sorted(N.GAME_CHANNELS) + ["voice:room"]:
        assert TEXT in prompt(tuned, channel=channel), channel
    assert TEXT not in prompt(tuned, channel="discord:general")


def test_ended_nudges_are_not_loaded(tuned):
    assert Tuning.load(tuned.conn, 2000).empty
    add(tuned)
    assert not Tuning.load(tuned.conn, 2000).empty
    assert not Tuning.load(tuned.conn, 5599).empty
    assert Tuning.load(tuned.conn, 5600).empty  # expired, never retired
    assert N.values(tuned.conn)  # the receipts and state stay for history and revert


def test_no_nudge_prompt_is_pinned(tuned):
    import hashlib
    out = comms_rules(HUMAN, PersonaCache(tuned).get(102), now=2000, channel="guild")
    assert out == comms_rules(HUMAN, load_persona(tuned, 102))
    assert hashlib.sha256(out.encode()).hexdigest() == "bf74a0a20994db64212bfe0ba6ce2d801039abf22579c2702079169ad6a55994"
