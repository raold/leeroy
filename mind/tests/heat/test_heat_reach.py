"""What a set heat reaches beyond the romance block (review of #373, P2-4, P2-5, P3-5): the
older readers of a member's romance dial (learn's lesson screen and apply, the outreach fill's
"miss you", the bond's night, invitations and recall), a gallery copy's own tier at sending,
and the public guard only on turns aimed at him. With heat off, or on and unset, each reads
what it always read."""
from types import SimpleNamespace

import pytest

from conftest import HUMAN, make_persona
from heat.rig import CAPPED, ON
from heat.test_heat_photos import Tier
from leeroy_mind import heat as H
from leeroy_mind.bond.policy import eligible
from leeroy_mind.heat import live as heat_live
from leeroy_mind.heat import photos
from leeroy_mind.heat.table import P0, P2, P3, P4
from leeroy_mind.human import HumanIdentity
from leeroy_mind.learn import lessons
from leeroy_mind.outreach import store as items
from leeroy_mind.photos import request_stock
from leeroy_mind.selfies import kind
from leeroy_mind.selfies.prompt import VARIED_VIEW_WORDS, lust_phrase
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.tuning.state import Tuning
from nightly.conftest import h  # noqa: F401 (the nightly harness fixture)
from outreach.test_fill import fill
from photos.test_request_storage import app, claim  # noqa: F401 (the request stock's fixture)

IDENT = HumanIdentity(guid=HUMAN, character_name="Zan", nickname="Zan", real_name="",
                      is_guild_master=True)
FLIRT = dict(text="She flirts a little more openly.", scope="whisper", non_manipulative=True,
             forward_min=0)


def live_at(store, clock, world, config=ON, dial=9, pins=None):
    """A HeatLive over a store with the world heat at `world` (None: on and unset) and `pins`,
    member offsets {guid: offset}."""
    cfg = SimpleNamespace(romance=SimpleNamespace(dial=dial))
    tuning = Tuning(heat_world=world, heat_pins=dict(pins or {}))
    app = SimpleNamespace(personas=SimpleNamespace(tuning=tuning), store=store,
                          clock=clock, cfg=cfg, file_cfg=cfg, data_dir=None, engine=None)
    heat_live._LIVE = heat_live.HeatLive(app, config)
    return heat_live._LIVE


def lust(own=None):
    return make_persona(104, "Dunna", player_gender="female", stance="lust", dial=own)


# -- the older readers of her romance dial --------------------------------------------------------

def test_member_dial_is_the_legacy_value_until_heat_is_set(store, clock):
    p = lust()
    assert heat_live.member_dial(p, 7) == 7  # heat off
    live_at(store, clock, None)
    assert heat_live.member_dial(p, 7) == 7 and heat_live.member_dial(lust(10), 10) == 10
    # set: they follow her heat down from the dial they read (the derived one), never up
    live_at(store, clock, 9)
    assert heat_live.member_dial(p, 9) == 9 and heat_live.member_dial(p, 3) == 3
    assert heat_live.member_dial(lust(10), 9) == 9  # her own 10 at world 9: as before (2a)
    assert heat_live.member_dial(lust(10), 10) == 10  # a reader that took her own dial
    live_at(store, clock, 9, pins={104: -5})
    assert heat_live.member_dial(p, 9) == 4  # her offset lowers what they read
    live_at(store, clock, 9, pins={104: 1})
    assert heat_live.member_dial(p, 9) == 9  # and a raise stays with the romance block
    live_at(store, clock, 0)
    assert heat_live.member_dial(p, 9) == 0  # no romance wording at all: left out everywhere
    assert heat_live.member_dial(lust(10), 10) == 1  # her own dial: +1 on the world's 0
    assert heat_live.member_dial(make_persona(101, "Arkon"), 6) == 6  # a man: never reached
    live = live_at(store, clock, 9)
    live.sessions.set(104, "clean", medium="discord")
    assert heat_live.member_dial(p, 9) == 2


@pytest.mark.parametrize("world,kept", [("off", True), (None, True), (9, True), (4, True),
                                        (0, False)])
def test_a_flirt_lesson_is_screened_by_her_resolved_heat(store, clock, world, kept):
    if world != "off":
        live_at(store, clock, world)
    got = lessons.screen(dict(FLIRT), scope="whisper", persona=lust(), ident=IDENT,
                         world_romance=10)
    assert (got is not None) is kept
    plain = dict(FLIRT, text="She keeps replies short.")
    assert lessons.screen(plain, scope="whisper", persona=lust(), ident=IDENT,
                          world_romance=10) is not None  # only romance is left out


def test_the_bond_and_learn_apply_take_her_resolved_dial(store, clock):
    woman = make_persona(104, "Dunna", player_gender="female", stance="lust", age=30)
    live_at(store, clock, 0)
    assert not eligible(woman, heat_live.member_dial(woman, 10))
    live_at(store, clock, 6)
    assert eligible(woman, heat_live.member_dial(woman, 10))
    live_at(store, clock, None)
    assert eligible(woman, heat_live.member_dial(woman, 10))
    import inspect

    from leeroy_mind.bond import night
    from leeroy_mind.learn import apply
    from leeroy_mind.outreach import fill as fill_module
    from leeroy_mind.realm import bond as realm_bond
    from leeroy_mind.selfies import romance_styling
    from leeroy_mind.talk import pipeline
    for module in (night, apply, fill_module, realm_bond, lessons, pipeline,
                   romance_styling):  # one helper
        assert "member_dial(" in inspect.getsource(module), module.__name__


def test_learn_apply_reads_her_resolved_dial_once_heat_is_set(store, clock, tmp_path):
    """The bandit's romance (min of the world's dial and hers, which bounds a tease arm) is the
    old value off and unset, and her resolved heat's dial once a world value is set."""
    from engine_rig import make_rig, save_member
    from heat.rig import attach, set_values
    from leeroy_mind.learn import apply
    from leeroy_mind.learn.config import LearnCfg
    from leeroy_mind.talk.turns import Turn, TurnKind
    save_member(store, 104, player_gender="female", stance="lust")
    r = make_rig(store, clock, tmp_path)  # the romance dial at 9
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=104, channel="whisper", to_guid=HUMAN,
                addressee=HUMAN)

    def romance():
        return apply.context_for(r.engine, turn, r.engine.personas.get(104),
                                 LearnCfg(enabled=True)).romance
    assert romance() == 9  # heat off
    attach(r.engine, store, clock, tmp_path, ON, world=None)
    assert romance() == 9  # on and unset: as before
    for world, dial in ((9, 9), (4, 4), (0, 0)):
        set_values(store, r.engine, [("world", "", world)])
        assert romance() == dial, world


def _playful(h):  # noqa: F811
    """The bond night's 'playful' mood for 102 (tests/bond/test_night.py's evening)."""
    h.woman(102, "fond")
    h.rel(102, 1, affinity=30)
    h.session(h.T - 5 * 86400, h.T - 5 * 86400 + 3600)
    h.boss(h.T - 100, 631, 1, "killed", (1, 102))
    h.say(h.T - 90, 1, "nice evening", channel="whisper", to=102)
    h.say(h.T - 80, 102, "agreed", channel="whisper", to=1)
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM session WHERE started>?", (h.T - 86400,))


@pytest.mark.parametrize("world,pins,strength", [
    ("off", None, .5), (None, None, .5), (None, {102: -5}, .5),  # unset: offsets wait
    (4, None, .4), (9, None, .6), (9, {102: -5}, .4), (0, None, "absent")])
def test_the_bond_night_reads_her_resolved_dial_once_heat_is_set(h, world, pins,  # noqa: F811
                                                                 strength):
    """Eligibility and the playful mood's strength (min(.6, dial / 10)): the night's dial off
    and unset (5 here); once set the night reads the dial derived from the world (as
    nightly/wiring merges it), lowered by her heat, and at heat 0 no bond for her at all."""
    from dataclasses import replace as _replace

    from conftest import run
    from leeroy_mind.bond.night import stage_bond
    from leeroy_mind.heat.table import ROWS
    _playful(h)
    dial = 5 if world in ("off", None) else ROWS[world].dial
    if world != "off":
        live_at(h.store, h.clock, world, pins=pins)
    h.clock.set(h.T)
    h.settings = _replace(h.settings, romance_activities=True, romance_dial=dial)
    run(stage_bond(h.ctx(window=(h.T - 86400, h.T))))
    row = h.store.conn.execute("SELECT * FROM bond_mood WHERE guid=102").fetchone()
    if strength == "absent":
        assert row is None and h.count("bond_moment") == 0
        return
    assert row["mood"] == "playful" and row["strength"] == pytest.approx(strength)


def test_a_bond_invitation_waits_by_her_resolved_dial_once_heat_is_set(tmp_path, store, clock):
    """realm/bond: eligibility and the wait after his login ((10 - dial) x 30 s of it) read the
    world dial off and unset; once set, the dial derived from the world (as the app merges
    it), lowered by her heat, and at heat 0 no invitation at all."""
    from dataclasses import replace as _replace

    from bond.test_live import live as live_fixture
    from leeroy_mind.tuning.effective import dials_of
    h = live_fixture.__wrapped__(tmp_path, store, clock)

    def world_dial(dial):
        h.app.cfg = _replace(h.app.cfg, romance=_replace(h.app.cfg.romance, dial=dial))
    p = h.bond.ports.persona(101)
    forward = 600 + (10 - min(10, dials_of(p).level("forward"))) * 30
    wait = {dial: forward + (10 - dial) * 30 for dial in range(0, 11)}
    now = h.bond.now()
    world_dial(9)
    h.app.engine._login_at = now - wait[9]
    assert h.bond.can_offer(101)  # heat off
    live_at(store, clock, None, pins={101: -5})
    assert h.bond.can_offer(101)  # on and unset: as before, her offset waits
    live_at(store, clock, 9)
    assert h.bond.can_offer(101)  # world 9: the derived dial 9, as before
    live_at(store, clock, 9, pins={101: -5})
    assert not h.bond.can_offer(101)  # her heat 4: the wait of dial 4
    h.app.engine._login_at = now - wait[4]
    assert h.bond.can_offer(101)
    world_dial(1)
    live_at(store, clock, 0)
    assert not h.bond.can_offer(101)  # heat 0: no invitation at all
    assert not h.bond.available(101, body=False)


@pytest.mark.parametrize("world,made", [("off", True), (None, True), (9, True), (3, True),
                                        (0, False)])
def test_miss_you_is_not_made_at_a_heat_with_no_romance(h, world, made):  # noqa: F811
    if world != "off":
        live_at(h.store, h.clock, world)
    fill(h, p=h.woman(101, "lust", age=30), romance_dial=10)
    assert bool(items.open_items(h.store, 101)) is made


def test_miss_you_weight_is_todays_while_heat_is_unset(h):  # noqa: F811
    fill(h, p=h.woman(101, "lust", age=30), romance_dial=10)
    before, = items.open_items(h.store, 101)
    live_at(h.store, h.clock, None, dial=10)
    fill(h, guid=102, p=h.woman(102, "lust", age=30), romance_dial=10)
    after, = items.open_items(h.store, 102)
    assert after.weight == before.weight


# -- a gallery copy's own tier ---------------------------------------------------------------------

def _prompt(outfit, colour, setting, view=None):
    photo = ProfilePhoto("snapshot", "upper_body", setting,
                         kind.outfit_phrase(outfit, colour, {**kind.LUST_OUTFITS,
                                                             **kind.VARIED_OUTFITS}),
                         "a playful smile")
    words = "a woman, " + lust_phrase(photo) + ", a playful smile"
    return words + (", " + VARIED_VIEW_WORDS[view] if view else "")


def test_a_rendered_photos_tier_is_read_from_its_own_prompt():
    assert photos.prompt_tier(_prompt("satin midi dress", "navy", "her living room couch")) == P0
    assert photos.prompt_tier(_prompt("bodycon mini dress", "red", "a nightclub")) == P2
    assert photos.prompt_tier(_prompt("sports top and leggings", "teal", "the gym")) == P3
    assert photos.prompt_tier(_prompt("halter bikini", "red", "the beach at sunset")) == P4
    # the pose it was rendered in counts: a covered dress from behind is P3, not P0
    assert photos.prompt_tier(_prompt("satin midi dress", "navy", "a cafe terrace",
                                      "behind")) == P3
    assert photos.prompt_tier(_prompt("satin midi dress", "navy", "a cafe terrace",
                                      "rear_three_quarter")) == P2
    assert photos.prompt_tier(_prompt("satin midi dress", "navy", "a cafe terrace",
                                      "front")) == P0
    # anything it cannot read is P4: sent only where everything is
    for unreadable in (None, "", 7, "a woman at a desk", "wearing something, somewhere"):
        assert photos.prompt_tier(unreadable) == P4
    for choice in {**kind.LUST_OUTFITS, **kind.VARIED_OUTFITS}:  # never under its outfit's tier
        assert photos.prompt_tier(_prompt(choice, "black", "her living room couch")) \
            >= photos.OUTFIT_TIER[choice]


def test_a_copys_tier_receipt_is_what_sending_checks(store):
    p = lust()
    assert heat_live.stored_tier(store, 41) is None
    heat_live.remember_tier(store, 41, P3)
    assert heat_live.stored_tier(store, 41) == P3
    assert heat_live.sends_tier(p, P3)  # heat off: everything
    heat_live._LIVE = Tier(P4)
    assert heat_live.sends_tier(p, P3) and heat_live.sends_tier(p, P4)
    heat_live._LIVE = Tier(P0)  # heat lowered after the copy was reserved: passed over
    assert not heat_live.sends_tier(p, P3) and heat_live.sends_tier(p, P0)
    assert not heat_live.sends_tier(p, None) and not heat_live.sends_tier(p, 99)  # fail closed
    heat_live._LIVE = Tier(P3)  # raised again: eligible again, nothing was expired
    assert heat_live.sends_tier(p, P3)


def test_a_reserved_gallery_copy_is_judged_at_sending_by_its_own_tier(app):  # noqa: F811
    """A copy reserved while heat was higher is passed over once her tier is below the photo's
    own (its receipt, not her kind of the day), and eligible again when raised: nothing is
    expired. A plain set's copy is P0."""
    req, _ = claim(app)
    photo = request_stock.reserve_fallback(app, req)
    assert heat_live.stored_tier(app.store, photo.id) == P0 and request_stock.valid(app, req, photo)
    heat_live.remember_tier(app.store, photo.id, P3)  # as a back view copied at P3 would be
    assert request_stock.valid(app, req, photo)  # heat off: everything
    heat_live._LIVE = Tier(P0)
    assert not request_stock.valid(app, req, photo)
    heat_live._LIVE = Tier(P3)
    assert request_stock.valid(app, req, photo)
    assert app.store.conn.execute("SELECT status FROM selfie_snap WHERE id=?",
                                  (photo.id,)).fetchone()[0] == "ready"


def test_the_request_stock_judges_the_photo_not_her_kind_of_the_day():
    """By construction: a gallery candidate's tier comes from the prompt it was rendered with
    and its varied look, the copy keeps it as a receipt, and the check at sending reads the
    receipt before the copy's scene."""
    import inspect

    from leeroy_mind.photos import request_stock
    cand = inspect.getsource(request_stock._candidates)
    assert "prompt_tier(row['prompt'])" in cand and "look_tier(look)" in cand
    assert "heat_live.sends(p, scene" not in cand  # never her current kind's scene
    assert "remember_tier(" in inspect.getsource(request_stock.reserve_fallback)
    safe = inspect.getsource(request_stock._safe_snap)
    assert safe.index("stored_tier(") < safe.index("heat_live.sends(")
    for fn in (request_stock._candidates, request_stock._safe_snap,
               request_stock.reserve_fallback):
        assert "expire(" not in inspect.getsource(fn).split("heat_live")[-1] or fn is \
            request_stock.reserve_fallback


# -- the public guard only on turns aimed at him -------------------------------------------------

def test_the_public_guard_leaves_a_line_to_another_member_alone(store, clock, tmp_path):
    from engine_rig import make_rig, save_member
    from heat.rig import attach
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn, TurnKind
    save_member(store, 104, player_gender="female", stance="lust")
    r = make_rig(store, clock, tmp_path)
    live = attach(r.engine, store, clock, tmp_path, CAPPED)
    hr = live.resolve(r.engine.personas.get(104), channel="guild")
    to_him = Turn(kind=TurnKind.REPLY_HUMAN, speaker=104, channel="guild", addressee=HUMAN)
    to_member = Turn(kind=TurnKind.REPLY_MEMBER if hasattr(TurnKind, "REPLY_MEMBER")
                     else TurnKind.OPEN, speaker=104, channel="guild", addressee=102)
    opener = Turn(kind=TurnKind.OPEN, speaker=104, channel="guild")
    assert r.engine._heat_public(to_him, TurnMeta("t1", heat=hr), "so horny")
    assert not r.engine._heat_public(to_member, TurnMeta("t2", heat=hr), "so horny")
    assert not r.engine._heat_public(opener, TurnMeta("t3", heat=hr), "so horny")
    assert H.resolve(9, H.Member(stance="lust"), private=False, public_cap=True).tier == "flirty"


def test_private_photo_styling_follows_her_heat_down_once_set(store, clock):
    """Main's [selfies] romance_styling (#364) reads her romance dial for how bold a styled
    private photo is: the same helper, so unset it is main's, a set heat lowers it, and at heat
    0 nothing is styled. Its clothed catalogue is not tiered; the tone is what heat moves."""
    from leeroy_mind.selfies.romance_styling import level
    from leeroy_mind.storage.rows import Relationship
    p = make_persona(104, "Dunna", player_gender="female", stance="lust", age=30)
    rel = Relationship(a=104, b=HUMAN, romance_stage="late_nights")
    off = level(p, rel, 9)
    assert off == "confident"
    live_at(store, clock, None)
    assert level(p, rel, 9) == off  # on and unset: as main
    live_at(store, clock, 9)
    assert level(p, rel, 9) == off  # world 9: the dial she read
    live_at(store, clock, 2)
    assert level(p, rel, 9) == "warm"  # heat 2: the warm wording, the warm tone
    live_at(store, clock, 0)
    assert level(p, rel, 9) is None  # heat 0: no romance, so nothing is styled
    assert level(make_persona(101, "Arkon"), rel, 9) is None  # a man: as main
