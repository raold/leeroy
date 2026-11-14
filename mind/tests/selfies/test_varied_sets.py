"""Varied sets (owner, 2026-11-14): four looks and poses in one set for the lust style, composed
by code from fixed lists, screened like every lust kind, queued as "4 more" is and rendered by
the night's stage. Every person here is invented; no model, GPU or real worker."""
import importlib.util
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import run
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.endpoint import BadRequest, selfie_args
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.selfies import cli, files, kind, varied
from leeroy_mind.selfies.config import SelfieCfg, load_selfie_config
from leeroy_mind.selfies.prompt import (BUILD_WORDS, LUST_NEGATIVE, VARIED_FRAMING_WORDS,
                                        VARIED_NEGATIVE, VARIED_VIEW_WORDS, build_prompt,
                                        build_varied_prompt, subject_phrase, varied_parts)
from leeroy_mind.selfies.scene import ref_crop
from leeroy_mind.selfies.store import (PICK_LOOK_KEY, VARIED_SET_KEY, lust_set, pick,
                                       picked_framing, queued_sets, ready_set)
from leeroy_mind.selfies.style import AGE_RE, LUST
from nightly.test_night_enrich import APPEARANCE
from selfies.fakes import fake_worker, ready

DAY = "2026-10-29"
KIND = ProfilePhoto("mirror_selfie", "upper_body", "the mirror in her hallway",
                    "a black fitted little dress", "a playful smile")
FORMAL = {"fitted little dress", "low-cut cocktail dress", "satin midi dress", "sequin party dress",
          "plunging wrap dress", "fitted blazer dress", "off-the-shoulder sweater dress"}
ON = SelfieCfg(enabled=True, varied_sets=True)


def lust(h, guid=101, **over):
    """A woman of the lust style with looks and a lust photo kind the lust rules chose."""
    p = h.woman(guid, "lust", appearance=over.pop("appearance", APPEARANCE), **over)
    p = replace(p, photo=KIND)
    save_persona(h.store, p, source="generated", ts=h.T, race="Human", class_name="Warrior")
    kind._record(h.store, guid, LUST, p.photo)
    return load_persona(h.store, guid)


def plain(h, guid):
    p = replace(h.persona(guid, appearance=APPEARANCE),
                photo=ProfilePhoto("snapshot", "upper_body", "a quiet cafe", "a grey hoodie",
                                   "a small smile"))
    save_persona(h.store, p, source="generated", ts=h.T, race="Human", class_name="Warrior")
    return p


def _words(text):
    return len(re.findall(r"\w+|[^\w\s]", text))


# -- composition --------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [str(n) for n in range(40)])
def test_a_set_has_four_outfits_and_four_poses_and_no_formal_dress(h, seed):
    p = lust(h)
    looks = varied.compose(p, seed)
    assert looks is not None and looks == varied.compose(p, seed)  # deterministic
    assert [x.category for x in looks] == list(kind.CATEGORIES)
    assert len({x.outfit for x in looks}) == 4 and len({x.view for x in looks}) == 4
    assert len({x.colour for x in looks}) == 4 and len({x.expression for x in looks}) == 4
    assert {x.view for x in looks} == set(kind.VARIED_VIEWS)
    assert not {x.outfit for x in looks} & FORMAL
    assert sum("dress" in x.outfit for x in looks) <= 1
    swim, gym, casual, going_out = looks
    assert swim.outfit in kind.SWIM_OUTFITS and swim.setting in kind.WATER_SETTINGS
    assert swim.setting not in kind.SWIM_ONLY_SETTINGS
    assert gym.outfit in kind.VARIED_GYM_OUTFITS and gym.setting in kind.GYM_SETTINGS
    for x in looks:
        assert not varied.screen(p, x)
        assert x.framing in kind.VARIED_FRAMINGS
        if x.view not in kind.BACK_VIEWS:
            assert x.framing == "full_body"


def test_seeds_vary_the_poses_so_swimwear_is_shot_from_behind_too(h):
    p = lust(h)
    views = {varied.compose(p, str(n))[0].view for n in range(60)}
    assert views == set(kind.VARIED_VIEWS)
    framings = {x.framing for n in range(60) for x in varied.compose(p, str(n))}
    assert framings == set(kind.VARIED_FRAMINGS)


def test_no_look_shares_her_name(h):
    p = lust(h, real_name="Coral")
    for n in range(30):
        looks = varied.compose(p, str(n))
        assert looks is not None and all(x.colour != "coral" for x in looks)
    clash = varied.Look("swim", "behind", "full_body", "a hotel pool deck", "string bikini",
                        "coral", "a wink")
    assert "photo.outfit: shares her name" in varied.screen(p, clash)
    assert "photo.wearing: shares her name" in varied.screen(p, clash)


def test_every_varied_value_is_vetted_and_off_the_age_guard():
    phrases = [*VARIED_VIEW_WORDS.values(), *VARIED_FRAMING_WORDS.values()]
    for category, (outfits, settings) in kind.VARIED_LOOKS.items():
        assert outfits and settings
        for outfit in outfits:
            assert outfit in kind.VARIED_OUTFITS
            phrases += [kind.outfit_phrase(outfit, c, kind.VARIED_OUTFITS)
                        for c in kind.LUST_COLOURS]
        phrases += [s for s in settings if s in kind.LUST_SETTINGS] + list(settings)
        assert set(settings) <= set(kind.LUST_SETTINGS)
    for text in phrases + list(kind.LUST_EXPRESSIONS):
        assert not AGE_RE.search(text), text
        assert not kind.WEAR_RE.search(text), text
        assert not re.search(r"\b(nude|naked|topless|lingerie|underwear|bra|panties|thong|"
                             r"see-through|sheer|sexy|nsfw)\b", text, re.I), text
    # the night's lust pass and its schema keep their lists: nothing new is offered there
    assert set(kind.LUST_OUTFITS) < set(kind.VARIED_OUTFITS)
    assert kind.LUST_SCHEMA["properties"]["outfit"]["enum"] == list(kind.LUST_OUTFITS)
    assert "view" not in kind.LUST_SCHEMA["properties"]


def test_the_age_guard_reads_every_composed_phrase(h, monkeypatch):
    p = lust(h)
    look = varied.Look("gym", "side", "full_body", "the gym", "sports top and leggings", "red",
                       "laughing")
    assert not varied.screen(p, look)
    monkeypatch.setitem(VARIED_VIEW_WORDS, "side", "side profile of a schoolgirl")
    assert "photo.view: age" in varied.screen(p, look)
    monkeypatch.setitem(VARIED_VIEW_WORDS, "side", "side profile view")
    monkeypatch.setitem(VARIED_FRAMING_WORDS, "full_body", "full-body shot, youthful look")
    assert "photo.framing: age" in varied.screen(p, look)


# -- pairings -----------------------------------------------------------------------------------

def _data(**over):
    d = {"kind": "snapshot", "framing": "full_body", "view": "behind", "setting": "the beach at sunset",
         "outfit": "string bikini", "colour": "red", "expression": "a wink"}
    d.update(over)
    return d


def test_swimwear_full_length_and_from_behind_only_in_a_varied_set(h):
    p = lust(h)
    assert not kind.screen_lust(p, _data(), varied=True)
    assert not kind.screen_lust(p, _data(framing="three_quarter", view="rear_three_quarter"),
                                varied=True)
    # outside a varied set: upper body only, and no pose at all
    plain_swim = {k: v for k, v in _data().items() if k != "view"}
    assert kind.PAIRINGS[3] in kind.screen_lust(p, plain_swim)
    assert kind.PAIRINGS[3] in kind.screen_lust(p, {**plain_swim, "framing": "three_quarter"})
    assert not kind.screen_lust(p, {**plain_swim, "framing": "upper_body"})
    assert kind.screen_lust(p, {**plain_swim, "framing": "upper_body", "view": "behind"}) == [
        kind.VIEW_OUTSIDE]


@pytest.mark.parametrize("over,problem", [
    ({"setting": "the gym"}, kind.PAIRINGS[0]),                          # swim away from water
    ({"setting": "a nightclub"}, kind.PAIRINGS[0]),
    ({"outfit": "cropped sports top and high-waisted leggings", "setting": "a cafe terrace"},
     kind.PAIRINGS[1]),                                                  # gym away from the gym
    ({"outfit": "sports top and leggings", "setting": "the beach at sunset"}, kind.PAIRINGS[1]),
    ({"outfit": "tank top and denim shorts", "setting": "a hot tub on a wooden deck"},
     kind.PAIRINGS[2]),                                                  # the hot tub: swim only
    ({"kind": "headshot"}, kind.PAIRINGS[4]),
    ({"framing": "upper_body"}, "photo.framing: not on its list"),
    ({"view": "lying down"}, "photo.view: not on its list"),
    ({"outfit": "string bikini underwear"}, "photo.outfit: not on its list"),
])
def test_every_pairing_holds_in_a_varied_set(h, over, problem):
    assert problem in kind.screen_lust(lust(h), _data(**over), varied=True)


def test_the_hot_tub_takes_swimwear_in_a_varied_set(h):
    assert not kind.screen_lust(lust(h), _data(setting="a hot tub on a wooden deck"), varied=True)


def test_new_gym_and_casual_outfits_stay_out_of_the_ordinary_lust_kind(h):
    p = lust(h)
    for outfit in ("racerback sports top and leggings", "flowy sundress"):
        reply = {"kind": "snapshot", "framing": "upper_body", "setting": "the gym",
                 "outfit": outfit, "colour": "red", "expression": "a wink"}
        assert "photo.outfit: not on its list" in kind.screen_lust(p, reply)


@pytest.mark.parametrize("category", list(kind.VARIED_LOOKS))
def test_every_listed_outfit_and_setting_of_a_category_pairs(h, category):
    p = lust(h)
    outfits, settings = kind.VARIED_LOOKS[category]
    for outfit in outfits:
        for setting in settings:
            look = varied.Look(category, "behind", "three_quarter", setting, outfit, "navy",
                               "a sultry look")
            assert not varied.screen(p, look), (outfit, setting)


# -- the prompt ---------------------------------------------------------------------------------

def _worker_fit():
    path = Path(__file__).resolve().parents[3] / "selfie" / "leeroy_selfie" / "fit.py"
    spec = importlib.util.spec_from_file_location("_selfie_fit_for_varied", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fit


def test_the_fit_keeps_pose_framing_build_and_outfit_for_the_longest_look(h):
    """Every priority-0 phrase stays inside the worker's window. Measured with the real
    RealVisXL tokenizers (2026-11-14) the longest required set is 68 of 75 tokens; words and
    marks count it 66, so the bound below fails as soon as a longer value is added."""
    fit = _worker_fit()
    big = replace(APPEARANCE, build="voluptuous", hair_style="h" * 40, face=("f" * 40,) * 4)
    p = lust(h, age=99, appearance=big)
    wearing = max(((o, c, s) for cat, (outfits, settings) in kind.VARIED_LOOKS.items()
                   for o in outfits for c in kind.LUST_COLOURS for s in settings),
                  key=lambda x: _words(f"wearing {kind.outfit_phrase(x[0], x[1], kind.VARIED_OUTFITS)}, {x[2]}"))
    cat = next(c for c, (o, _) in kind.VARIED_LOOKS.items() if wearing[0] in o)
    view = max(VARIED_VIEW_WORDS, key=lambda v: _words(VARIED_VIEW_WORDS[v]))
    framing = max(VARIED_FRAMING_WORDS, key=lambda v: _words(VARIED_FRAMING_WORDS[v]))
    look = varied.Look(cat, view, framing, wearing[2], wearing[0], wearing[1], "a sultry look")
    spec = build_varied_prompt(p, look)
    required = ", ".join(t for t, pr in spec.phrases if pr == 0)
    assert _words(required) <= 66
    kept, dropped = fit(spec.phrases, _words, 66)
    for text in (VARIED_VIEW_WORDS[view], VARIED_FRAMING_WORDS[framing], BUILD_WORDS["voluptuous"],
                 f"wearing {look.phrase}, {look.setting}", subject_phrase(p)[0]):
        assert text in kept and text not in dropped
    assert "f" * 40 in dropped


def test_a_varied_prompt_puts_pose_first_and_keeps_every_negative(h):
    p = lust(h)
    look = varied.compose(p, "x")[0]
    spec = build_varied_prompt(p, look)
    assert spec.phrases[0] == (VARIED_VIEW_WORDS[look.view], 0)
    assert spec.phrases[1] == (VARIED_FRAMING_WORDS[look.framing], 0)
    assert (BUILD_WORDS[APPEARANCE.build], 0) in spec.phrases  # the figure, always required here
    assert (f"wearing {look.phrase}, {look.setting}", 0) in spec.phrases
    assert set(LUST_NEGATIVE) < set(VARIED_NEGATIVE)
    zero = " ".join(t for t, pr in spec.negative if pr == 0)
    for word in ("nsfw", "nude", "topless", "lingerie", "underwear", "see-through", "sheer",
                 "child", "teenager", "celebrity", "thong"):
        assert word in zero
    for text, _ in spec.phrases:
        assert "arkon" not in text.lower()
    assert set(varied_parts(look, look.phrase)) == {"view", "framing", "setting", "outfit",
                                                     "expression", "wearing"}


@pytest.mark.parametrize("over", [{"stance": "fond"}, {"age": 24},
                                  {"appearance": replace(APPEARANCE, hair_style="pigtails")}])
def test_no_varied_prompt_without_the_lust_style(h, over):
    her = lust(h, guid=102)
    look = varied.compose(her, "x")[0]
    assert build_varied_prompt(her, look).phrases
    p = replace(her, identity=replace(her.identity, age=over.get("age", her.identity.age)),
                romance=replace(her.romance, stance=over.get("stance", "lust")),
                appearance=over.get("appearance", her.appearance))
    with pytest.raises(ValueError):
        build_varied_prompt(p, look)
    assert varied.looks_for(h.store, p, 102, 1) is None


def test_the_ordinary_prompt_is_untouched(h):
    p = lust(h)
    assert build_prompt(p, kind_style=LUST) == build_prompt(p, kind_style=LUST)
    assert build_prompt(p, kind_style=LUST).negative == LUST_NEGATIVE
    assert not any(t in VARIED_VIEW_WORDS.values() for t, _ in build_prompt(p, kind_style=LUST).phrases)


# -- queueing -----------------------------------------------------------------------------------

def _cast(h):
    lust(h, 101)
    lust(h, 102)
    plain(h, 103)  # not of the lust style
    lust(h, 104)
    h.woman(105, "lust", appearance=APPEARANCE)  # no photo kind yet: not renderable


def test_queue_plans_then_queues_once_a_day_for_the_lust_style_only(h):
    _cast(h)
    ready(h.store, h.settings.data_dir, 101, 1)  # a set waiting for his choice
    before = h.count("meta"), h.count("selfie_set")
    plan = varied.queue(h.store, human=1, now=h.T, day=DAY, go=False)
    assert (h.count("meta"), h.count("selfie_set")) == before  # the plan writes nothing
    assert [r["guid"] for r in plan["queued"]] == [101, 102, 104]
    assert plan["not_stance"] == 1 and plan["skipped"] == [{"guid": 105, "code": "not_renderable"}]
    assert plan["queued"][0]["set"] == 2 and len(plan["queued"][0]["looks"]) == 4
    done = varied.queue(h.store, human=1, now=h.T, day=DAY, go=True)
    assert [r["looks"] for r in done["queued"]] == [r["looks"] for r in plan["queued"]]
    assert [(s.guid, s.set_no, s.reason) for s in queued_sets(h.store)] == [
        (101, 2, "more"), (102, 1, "more"), (104, 1, "more")]
    assert ready_set(h.store, 101, 1).decided == "passed"  # as "4 more" leaves it
    for guid, set_no in ((101, 2), (102, 1), (104, 1)):
        stored = json.loads(h.store.get_meta(VARIED_SET_KEY.format(guid=guid, set_no=set_no)))
        assert stored["day"] == DAY and len(stored["looks"]) == 4
        assert varied.plan_of(h.store.conn, guid, set_no) is not None
    again = varied.queue(h.store, human=1, now=h.T, day=DAY, go=True)
    assert again["queued"] == [] and {r["code"] for r in again["skipped"]} == {
        "already_today", "not_renderable"}
    tomorrow = varied.queue(h.store, human=1, now=h.T, day="2026-10-30", go=True)
    assert tomorrow["queued"] == [] and {r["code"] for r in tomorrow["skipped"]} == {
        "already_queued", "not_renderable"}
    assert len(queued_sets(h.store)) == 3
    text = json.dumps(done)
    assert "Arkon" not in text and "bikini" not in text  # codes and indices only


def test_queue_refuses_another_stance(h):
    with pytest.raises(ValueError, match="stance_unsupported"):
        varied.queue(h.store, human=1, now=h.T, day=DAY, go=False, stance="fond")


def _root(h):
    root = h.settings.data_dir
    (root / "mind.toml").write_text("[selfies]\nenabled = true\nvaried_sets = true\n")
    return root


def test_cli_dry_run_reads_only_and_go_queues_with_the_mind_stopped(h, capsys):
    _cast(h)
    root = _root(h)
    h.store.conn.commit()
    assert cli.selfies_main(["vary", "--stance", "lust", "--data-dir", str(root)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True and plan["switch"] is True and not queued_sets(h.store)
    assert cli.selfies_main(["vary", "--stance", "lust", "--go", "--data-dir", str(root)]) == 0
    done = json.loads(capsys.readouterr().out)
    assert [r["guid"] for r in done["queued"]] == [101, 102, 104]
    assert {s.guid for s in queued_sets(h.store)} == {101, 102, 104}
    assert list((root / "snapshots").glob("*pre-vary*"))


def test_cli_go_asks_the_running_mind_and_needs_the_switch(h, capsys):
    _cast(h)
    root = _root(h)
    h.store.conn.commit()
    asked = []

    def call(r, args):
        asked.append(args)
        return {"ok": True, "queued": [101], "message": "1 varied sets will be rendered at night."}
    with acquire_instance_lock(root):
        assert cli.selfies_main(["vary", "--stance", "lust", "--data-dir", str(root)]) == 0
        capsys.readouterr()
        assert cli.selfies_main(["vary", "--stance", "lust", "--go", "--data-dir", str(root)],
                                call_mind=call) == 0
    assert asked == [{"action": "vary", "go": True}] and not queued_sets(h.store)
    assert json.loads(capsys.readouterr().out)["queued"] == [101]
    (root / "mind.toml").write_text("[selfies]\nenabled = true\n")
    assert cli.selfies_main(["vary", "--stance", "lust", "--go", "--data-dir", str(root)]) == 1
    assert "varied_sets = true" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.selfies_main(["vary", "--stance", "fond", "--data-dir", str(root)])


def test_the_selfie_op_takes_vary_strictly():
    assert selfie_args({"action": "vary", "go": False}) == {"action": "vary", "go": False}
    assert selfie_args({"action": "vary", "go": True}) == {"action": "vary", "go": True}
    for bad in ({"action": "vary"}, {"action": "vary", "go": 1},
                {"action": "vary", "go": True, "guid": 101}):
        with pytest.raises(BadRequest):
            selfie_args(bad)


def test_the_service_plans_and_queues_only_with_the_switch(h):
    from types import SimpleNamespace
    from leeroy_mind.personas.io import PersonaCache
    from leeroy_mind.selfies.service import SelfieService
    _cast(h)
    app = SimpleNamespace(store=h.store, clock=h.clock, data_dir=h.settings.data_dir,
                          personas=PersonaCache(h.store), human_guid=1)
    off = SelfieService(app, SelfieCfg(enabled=True))
    assert run(off.handle({"action": "vary", "go": True})) == {
        "ok": False, "error": "varied sets are off in mind.toml ([selfies] varied_sets = false)"}
    preview = run(off.handle({"action": "vary", "go": False}))
    assert preview["ok"] and preview["queued"] == [101, 102, 104] and not queued_sets(h.store)
    on = SelfieService(app, ON)
    got = run(on.handle({"action": "vary", "go": True}))
    assert got["ok"] and got["queued"] == [101, 102, 104]
    assert got["skipped"] == {"not_renderable": [105]} and got["not_stance"] == 1
    assert {s.guid for s in queued_sets(h.store)} == {101, 102, 104}


# -- rendering through the fake worker ------------------------------------------------------------

def _render(h, tmp_path, cfg):
    from leeroy_mind.selfies.stage import make_stage
    run(make_stage(cfg, worker=fake_worker(tmp_path))(h.ctx()))


def _prompts(h, guid, set_no):
    return [r[0] for r in h.store.conn.execute(
        "SELECT prompt FROM selfie_candidate WHERE guid=? AND set_no=? ORDER BY idx",
        (guid, set_no))]


def test_the_night_renders_each_candidate_in_its_own_look(h, tmp_path):
    lust(h, 101)
    varied.queue(h.store, human=1, now=h.T, day=DAY, go=True)
    looks = varied.plan_of(h.store.conn, 101, 1)
    _render(h, tmp_path, ON)
    prompts = _prompts(h, 101, 1)
    assert len(prompts) == 4 and len(set(prompts)) == 4
    for prompt, look in zip(prompts, looks):
        assert prompt.startswith(VARIED_VIEW_WORDS[look.view])
        assert f"wearing {look.phrase}, {look.setting}" in prompt
    assert lust_set(h.store.conn, 101, 1) and varied.plan_of(h.store.conn, 101, 1) == looks
    for i in range(4):
        assert files.candidate(h.settings.data_dir, 101, 1, i).is_file()


def test_with_the_switch_off_the_set_renders_ordinary_and_drops_its_plan(h, tmp_path):
    p = lust(h, 101)
    varied.queue(h.store, human=1, now=h.T, day=DAY, go=True)
    _render(h, tmp_path, SelfieCfg(enabled=True))
    prompts = _prompts(h, 101, 1)
    assert len(set(prompts)) == 1
    assert prompts[0] == ", ".join(t for t, _ in build_prompt(p, kind_style=LUST).phrases)
    assert varied.plan_of(h.store.conn, 101, 1) is None


def test_a_member_who_left_the_lust_style_never_renders_a_varied_look(h, tmp_path):
    lust(h, 101)
    varied.queue(h.store, human=1, now=h.T, day=DAY, go=True)
    p = load_persona(h.store, 101)
    save_persona(h.store, replace(p, romance=replace(p.romance, stance="fond")), source="owner",
                 ts=h.T, race="Human", class_name="Warrior")
    _render(h, tmp_path, ON)
    assert _prompts(h, 101, 1) == []  # her lust kind waits for a re-choice: held, not rendered
    assert queued_sets(h.store)[0].guid == 101


# -- the pick and the gallery ---------------------------------------------------------------------

def test_a_pick_records_its_look_and_the_gallery_names_it(h, tmp_path):
    from leeroy_mind.console.galleryview import gallery
    lust(h, 101)
    varied.queue(h.store, human=1, now=h.T, day=DAY, go=True)
    _render(h, tmp_path, ON)
    looks = varied.plan_of(h.store.conn, 101, 1)
    change = pick(h.store, 101, 1, 2, source="console", now=h.T)
    rec = json.loads(h.store.get_meta(PICK_LOOK_KEY.format(pick=change)))
    assert rec["view"] == looks[2].view and rec["outfit"] == looks[2].outfit
    assert picked_framing(h.store.conn, change) == looks[2].framing
    assert load_persona(h.store, 101).photo == KIND  # her photo kind is unchanged
    view = gallery(h.store.conn, h.settings.data_dir, 101)
    shown = {p["index"]: p.get("look") for p in view["photos"]}
    assert shown[2]["label"] == varied.label(looks[2]) and shown[2]["view"] == looks[2].view
    assert all(isinstance(v, dict) for v in shown.values())


def test_an_ordinary_pick_records_no_look_and_keeps_her_kinds_framing(h):
    lust(h, 101)
    ready(h.store, h.settings.data_dir, 101, 1)
    change = pick(h.store, 101, 1, 0, source="console", now=h.T)
    assert h.store.get_meta(PICK_LOOK_KEY.format(pick=change)) is None
    assert picked_framing(h.store.conn, change) is None


def test_a_full_length_cover_has_a_square_face_crop():
    l, t, r, b = ref_crop("full_body")
    assert 0 <= l < r <= 1 and 0 <= t < b <= 1 and abs((r - l) - (b - t)) < 1e-9


# -- the switch ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text,on", [("", False), ("[selfies]\nenabled = true\n", False),
                                     ("[selfies]\nvaried_sets = true\n", True),
                                     ("[selfies]\nvaried_sets = true\nbogus = 1\n", False)])
def test_the_switch_is_off_by_default_and_on_only_when_valid(tmp_path, text, on):
    path = tmp_path / "mind.toml"
    path.write_text(text)
    assert load_selfie_config(path).varied_sets is on
    assert SelfieCfg().varied_sets is False


def test_situational_photos_cut_her_face_by_a_varied_covers_framing(h, tmp_path):
    from types import SimpleNamespace
    from leeroy_mind.selfies import request_render, snapstage
    p = lust(h, 101)
    varied.queue(h.store, human=1, now=h.T, day=DAY, go=True)
    _render(h, tmp_path, ON)
    looks = varied.plan_of(h.store.conn, 101, 1)
    change = pick(h.store, 101, 1, 0, source="console", now=h.T)
    assert snapstage._framing(h.ctx(), p, change) == looks[0].framing
    assert request_render._framing(SimpleNamespace(store=h.store), p, change) == looks[0].framing
    ready(h.store, h.settings.data_dir, 101, 2)
    plain_pick = pick(h.store, 101, 2, 0, source="console", now=h.T)
    assert snapstage._framing(h.ctx(), p, plain_pick) == p.photo.framing
