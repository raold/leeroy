"""The battery's scripts and the night's seeded plan."""
import pytest

from leeroy_mind.lab import battery


def test_the_battery_has_thirty_scripts_over_every_kind():
    scripts = battery.load()
    assert len(scripts) == 30
    assert {s.kind for s in scripts} == set(battery.KINDS)
    assert all(1 <= len(s.lines) <= battery.MAX_LINES for s in scripts)
    asks = [line for s in scripts for line in s.lines if line.ask]
    assert {line.ask for line in asks} == set(battery.ASKS)
    # {name} is the only thing filled in, and only where a channel has others reading
    for s in scripts:
        for line in s.lines:
            assert "{" not in line.text.replace("{name}", "")


@pytest.mark.parametrize("doc, words", [
    ({"script": [{"id": "a", "kind": "nope", "channels": ["dm"], "lines": [{"text": "hi"}]}]}, "kind"),
    ({"script": [{"id": "a", "kind": "flirt", "channels": ["tv"], "lines": [{"text": "hi"}]}]}, "channels"),
    ({"script": [{"id": "a", "kind": "flirt", "channels": ["dm"], "lines": []}]}, "lines"),
    ({"script": [{"id": "a", "kind": "flirt", "channels": ["dm"], "lines": [{"text": "hi {who}"}]}]},
     "only {name}"),
    ({"script": [{"id": "a", "kind": "flirt", "channels": ["dm"],
                  "lines": [{"text": "hi", "ask": "noun"}]}]}, "nouns"),
    ({"script": [{"id": "a", "kind": "flirt", "channels": ["dm"],
                  "lines": [{"text": "hi", "ask": "maybe"}]}]}, "ask"),
    ({"other": []}, "[[script]]"),
])
def test_a_bad_script_is_refused_in_words(doc, words):
    with pytest.raises(battery.BatteryError, match=words.replace("[", r"\[").replace("{", r"\{")):
        battery.parse(doc)


def test_the_sample_draws_across_stances_and_is_seeded():
    members = {g: s for g, s in zip(range(1, 41), ["none", "fond", "drawn", "lust"] * 10)}
    first = battery.sample(members, "lab|2026-11-16")
    assert first == battery.sample(members, "lab|2026-11-16")
    assert first != battery.sample(members, "lab|2026-11-17")
    assert len(first) == battery.SAMPLE and len(set(first)) == battery.SAMPLE
    stances = [members[g] for g in first]
    assert {s: stances.count(s) for s in set(stances)} == {"none": 5, "fond": 5, "drawn": 5, "lust": 5}
    assert battery.sample({1: "weird"}, "s") == [1]  # an unknown stance samples as none


def test_the_plan_spreads_kinds_respects_channels_and_repeats_per_night():
    scripts = battery.load()
    members = {g: "none" for g in range(101, 111)}
    plan = battery.plan(scripts, members, "lab|n1", 12)
    assert plan == battery.plan(scripts, members, "lab|n1", 12)
    assert [p["pair"] for p in plan] == list(range(1, 13))
    by_id = {s.id: s for s in scripts}
    assert all(p["channel"] in by_id[p["script"]].channels for p in plan)
    assert len({p["kind"] for p in plan[:10]}) == 10  # one of each kind before a second
    assert len({p["guid"] for p in plan}) == 10
    only_dm = battery.plan(scripts, members, "lab|n1", 12, channels=("dm",))
    assert only_dm and all(p["channel"] == "dm" for p in only_dm)
    assert battery.plan(scripts, {}, "s", 5) == []


def test_five_scripts_and_three_members_cover_every_combination():
    scripts = battery.load()[:5]
    plan = battery.plan(scripts, {1: "none", 2: "fond", 3: "lust"}, "s", 15)
    assert len(plan) == 15
    assert {(p["script"], p["guid"]) for p in plan} == {(s.id, g) for s in scripts for g in (1, 2, 3)}
