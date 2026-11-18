"""resolve() over its whole input space: raising anything that means "more" never lowers a
field, and the bounds hold everywhere. (Review of #373, P3-1: a positive group offset on a woman
at her own dial 10 used to turn "every turn" into "every private turn".)

Each point is compared with its neighbour one step up on every ordered axis: the world value,
a pinned offset, the groups' sum, her own dial, his lead, the session (clean <= none <= open),
the public cap (on <= off), the channel (public <= private) and the hour (not late <= late);
and in the derived mode (heat unset, a session override reaching her on a world value derived
from the romance dial) that dial too."""
import itertools
from dataclasses import replace

from leeroy_mind import heat as H
from leeroy_mind.heat.table import DIAL_TO_HEAT, P0, P4, TIERS, tier_index

EVERY = {"": 0, "private": 1, "all": 2}
SESSION_UP = {"clean": None, None: "open"}
WORLDS = range(11)
STANCES = ("fond", "drawn", "lust")
OWN = (None, 1, 4, 8, 9, 10)
OFFSETS = (None, -5, -1, 0, 1, 5)
GROUPS = (-6, -1, 0, 1, 6)
SESSIONS = ("clean", None, "open")
LEADS = (0, 1, 2)


def rank(r) -> tuple:
    """Every field that means "more" as a number, so one comparison covers them all."""
    return (r.heat, tier_index(r.tier), tier_index(r.ceiling), r.dial, r.flirt, int(r.reassure),
            r.starts, EVERY[r.every_turn], tier_index(r.voice), int(r.speaks_explicit), r.photo,
            r.offers)


NAMES = ("heat", "tier", "ceiling", "dial", "flirt", "reassure", "starts", "every_turn", "voice",
         "speaks_explicit", "photo", "offers")


def _never_lower(a, b, what):
    ra, rb = rank(a), rank(b)
    for name, x, y in zip(NAMES, ra, rb):
        assert x <= y, (what, name, x, y)


def test_raising_any_input_never_lowers_any_field():
    points = 0
    for world, stance, own, offset, groups in itertools.product(WORLDS, STANCES, OWN, OFFSETS,
                                                                GROUPS):
        member = H.Member(stance=stance, own_dial=own, offset=offset, groups=groups,
                          lust_style=stance == "lust")
        for session, private, cap, late, led in itertools.product(
                SESSIONS, (True, False), (False, True), (False, True), LEADS):
            kw = dict(private=private, session=session, public_cap=cap, late=late, his_heat=led)
            r = H.resolve(world, member, **kw)
            points += 1
            here = (world, stance, own, offset, groups, session, private, cap, late, led)
            if world < 10:
                _never_lower(r, H.resolve(world + 1, member, **kw), ("world", here))
            if offset is not None and offset < 5:
                _never_lower(r, H.resolve(world, replace(member, offset=offset + 1), **kw),
                             ("offset", here))
            _never_lower(r, H.resolve(world, replace(member, groups=groups + 1), **kw),
                         ("groups", here))
            if own is not None and own < 10 and offset is None:
                _never_lower(r, H.resolve(world, replace(member, own_dial=own + 1), **kw),
                             ("own dial", here))
            _never_lower(r, H.resolve(world, member, **{**kw, "his_heat": led + 1}),
                         ("his lead", here))
            if session in SESSION_UP:
                _never_lower(r, H.resolve(world, member, **{**kw, "session": SESSION_UP[session]}),
                             ("session", here))
            if cap:
                _never_lower(r, H.resolve(world, member, **{**kw, "public_cap": False}),
                             ("cap off", here))
            if not private:
                _never_lower(r, H.resolve(world, member, **{**kw, "private": True}),
                             ("private", here))
            if not late:
                _never_lower(r, H.resolve(world, member, **{**kw, "late": True}), ("late", here))
            # the bounds, everywhere
            assert 0 <= r.heat <= 10 and 1 <= r.dial <= 10 and P0 <= r.photo <= P4
            assert r.tier in TIERS and tier_index(r.tier) <= tier_index(r.ceiling)
            if cap and not private:
                assert tier_index(r.tier) <= tier_index("flirty")
            if stance != "lust":
                assert r.photo == P0  # no lust photo style: the tiers do not apply
            assert r.applies and r == H.resolve(world, member, **kw)  # pure
    assert points == 11 * 3 * 6 * 6 * 5 * 3 * 2 * 2 * 2 * 3


def test_the_age_rules_and_the_style_bound_the_photo_tier_everywhere():
    for world, own, offset, groups, session in itertools.product(WORLDS, OWN, OFFSETS, GROUPS,
                                                                 SESSIONS):
        for reason in ("age_guard", "plain_style"):
            member = H.Member(stance="lust", own_dial=own, offset=offset, groups=groups,
                              lust_style=False, photo_reason=reason)
            r = H.resolve(world, member, session=session)
            assert r.photo == P0 and r.reason("photo") == reason


def test_heat_never_applies_to_a_man_or_a_woman_with_no_attitude():
    for world, own, offset, groups, session, female, stance in itertools.product(
            WORLDS, OWN, OFFSETS, GROUPS, SESSIONS, (True, False), ("none", "lust")):
        if female and stance == "lust":
            continue
        r = H.resolve(world, H.Member(female=female, stance=stance, own_dial=own, offset=offset,
                                      groups=groups, lust_style=True), session=session)
        assert (r.applies, r.tier, r.ceiling, r.flirt, r.starts, r.offers, r.every_turn, r.photo,
                r.session) == (False, "none", "none", 1.0, 1.0, 1.0, "", P0, None)


def test_the_rates_never_read_her_own_dial():
    """Ruling 2a over the space: with no pinned offset, how often she starts it and how often
    a photo is offered are the same whatever dial she has of her own."""
    for world, groups, session in itertools.product(WORLDS, GROUPS, SESSIONS):
        plain = H.resolve(world, H.Member(stance="lust", groups=groups), session=session)
        for own in range(1, 11):
            r = H.resolve(world, H.Member(stance="lust", own_dial=own, groups=groups),
                          session=session)
            assert (r.starts, r.offers) == (plain.starts, plain.offers), (world, groups, own)


def test_the_derived_mode_is_monotonic_and_gives_the_older_readers_their_very_dial():
    """While no world heat is set only his session override reaches a member, on the world value
    the romance dial stands for (`legacy`). Raising that dial, an offset or a group, or opening
    the session, never lowers a field; with nothing else moving her the older readers get that
    very dial back, and the dial is the only field `legacy` changes."""
    points = 0
    for legacy, own, offset, groups, session, private in itertools.product(
            range(1, 11), OWN, OFFSETS, GROUPS, SESSIONS, (True, False)):
        member = H.Member(stance="lust", own_dial=own, offset=offset, groups=groups,
                          lust_style=True)
        kw = dict(private=private, session=session, legacy=legacy)
        r = H.resolve(DIAL_TO_HEAT[legacy], member, **kw)
        points += 1
        here = (legacy, own, offset, groups, session, private)
        if legacy < 10:
            _never_lower(r, H.resolve(DIAL_TO_HEAT[legacy + 1], member,
                                      **{**kw, "legacy": legacy + 1}), ("legacy", here))
        if offset is not None and offset < 5:
            _never_lower(r, H.resolve(DIAL_TO_HEAT[legacy], replace(member, offset=offset + 1),
                                      **kw), ("offset", here))
        _never_lower(r, H.resolve(DIAL_TO_HEAT[legacy], replace(member, groups=groups + 1), **kw),
                     ("groups", here))
        if session in SESSION_UP:
            _never_lower(r, H.resolve(DIAL_TO_HEAT[legacy], member,
                                      **{**kw, "session": SESSION_UP[session]}), ("session", here))
        assert 1 <= r.dial <= 10 and 0 <= r.heat <= 10
        bare = H.resolve(DIAL_TO_HEAT[legacy], member, private=private, session=session)
        assert replace(r, dial=bare.dial) == bare  # legacy reaches the dial and nothing else
        if own is None and not offset and not groups and session is None:
            assert r.dial == legacy
    assert points == 10 * 6 * 6 * 5 * 3 * 2
