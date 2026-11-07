"""The lust style of the profile photos (owner, 2026-11-07): a woman whose romance stance toward
the owner is lust gets a flattering, flirtatious photo kind, still SFW, chosen with its own
template and screen, and a kind chosen under the other rules is chosen again once when her
stance moves. Everyone else keeps the plain rules exactly. Fakes only; every person here is
invented."""
import logging
from dataclasses import asdict, replace
from string import Formatter

import pytest

from conftest import TEST_CAST, run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.enrich import _facts
from leeroy_mind.nightly.llm import system_prompt
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.selfies import kind
from leeroy_mind.selfies.kind import (KIND_PROMPT, KIND_PROMPT_LUST, LUST_PROMPT, STAGE,
                                      chosen_style, looks_line, needs_rechoice, screen_kind,
                                      stage_photo)
from leeroy_mind.selfies.style import LUST, PLAIN, SUGGESTIVE_MIN_AGE, style_of, suggestive
from leeroy_mind.wow import CLASSES, RACES
from nightly.conftest import IDENT
from nightly.test_night_enrich import APPEARANCE

# today's strings, copied: the plain rules must stay exactly these
TODAY_KIND_PROMPT = (
    "Choose the profile picture the real person who plays {name} would put on their own social "
    "or work profile.\n{card}\n{facts}\nHow they look: {looks}\n"
    "Choose what fits who they are, their work, age, region, habits and personality: the kind "
    "of photo (a phone selfie, a mirror selfie, a professional headshot, or a snapshot a friend "
    "took), the framing (face, head and shoulders, or upper body), where it was taken (a place "
    "in their own life, in a few words), what they are wearing in it, and their expression. "
    "Name no year, no decade, no device and no brand. Fully clothed, nothing revealing or "
    "sexual, never like anyone famous, and no one else's name.")
TODAY_EXPLICIT = (
    r"\b(nude|naked|topless|shirtless|braless|lingerie|underwear|bra|panties|bikini|swimsuit|"
    r"swimwear|see-through|sheer|sexy|sexual|seductive|nsfw|cleavage)\b")

PLAIN_KIND = {"kind": "snapshot", "framing": "upper_body", "setting": "on the deck of a small boat",
              "outfit": "orange rain bib over a grey hoodie",
              "expression": "squinting into the wind"}
LUST_KIND = {"kind": "mirror_selfie", "framing": "upper_body",
             "setting": "the hallway mirror at home", "outfit": "a fitted low-cut black top",
             "expression": "a playful half-smile"}
HAND = ProfilePhoto("headshot", "face", "an office", "a blazer", "a calm smile")
NIGHTS = ("2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01")


def lust(h, guid=102, **over):
    return h.woman(guid, "lust", appearance=APPEARANCE, **over)


def systems(ctx) -> list[str]:
    """The system prompts the photo pass sends, in order."""
    seen, real = [], ctx.reasoner.json

    async def json_(system, user, schema, **kw):
        seen.append(system)
        return await real(system, user, schema, **kw)
    ctx.reasoner.json = json_
    return seen


def expected(ctx, guid, name, template) -> str:
    """The photo pass's system prompt for `guid` now, from prompt `name` with `template` as its
    built-in default."""
    p = ctx.base(guid)
    return system_prompt(name, template, ctx.settings.data_dir, human=IDENT, name=ctx.name(guid),
                         card=ctx.base_card(guid), facts=_facts(p), looks=looks_line(p.appearance))


def _save(h, guid, p, source):
    _, race, cls = TEST_CAST[guid]
    save_persona(h.store, p, source=source, ts=h.T, race=RACES[race], class_name=CLASSES[cls])


def restance(h, guid, stance, *, source="generated"):
    """Her stance moved (the night's stance move, or the owner's rebalance)."""
    p = load_persona(h.store, guid)
    _save(h, guid, replace(p, romance=replace(p.romance, stance=stance)), source)


def by_hand(h, guid, photo: ProfilePhoto):
    _save(h, guid, replace(load_persona(h.store, guid), photo=photo), "owner")


def night(h, n, replies=()):
    ctx = h.ctx({"photo": list(replies)}, night=NIGHTS[n])
    run(stage_photo(ctx))
    return ctx


def photo(h, guid):
    return load_persona(h.store, guid).photo


def style(h, guid):
    return chosen_style(h.store, load_persona(h.store, guid))


# -- who ----------------------------------------------------------------------------------------

def test_the_suggestive_style_is_a_lust_woman_of_21_or_more(h):
    assert SUGGESTIVE_MIN_AGE == 21
    assert suggestive(lust(h)) and style_of(lust(h)) == LUST
    assert suggestive(lust(h, age=21))
    assert not suggestive(lust(h, age=20)) and style_of(lust(h, age=20)) == PLAIN
    assert not suggestive(h.woman(103, "drawn", appearance=APPEARANCE))
    assert not suggestive(h.woman(104, "fond")) and not suggestive(h.woman(105, "none"))
    assert not suggestive(h.persona(101, appearance=APPEARANCE))
    assert not suggestive(None) and style_of(None) == PLAIN


# -- the template ---------------------------------------------------------------------------

def test_a_lust_woman_gets_the_lust_template_and_everyone_else_todays(h):
    assert KIND_PROMPT == TODAY_KIND_PROMPT and LUST_PROMPT == "photo_lust"
    h.persona(101, appearance=APPEARANCE)
    lust(h, 102)
    h.woman(103, "fond", appearance=APPEARANCE)
    lust(h, 104, age=20)
    ctx = h.ctx({"photo": [PLAIN_KIND, LUST_KIND, PLAIN_KIND, PLAIN_KIND]})
    want = {102: expected(ctx, 102, LUST_PROMPT, KIND_PROMPT_LUST),
            **{g: expected(ctx, g, STAGE, TODAY_KIND_PROMPT) for g in (101, 103, 104)}}
    seen = systems(ctx)
    run(stage_photo(ctx))
    assert sorted(seen) == sorted(want.values())
    assert want[102] != want[103]


def test_the_lust_template_takes_the_same_fields_and_stays_within_the_line():
    def fields(t):
        return {f for _, f, _, _ in Formatter().parse(t) if f}
    assert fields(KIND_PROMPT_LUST) == fields(KIND_PROMPT) == {"name", "card", "facts", "looks"}
    text = KIND_PROMPT_LUST.lower()
    for rule in ("never nude, topless, see-through or sheer", "never lingerie or underwear",
                 "nothing sexual", "swimwear only at a beach, a pool, a lake or a hot tub",
                 "never like anyone famous", "no one else's name"):
        assert rule in text


def test_the_overlay_nightly_photo_lust_replaces_the_lust_template_only(h):
    prompts = h.settings.data_dir / "prompts"
    prompts.mkdir()
    (prompts / "nightly_photo_lust.txt").write_text("OVERLAYLUST {name}: {looks}\n",
                                                    encoding="utf-8")
    lust(h, 102)
    h.woman(103, "fond", appearance=APPEARANCE)
    ctx = h.ctx({"photo": [LUST_KIND, PLAIN_KIND]})
    mine = expected(ctx, 102, LUST_PROMPT, "unused")
    other = expected(ctx, 103, STAGE, TODAY_KIND_PROMPT)
    assert mine.startswith("OVERLAYLUST Belra: ") and KIND_PROMPT_LUST[:40] not in mine
    seen = systems(ctx)
    run(stage_photo(ctx))
    assert sorted(seen) == sorted([mine, other])


# -- the screen ------------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["nude", "naked", "topless", "shirtless", "braless", "lingerie",
                                  "underwear", "bra", "panties", "see-through", "sheer", "nsfw",
                                  "sexual"])
def test_the_lust_screen_still_refuses_what_is_never_allowed(h, word):
    out = screen_kind(lust(h), {**LUST_KIND, "outfit": f"a {word} top"}, ident=IDENT,
                      member_names=["Testb"])
    assert any(m.startswith("photo.outfit: explicit") for m in out), word


@pytest.mark.parametrize("field,text", [
    ("outfit", "a fitted low-cut top that shows some cleavage"),
    ("outfit", "a sexy little black dress"), ("outfit", "an off-the-shoulder flirty sundress"),
    ("expression", "a sultry half-smile"), ("expression", "a flirty wink")])
def test_the_lust_screen_allows_a_flattering_flirtatious_photo(h, field, text):
    assert screen_kind(lust(h), {**LUST_KIND, field: text}, ident=IDENT,
                       member_names=["Testb"]) == []


@pytest.mark.parametrize("setting,ok", [
    ("a small office", False), ("her kitchen", False), ("a pool hall downtown", False),
    ("on the beach at sunset", True), ("by the hotel pool", True), ("poolside", True),
    ("on a dock at the lake", True), ("in a hot tub at a ski lodge", True)])
def test_swimwear_only_where_it_belongs(h, setting, ok):
    p = lust(h)
    for outfit in ("a red bikini", "a black swimsuit"):
        out = screen_kind(p, {**LUST_KIND, "setting": setting, "outfit": outfit}, ident=IDENT,
                          member_names=["Testb"])
        if ok:
            assert out == [], (setting, outfit)
        else:
            assert any(m.startswith("photo.outfit: swimwear") for m in out), (setting, outfit)


def test_the_lust_screen_keeps_the_other_checks(h):
    p = lust(h)
    for field, text, problem in [("setting", "a 2009 house party", "era"),
                                 ("setting", "a pier with testb", "names a member"),
                                 ("expression", "looks like a famous actress", "likeness")]:
        out = screen_kind(p, {**LUST_KIND, field: text}, ident=IDENT, member_names=["Testb"])
        assert any(m.startswith(f"photo.{field}: {problem}") for m in out), text


@pytest.mark.parametrize("who", ["fond", "drawn", "young", "man"])
@pytest.mark.parametrize("text", ["a low-cut top that shows some cleavage", "a sexy black dress",
                                  "a red bikini", "a sheer blouse"])
def test_everyone_else_keeps_todays_screen(h, who, text):
    assert kind.EXPLICIT_RE.pattern == TODAY_EXPLICIT
    p = {"fond": lambda: h.woman(103, "fond", appearance=APPEARANCE),
         "drawn": lambda: h.woman(103, "drawn", appearance=APPEARANCE),
         "young": lambda: lust(h, 103, age=20),
         "man": lambda: h.persona(101, appearance=APPEARANCE)}[who]()
    data = {**LUST_KIND, "setting": "on the beach at sunset", "outfit": text}
    out = screen_kind(p, data, ident=IDENT, member_names=["Testb"])
    assert any(m.startswith("photo.outfit: explicit") for m in out)


# -- over time: the re-choice --------------------------------------------------------------

def test_a_woman_who_became_lust_gets_one_new_kind_under_the_lust_rules(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    night(h, 0, [PLAIN_KIND])
    assert photo(h, 102) == ProfilePhoto(**PLAIN_KIND) and style(h, 102) == PLAIN
    restance(h, 102, "lust")
    assert needs_rechoice(h.store, load_persona(h.store, 102))
    ctx = h.ctx({"photo": [LUST_KIND]}, night=NIGHTS[1])
    want = expected(ctx, 102, LUST_PROMPT, KIND_PROMPT_LUST)
    seen = systems(ctx)
    run(stage_photo(ctx))
    assert seen == [want]
    assert photo(h, 102) == ProfilePhoto(**LUST_KIND) and style(h, 102) == LUST
    assert not needs_rechoice(h.store, load_persona(h.store, 102))
    assert night(h, 2, [LUST_KIND]).reasoner.calls == 0                  # never twice
    assert photo(h, 102) == ProfilePhoto(**LUST_KIND)


def test_the_owners_rebalance_into_lust_counts_too(h):
    h.woman(102, "drawn", appearance=APPEARANCE)
    night(h, 0, [PLAIN_KIND])
    restance(h, 102, "lust", source="owner")  # rebalance-stances saves as the owner
    assert night(h, 1, [LUST_KIND]).reasoner.calls == 1
    assert photo(h, 102) == ProfilePhoto(**LUST_KIND)


def test_a_refused_rechoice_keeps_her_kind_and_is_asked_again_the_next_night(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    night(h, 0, [PLAIN_KIND])
    restance(h, 102, "lust")
    bad = {**LUST_KIND, "outfit": "lace lingerie"}
    first = night(h, 1, [bad, bad])
    run(stage_photo(first))
    assert first.reasoner.calls == 1 and photo(h, 102) == ProfilePhoto(**PLAIN_KIND)
    assert style(h, 102) == PLAIN
    assert night(h, 2, [LUST_KIND]).reasoner.calls == 1
    assert photo(h, 102) == ProfilePhoto(**LUST_KIND)


def test_a_kind_set_by_hand_is_never_chosen_again(h):
    lust(h, 102)
    by_hand(h, 102, HAND)
    assert style(h, 102) is None
    for n in range(2):
        assert night(h, n, [LUST_KIND]).reasoner.calls == 0
    assert photo(h, 102) == HAND


def test_a_pass_kind_the_owner_changed_by_hand_is_never_chosen_again(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    night(h, 0, [PLAIN_KIND])
    by_hand(h, 102, HAND)
    restance(h, 102, "lust")
    assert style(h, 102) is None
    assert night(h, 1, [LUST_KIND]).reasoner.calls == 0
    assert photo(h, 102) == HAND


def test_a_kind_set_by_hand_while_the_rechoice_ran_is_kept(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    night(h, 0, [PLAIN_KIND])
    restance(h, 102, "lust")

    def meanwhile(text):
        by_hand(h, 102, HAND)
        return LUST_KIND
    ctx = h.ctx(default=meanwhile, night=NIGHTS[1])
    run(stage_photo(ctx))
    assert ctx.reasoner.calls == 1 and photo(h, 102) == HAND and style(h, 102) is None
    assert night(h, 2, [LUST_KIND]).reasoner.calls == 0


def _legacy(h, guid, *, ran: bool):
    """A kind from before the style records: saved with the persona and, when `ran`, the photo
    pass's unit for her on an earlier night, as the pass left them."""
    _save(h, guid, replace(load_persona(h.store, guid), photo=ProfilePhoto(**PLAIN_KIND)),
          "generated")
    if ran:
        with h.store.transaction():
            h.store.conn.execute("INSERT INTO night_step(night, stage, unit, finished) "
                                 "VALUES ('2026-10-20', ?, ?, ?)", (STAGE, str(guid), h.T))


def test_a_kind_from_before_the_records_is_the_pass_plain_one_when_the_pass_handled_her(h):
    lust(h, 102)
    _legacy(h, 102, ran=True)
    lust(h, 103)
    _legacy(h, 103, ran=False)                               # never the pass's: by hand
    assert style(h, 102) == PLAIN and style(h, 103) is None
    assert night(h, 0, [LUST_KIND, LUST_KIND]).reasoner.calls == 1
    assert photo(h, 102) == ProfilePhoto(**LUST_KIND) and style(h, 102) == LUST
    assert photo(h, 103) == ProfilePhoto(**PLAIN_KIND) and style(h, 103) is None
    assert night(h, 1, [LUST_KIND]).reasoner.calls == 0


def test_the_first_night_records_the_old_kinds_so_a_later_hand_change_is_kept(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    _legacy(h, 102, ran=True)
    h.persona(101, appearance=APPEARANCE)
    _legacy(h, 101, ran=True)
    assert night(h, 0).reasoner.calls == 0                  # nothing due: only the records
    assert style(h, 102) == PLAIN and style(h, 101) == PLAIN
    by_hand(h, 102, HAND)
    restance(h, 102, "lust")
    assert style(h, 102) is None
    assert night(h, 1, [LUST_KIND]).reasoner.calls == 0
    assert photo(h, 102) == HAND


def test_a_woman_who_left_lust_gets_one_kind_under_the_plain_rules_again(h):
    lust(h, 102)
    night(h, 0, [LUST_KIND])
    assert style(h, 102) == LUST
    restance(h, 102, "drawn")
    ctx = h.ctx({"photo": [PLAIN_KIND]}, night=NIGHTS[1])
    want = expected(ctx, 102, STAGE, TODAY_KIND_PROMPT)
    seen = systems(ctx)
    run(stage_photo(ctx))
    assert seen == [want]
    assert photo(h, 102) == ProfilePhoto(**PLAIN_KIND) and style(h, 102) == PLAIN
    assert night(h, 2, [PLAIN_KIND]).reasoner.calls == 0


def test_men_and_women_who_stay_out_of_lust_are_never_asked_again(h):
    h.persona(101, appearance=APPEARANCE)
    h.woman(102, "fond", appearance=APPEARANCE)
    h.woman(103, "drawn", appearance=APPEARANCE)
    lust(h, 104, age=20)
    assert night(h, 0, [PLAIN_KIND] * 4).reasoner.calls == 4
    for n in (1, 2):
        assert night(h, n, [PLAIN_KIND] * 4).reasoner.calls == 0
    for g in (101, 102, 103, 104):
        assert photo(h, g) == ProfilePhoto(**PLAIN_KIND) and style(h, g) == PLAIN


def test_the_record_holds_the_style_and_the_kind_as_saved_and_no_name(h):
    lust(h, 102)
    night(h, 0, [LUST_KIND])
    raw = h.store.get_meta("selfies:photo-style:102")
    assert raw is not None and '"lust"' in raw
    assert "Belra" not in raw and IDENT.real_name not in raw
    assert asdict(photo(h, 102)) == LUST_KIND


def test_the_log_names_a_rechoice_that_landed_by_id_only(h, caplog):
    h.woman(102, "fond", appearance=APPEARANCE)
    h.woman(103, "fond", appearance=APPEARANCE)
    night(h, 0, [PLAIN_KIND, PLAIN_KIND])
    restance(h, 102, "lust")
    restance(h, 103, "lust")

    def reply(text):
        if "Coros" in text:
            by_hand(h, 103, HAND)                    # his hand wins: no re-choice landed
        return LUST_KIND
    with caplog.at_level(logging.DEBUG):
        run(stage_photo(h.ctx(default=reply, night=NIGHTS[1])))
    assert "selfies_kind_rechosen: night=2026-10-30 guid=102 style=lust" in caplog.text
    assert "guid=103 style" not in caplog.text and "rechosen=1" in caplog.text
    for s in (*LUST_KIND.values(), "Belra", "Coros"):
        assert s not in caplog.text


def test_the_counter_counts_the_rechoices(h):
    h.woman(102, "fond", appearance=APPEARANCE)
    h.woman(103, "fond", appearance=APPEARANCE)
    night(h, 0, [PLAIN_KIND, PLAIN_KIND])
    restance(h, 102, "lust")
    others = frozenset({"coach", "realm"})
    assert budget._counts(h.ctx(night=NIGHTS[1]), set(), others | {"photo"})["photo"] == 1


# -- at render time --------------------------------------------------------------------------

def test_the_render_reads_her_stance_when_it_renders(h, tmp_path):
    from leeroy_mind.selfies.prompt import LUST_NEGATIVE, NEGATIVE
    from leeroy_mind.selfies.stage import make_stage
    from leeroy_mind.selfies.store import ensure_first_sets
    from leeroy_mind.selfies.worker import SelfieWorker
    from selfies.fakes import SELFIE_CFG, fake_command

    for g in (102, 103):
        h.woman(g, "fond", appearance=APPEARANCE)
        by_hand(h, g, ProfilePhoto(**PLAIN_KIND))
    ensure_first_sets(h.store, [102, 103], now=h.T)
    restance(h, 102, "lust")                             # after her set was queued
    seen: dict[str, set] = {}
    cmd, env = fake_command(tmp_path)

    class Seen(SelfieWorker):
        async def render(self, req, *, rid=None):
            seen.setdefault(rid.split(":")[0], set()).add(req.negative)
            return await super().render(req, rid=rid)
    run(make_stage(SELFIE_CFG, worker=lambda: Seen(cmd, env=env))(h.ctx()))
    assert seen == {"102": {LUST_NEGATIVE}, "103": {NEGATIVE}}
