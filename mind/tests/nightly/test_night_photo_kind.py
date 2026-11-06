"""The night's photo-kind pass (P2 Task 3): the reflection model picks each member's profile
photo kind, framing, setting, outfit and expression once they have looks. Fakes only; every
person here is invented."""
import logging
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.budget import UNIT_CALLS
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import Appearance, ProfilePhoto
from leeroy_mind.selfies.kind import (KIND_SCHEMA, STAGE, looks_line, needs_kind, screen_kind,
                                      stage_photo)
from leeroy_mind.wow import CLASSES, RACES
from nightly.conftest import IDENT
from nightly.test_night_enrich import APPEARANCE

KIND = {"kind": "snapshot", "framing": "upper_body", "setting": "on the deck of a small boat",
        "outfit": "orange rain bib over a grey hoodie", "expression": "squinting into the wind"}


def test_a_member_with_looks_and_no_photo_kind_gets_one_as_a_new_version(h):
    h.persona(101, appearance=APPEARANCE)
    v = h.store.persona_versions()[101]
    ctx = h.ctx({"photo": [KIND]})
    run(stage_photo(ctx))
    assert load_persona(h.store, 101).photo == ProfilePhoto(**KIND)
    assert h.store.persona_versions()[101] == v + 1 and ctx.done("photo", "101")


def test_members_without_looks_or_with_a_photo_kind_are_not_asked(h):
    h.persona(101)
    h.persona(102, appearance=APPEARANCE)
    stored = load_persona(h.store, 102)
    save_persona(h.store, replace(stored, photo=ProfilePhoto(**KIND)), source="owner", ts=h.T,
                 race=RACES[1], class_name=CLASSES[1])
    ctx = h.ctx({"photo": [KIND]})
    assert not needs_kind(load_persona(h.store, 101)) and not needs_kind(load_persona(h.store, 102))
    assert not needs_kind(None)
    run(stage_photo(ctx))
    assert ctx.reasoner.calls == 0
    assert load_persona(h.store, 101).photo is None
    assert not ctx.done("photo", "101") and not ctx.done("photo", "102")


@pytest.mark.parametrize("field,text,kind", [
    ("setting", "a retro arcade", "era"), ("setting", "a 2009 house party", "era"),
    ("setting", "a flip phone selfie in the kitchen", "era"), ("outfit", "lace lingerie", "explicit"),
    ("outfit", "a bikini", "explicit"), ("expression", "looks like a famous actress", "likeness")])
def test_the_screen_refuses_era_explicit_and_likeness(h, field, text, kind):
    p = h.persona(101, appearance=APPEARANCE)
    problems = screen_kind(p, {**KIND, field: text}, ident=IDENT, member_names=["Testb"])
    assert any(m.startswith(f"photo.{field}: {kind}") for m in problems)


def test_the_screen_refuses_a_member_name_his_real_name_and_meta(h):
    p = h.persona(101, appearance=APPEARANCE)
    names = ["Testb"]
    out = screen_kind(p, {**KIND, "setting": "a pier with testb"}, ident=IDENT, member_names=names)
    assert any(m.startswith("photo.setting: names a member") for m in out)
    out = screen_kind(p, {**KIND, "outfit": f"a shirt that says {IDENT.real_name}"}, ident=IDENT,
                      member_names=names)
    assert any(m.startswith("photo.outfit: real name") for m in out)
    assert IDENT.real_name not in " ".join(out)
    out = screen_kind(p, {**KIND, "expression": "As an AI language model, a smile"}, ident=IDENT,
                      member_names=names)
    assert any(m.startswith("photo.expression: ") for m in out)
    assert screen_kind(p, KIND, ident=IDENT, member_names=names) == []


def test_a_refused_reply_is_stored_nowhere_and_not_asked_again_tonight(h):
    h.persona(101, appearance=APPEARANCE)
    bad = {**KIND, "outfit": "lace lingerie"}
    ctx = h.ctx({"photo": [bad, bad, bad]})
    run(stage_photo(ctx))
    run(stage_photo(ctx))
    assert load_persona(h.store, 101).photo is None and ctx.done("photo", "101")
    assert ctx.reasoner.calls == 1


def test_a_photo_kind_set_by_hand_while_the_pass_ran_is_kept(h):
    h.persona(101, appearance=APPEARANCE)
    hand = ProfilePhoto("headshot", "face", "an office", "a blazer", "a calm smile")

    def by_hand(text):
        stored = load_persona(h.store, 101)
        save_persona(h.store, replace(stored, photo=hand), source="owner", ts=h.T,
                     race=RACES[1], class_name=CLASSES[1])
        return KIND
    ctx = h.ctx(default=by_hand)
    run(stage_photo(ctx))
    assert load_persona(h.store, 101).photo == hand


def test_women_and_men_both_get_one(h):
    h.persona(101, appearance=APPEARANCE)
    h.woman(102, "fond", appearance=APPEARANCE)
    ctx = h.ctx({"photo": [KIND, KIND]})
    run(stage_photo(ctx))
    assert load_persona(h.store, 101).photo and load_persona(h.store, 102).photo


def test_the_counter_counts_only_when_the_stage_is_installed(h):
    h.persona(101, appearance=APPEARANCE)
    h.persona(102, appearance=APPEARANCE)
    h.persona(103)
    ctx = h.ctx()
    others = frozenset({"coach", "realm"})
    assert "photo" not in budget._counts(ctx, set(), others)
    assert budget._counts(ctx, set(), others | {"photo"})["photo"] == 2
    ctx.settings = replace(ctx.settings, photo_per_night=1)
    assert budget._counts(ctx, set(), others | {"photo"})["photo"] == 1


def test_the_budget_prices_a_photo_unit_as_one_json_call():
    assert UNIT_CALLS["photo"] == {"json": 1} and "photo" in budget.EXTENSION_STAGES
    assert all("photo" not in step.name for step in budget.DEGRADE)


def test_logs_carry_no_reply_text(h, caplog):
    h.persona(101, appearance=APPEARANCE)
    bad = {**KIND, "setting": "a retro arcade"}
    ok = h.ctx({"photo": [bad]})
    with caplog.at_level(logging.DEBUG):
        run(stage_photo(ok))
    h.persona(102, appearance=APPEARANCE)
    with caplog.at_level(logging.DEBUG):
        run(stage_photo(h.ctx({"photo": [KIND]}, night="2026-10-30")))
    text = caplog.text
    assert "selfies_kind_refused" in text
    for s in (*KIND.values(), "retro arcade"):
        assert s not in text


def test_the_schema_and_looks_line():
    assert KIND_SCHEMA["title"] == "photo" and STAGE == "photo"
    assert list(KIND_SCHEMA["properties"]) == ["kind", "framing", "setting", "outfit", "expression"]
    line = looks_line(APPEARANCE)
    assert "168" in line and "hazel" in line and line.endswith(".") and "\n" not in line
