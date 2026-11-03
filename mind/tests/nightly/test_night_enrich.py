"""Persona enrichment (P1c Task 7): looks, tastes and disclosure layers in P1b's persona schema,
left out of the schema P1b's generator and seeder send, and the one offline pass that fills them.
Fakes only: a FakeLLM answers for the reflection model. Every name and line here is invented."""
import asyncio
import json
import logging
from dataclasses import replace

import pytest

from conftest import TEST_CAST, make_persona, run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.enrich import (ENRICH_SCHEMA, TASTES_MAN, TASTES_WOMAN, run_enrichment,
                                        screen_enrichment, stage_enrich)
from leeroy_mind.nightly.llm import ReflectionError
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import (Appearance, Disclosure, PersonaError, persona_from_dict,
                                         persona_json_schema, persona_to_dict, validate_persona)
from leeroy_mind.wow import CLASSES, RACES
from nightly.conftest import IDENT, no_real_name

LOOKS = {"height_cm": 168, "build": "average", "weight_kg": 64, "hair_colour": "dark brown",
         "hair_length": "shoulder", "hair_style": "tucked behind one ear",
         "skin_tone": "light olive", "eye_colour": "hazel", "face": ["freckles", "thin glasses"],
         "at_the_pc": "an old hoodie, fluffy socks and a mug of tea"}
LAYERS = [
    {"depth": "surface", "text": "Works early shifts at a garden centre and raids with the radio on."},
    {"depth": "middle", "text": "Money is tight since the car died, so raid night is the cheap night in."},
    {"depth": "deep", "text": "Started playing the winter a brother moved abroad, to hear voices at night."}]
ENRICHED = {"appearance": LOOKS, "drawn_to": ["being remembered", "questing together"],
            "turned_off_by": ["being ignored"], "disclosures": LAYERS}
APPEARANCE = Appearance(**{**LOOKS, "face": tuple(LOOKS["face"])})


def by_name(replies: dict[str, dict]):
    """A scripted default: each persona's enrich call is answered by the reply for the
    character name its prompt asks about, so the order the calls arrive in does not matter."""
    return lambda text: next(r for name, r in replies.items() if name in text)


def units(h, night: str) -> list[str]:
    return [r[0] for r in h.store.conn.execute(
        "SELECT unit FROM night_step WHERE night = ? AND stage = 'enrich' ORDER BY unit", (night,))]


# -- the schema ----------------------------------------------------------------------------------

def test_old_persona_files_still_load():                           # a file written before Task 7 (review M1)
    d = persona_to_dict(make_persona(101, "Arkon"))
    for k in ("appearance", "disclosures"): d.pop(k)
    for k in ("drawn_to", "turned_off_by"): d["romance"].pop(k)
    p = persona_from_dict(d)
    assert p.appearance is None and p.disclosures == () and p.romance.drawn_to == () and validate_persona(p) == []


def test_generation_schema_is_unchanged():                         # controller ruling I9
    gen, full = persona_json_schema(), persona_json_schema(enrichment=True)
    assert not {"appearance", "disclosures"} & set(gen["properties"])
    assert not {"drawn_to", "turned_off_by"} & set(gen["properties"]["romance"]["properties"])   # what P1b's generator and seeder send
    assert {"appearance", "disclosures"} <= set(full["properties"]) and "appearance" not in full["required"]
    assert {"drawn_to", "turned_off_by"} <= set(full["properties"]["romance"]["properties"])


def test_schema_bounds_and_men():
    man = replace(make_persona(101, "Arkon"), romance=replace(make_persona(101, "Arkon").romance, drawn_to=("replies",)))
    assert "romance.drawn_to: men stay friends; leave it empty" in validate_persona(man)
    two_deep = replace(make_persona(102, "Belra", player_gender="female"),
                       disclosures=(Disclosure("surface", "a"), Disclosure("deep", "b"), Disclosure("deep", "c")))
    assert "disclosures: exactly one deep layer" in validate_persona(two_deep)
    with pytest.raises(PersonaError):
        persona_from_dict(persona_to_dict(make_persona(101, "Arkon")) | {"appearance": {"height_cm": "tall"}})
    # the rest of the rules: a man's turn-offs, the ladder's order, no deep layer at all, and a
    # woman's tastes and a well-ordered ladder of four, which pass
    off = replace(man, romance=replace(man.romance, drawn_to=(), turned_off_by=("rudeness",)))
    assert validate_persona(off) == ["romance.turned_off_by: men stay friends; leave it empty"]
    woman = make_persona(102, "Belra", player_gender="female")
    layer = lambda *depths: tuple(Disclosure(d, "a layer") for d in depths)            # noqa: E731
    assert validate_persona(replace(woman, disclosures=layer("middle", "surface", "deep"))) == [
        "disclosures: depths must run surface, then middle, then deep"]
    assert validate_persona(replace(woman, disclosures=layer("surface", "middle"))) == [
        "disclosures: exactly one deep layer"]
    keen = replace(woman, appearance=APPEARANCE, disclosures=layer("surface", "middle", "middle", "deep"),
                   romance=replace(woman.romance, drawn_to=("replies",), turned_off_by=("rudeness",)))
    assert validate_persona(keen) == [] and persona_from_dict(persona_to_dict(keen)) == keen
    tall = replace(woman, appearance=replace(APPEARANCE, height_cm=211, face=("a",) * 5))
    assert validate_persona(tall) == ["appearance.height_cm: must be at most 210",
                                      "appearance.face: must have at most 4 items"]


# -- the pass ------------------------------------------------------------------------------------

def test_enrich_woman_and_man(h):
    h.woman(102, "drawn")
    card = h.store.get_card(102)
    ctx = h.ctx({"enrich": [ENRICHED, ENRICHED]})
    report = run(run_enrichment(ctx, [101, 102], dry_run=False))
    belra, arkon = load_persona(h.store, 102), load_persona(h.store, 101)
    assert report.done == [101, 102] and belra.romance.drawn_to == ("being remembered", "questing together")
    assert arkon.appearance is not None and arkon.romance.drawn_to == () and len(arkon.disclosures) == 3
    assert h.store.persona_versions()[102] == 3 and load_persona(h.store, 102).romance.stance == "drawn"   # seed, woman, enrich
    assert report.refused == {} and report.path is None and units(h, h.night) == ["101", "102"]
    assert belra.appearance == APPEARANCE and [d.depth for d in belra.disclosures] == ["surface", "middle", "deep"]
    assert h.store.conn.execute("SELECT source FROM persona_core WHERE guid = 102").fetchone()[0] == "generated"
    assert ctx.persona(102).appearance == APPEARANCE                    # the cache was invalidated
    assert h.store.get_card(102) == card                                # the core card is unchanged
    again = h.ctx(night="2026-10-30")
    assert run(run_enrichment(again, [101, 102], dry_run=False)).done == [] and again.reasoner.llm.calls == []


def test_screens_reject_likeness_real_name_and_members(h, caplog):
    h.woman(102, "fond")
    h.woman(103, "drawn")
    famous = {**ENRICHED, "appearance": {**LOOKS, "at_the_pc": "looks like a famous singer in a big jumper"}}
    named = {**ENRICHED, "disclosures": [*LAYERS[:2], {"depth": "deep", "text": "Quillon is the only one who knows about the bad year."}]}
    member = {**ENRICHED, "drawn_to": ["Belra's laugh", "replies"]}
    crush = {**ENRICHED, "disclosures": [*LAYERS[:2], {"depth": "deep", "text": "Still has a soft spot from a crush on ebbo back in vanilla."}]}
    # a man's tastes are dropped, yet still screened; and the merged persona must validate
    muddled = {**ENRICHED, "drawn_to": ["as an AI i like replies"], "disclosures": [LAYERS[0], LAYERS[2], LAYERS[1]]}
    ctx = h.ctx(default=by_name({"Arkon": famous, "Belra": named, "Coros": member, "Dunna": crush, "Ebbo": muddled}))
    versions = h.store.persona_versions()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        report = run(run_enrichment(ctx, [101, 102, 103, 104, 105], dry_run=False))
    assert report.done == [] and report.refused == {
        101: ['appearance.at_the_pc: likeness: "looks like a famous singer in a big jumper"'],
        102: ['disclosures[2].text: real name: "[name] is the only one who knows about the bad year."'],
        103: ['romance.drawn_to[0]: names a member (Belra): "Belra\'s laugh"'],
        # "ebbo": check_romance reads members in any case (PR6); a layer's own name check reads
        # them as written, so it adds nothing here
        104: ["disclosures[2].text: romance involving another member is not allowed; romance may aim "
              "only at the guild master's character"],
        105: ["romance.drawn_to[0]: reads like an assistant, not a person",
              "disclosures: depths must run surface, then middle, then deep"]}
    assert h.store.persona_versions() == versions                       # no persona version changed
    assert "nightly_enrich_refused" in caplog.text and "[name]" in caplog.text and "Quillon" not in caplog.text
    assert no_real_name(h.store) == [] and not ctx.failures              # refused, not failed
    calls = len(ctx.reasoner.llm.calls)
    run(run_enrichment(ctx, [101, 102, 103, 104, 105], dry_run=False))
    assert len(ctx.reasoner.llm.calls) == calls                         # nothing is retried tonight
    # a layer is the player's life away from the guild, so a guildmate named there refuses it, with
    # no romance word needed (review I1); a refusal is asked again the next night
    fell = {**ENRICHED, "disclosures": [*LAYERS[:2], {"depth": "deep", "text": "Fell for Coros on the first raid night."}]}
    later = h.ctx(default=by_name({"Belra": fell}), night="2026-10-30")
    assert run(run_enrichment(later, [102], dry_run=False)).refused == {
        102: ['disclosures[2].text: names a member (Coros): "Fell for Coros on the first raid night."']}
    # a layer reads names as written, so an ordinary word that is also a member's name passes
    # there; the tastes still read names in any case
    belra = load_persona(h.store, 102)
    hope = {**ENRICHED, "disclosures": [*LAYERS[:2], {"depth": "deep", "text": "Lost hope the winter the shop closed."}]}
    assert screen_enrichment(belra, hope, ident=IDENT, member_names=["Hope"]) == []
    assert screen_enrichment(belra, {**hope, "drawn_to": ["someone who gives her hope"]}, ident=IDENT,
                             member_names=["Hope"]) == ['romance.drawn_to[0]: names a member (Hope): "someone who gives her hope"']


def test_a_failed_reply_refuses_the_unit_and_a_missing_model_fails_the_night(h):
    ctx = h.ctx({"enrich": [{"appearance": {}}, {"oops": 1}]})          # wrong twice
    report = run(run_enrichment(ctx, [101], dry_run=False))
    assert list(report.refused) == [101] and report.refused[101][0].startswith(
        "model: reflection request 'enrich' failed:")
    assert units(h, h.night) == ["101"] and not ctx.failures             # not asked again tonight

    async def gone(system, user):
        raise ReflectionError("the reflection model is not pulled", fatal=True)
    later = h.ctx(night="2026-10-30")
    later.reasoner.think = gone
    with pytest.raises(ReflectionError, match="not pulled"):
        run(run_enrichment(later, [101], dry_run=False))
    assert units(h, "2026-10-30") == []


def test_dry_run_writes_scratch_only(h):
    h.woman(102, "lust")
    flagged = {**ENRICHED, "turned_off_by": ["rudeness", "anyone who acts like a celebrity"]}
    ctx = h.ctx(default=by_name({"Arkon": ENRICHED, "Belra": flagged}))
    versions = h.store.persona_versions()
    report = run(run_enrichment(ctx, [101, 102], dry_run=True))
    path = h.settings.data_dir / "scratch" / "enrich-2026-10-29.json"
    assert report.path == path and report.done == [101] and list(report.refused) == [102]
    arkon, belra = json.loads(path.read_text(encoding="utf-8"))
    assert arkon == {"guid": 101, "name": "Arkon", "appearance": LOOKS, "drawn_to": [],      # a man's tastes stay empty
                     "turned_off_by": [], "disclosures": LAYERS, "problems": []}
    assert belra == {"guid": 102, "name": "Belra", "appearance": None, "drawn_to": [], "turned_off_by": [],
                     "disclosures": [], "problems": ['romance.turned_off_by[1]: likeness: "anyone who acts like a celebrity"']}
    assert h.store.persona_versions() == versions and h.count("grounding_log") == 0
    assert h.count("night_step") == 0 and h.store.get_meta("enrich_approved") is None
    assert [f.name for f in path.parent.iterdir()] == ["enrich-2026-10-29.json"]


def test_night_stage_waits_for_approval(h):
    h.persona(101, appearance=APPEARANCE)
    h.persona(103, appearance=APPEARANCE)
    ctx = h.ctx(default=lambda text: ENRICHED)
    ctx.settings = replace(ctx.settings, enrich_per_night=2)
    count = budget.COUNTERS["enrich"]                                    # registered at import
    assert count(ctx) == 0
    run(stage_enrich(ctx))
    assert ctx.reasoner.llm.calls == [] and units(h, h.night) == []     # enrich_approved absent: no call
    h.store.set_meta("enrich_approved", "1")
    assert count(ctx) == 2                                               # 102, 104 and 105 wait; two tonight
    run(stage_enrich(ctx))
    assert {g: load_persona(h.store, g).appearance is not None for g in TEST_CAST} == {
        101: True, 102: True, 103: True, 104: True, 105: False}          # the two lowest without, 105 waits
    assert units(h, h.night) == ["102", "104"]
    calls = len(ctx.reasoner.llm.calls)
    run(stage_enrich(ctx))                                               # the night resumed: its two are done
    assert len(ctx.reasoner.llm.calls) == calls
    tomorrow = h.ctx(default=lambda text: ENRICHED, night="2026-10-30")
    tomorrow.settings = replace(tomorrow.settings, enrich_per_night=2)
    assert count(tomorrow) == 1
    run(stage_enrich(tomorrow))
    assert load_persona(h.store, 105).appearance is not None and units(h, "2026-10-30") == ["105"]


def test_the_prompt_and_the_two_calls(h):
    h.woman(102, "fond", likes=["dawn fishing", "old maps"])
    ctx = h.ctx({"enrich": [ENRICHED, ENRICHED]})
    run(run_enrichment(ctx, [101, 102], dry_run=False))
    calls = ctx.reasoner.llm.calls
    for name, guid, tastes in (("Arkon", 101, TASTES_MAN), ("Belra", 102, TASTES_WOMAN.format(human="Testy"))):
        think, answer = [c for c in calls if f"who plays {name}," in c["messages"][0]["content"]]
        system = think["messages"][0]["content"]
        assert think["think"] is True and answer["think"] is False and answer["messages"][0]["content"] == system
        assert ctx.card(guid) in system and tastes in system and "Testy (character Testmage)" in system
        assert answer["options"]["temperature"] == 0.8 and answer["format"] == ENRICH_SCHEMA
        assert "Your notes:\n" in answer["messages"][-1]["content"]      # the reasoning pass's notes
    woman = next(c for c in calls if "who plays Belra," in c["messages"][0]["content"])["messages"][0]["content"]
    assert ("Age: 34\nRegion: UK\nGender: female\nJob: warehouse supervisor\nHousehold: lives with a flatmate\n"
            "Rhythm: early shifts, free evenings\nLife thread: learning to cook\nLikes: dawn fishing; old maps\n"
            "Dislikes: late starts") in woman and TASTES_MAN not in woman
    assert not any("Quillon" in m["content"] for c in calls for m in c["messages"])   # never his real name
    s = ENRICH_SCHEMA                                                    # the reply asked for: the persona's bounds
    assert s["title"] == "enrich" and s["required"] == ["appearance", "drawn_to", "turned_off_by", "disclosures"]
    looks = s["properties"]["appearance"]
    assert looks["properties"]["height_cm"] == {"type": "integer", "minimum": 140, "maximum": 210}
    assert looks["properties"]["build"] == {"enum": ["slim", "athletic", "average", "curvy", "stocky", "heavy"]}
    assert looks["properties"]["face"] == {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 4}
    assert looks["required"] == list(LOOKS) and looks["additionalProperties"] is False
    assert s["properties"]["turned_off_by"] == {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 6}
    layers = s["properties"]["disclosures"]
    assert (layers["minItems"], layers["maxItems"]) == (3, 4)
    assert layers["items"]["properties"] == {"depth": {"enum": ["surface", "middle", "deep"]},
                                             "text": {"type": "string", "maxLength": 160}}


def test_a_save_builds_on_the_stored_persona(h):
    """Controller ruling C1: an import made while the pass runs is never overwritten. The save
    re-reads the stored persona and fills only those of the pass's fields that are still empty."""
    h.woman(102, "fond")
    _, race, cls = TEST_CAST[102]
    gate = asyncio.Event()
    ctx = h.ctx({"enrich": [ENRICHED]}, gate=gate)
    hand_looks = replace(APPEARANCE, hair_colour="copper red", face=(), at_the_pc="a band t-shirt")

    async def owner_imports_mid_pass():
        task = asyncio.create_task(run_enrichment(ctx, [102], dry_run=False))
        for _ in range(100):
            if ctx.reasoner.llm.inflight:
                break
            await asyncio.sleep(0)
        assert ctx.reasoner.llm.inflight == 1                           # the pass is thinking
        stored = load_persona(h.store, 102)
        hand = replace(stored, appearance=hand_looks,
                       romance=replace(stored.romance, stance="lust", style="bold", drawn_to=("teasing back",),
                                       dial=7))
        save_persona(h.store, hand, source="owner", ts=h.T + 60, race=RACES[race], class_name=CLASSES[cls])
        gate.set()
        return hand, await task
    hand, report = run(owner_imports_mid_pass())
    p = load_persona(h.store, 102)
    assert report.done == [102] and h.store.persona_versions()[102] == 4         # seed, woman, his import, the pass
    assert p.appearance == hand_looks and p.romance.drawn_to == ("teasing back",)  # his, kept
    assert p.romance.dial == 7                                                    # her own dial (G4), kept
    assert p.romance.turned_off_by == ("being ignored",) and len(p.disclosures) == 3   # the pass's, where his were empty
    assert replace(p, disclosures=(), romance=replace(p.romance, turned_off_by=())) == hand   # nothing else moved
