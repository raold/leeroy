"""One mind per member (2026-11-12): a whisper and a Discord DM between her and him are one
conversation, and the officers' channels in the game and on Discord are one audience. What she
knows reaches a turn by its audience, never by its medium; public conversations still keep to
their own place (R11). Invented members only (TEST_CAST 101-105, him 1)."""
from datetime import timezone

import pytest

from conftest import run
from leeroy_mind.discord.channels import is_officer_channel, is_restricted
from leeroy_mind.memory.privacy import OFFICERS as OFFICER_SCOPE, backfill_officer_scopes, derive_scope
from leeroy_mind.memory.recall import OFFICERS_TAIL, PRIVATE_TAIL, format_memory, hidden_ok, scope_ok, whisper_ok
from nightly.test_night_discord_privacy import DM, GENERAL, OFFICER_AUDIENCE, OFFICERS, put, recall


@pytest.mark.parametrize("channel,frm,to,turn_channel,speaker,to_guid,ok", [
    # the pair: one conversation in either medium
    ("whisper", 102, 1, "dm:102", 102, 1, True),
    ("dm:102", 1, 102, "whisper", 102, 1, True),
    ("dm:102", 102, 1, "dm:102", 102, 1, True),
    ("whisper", 102, 1, "whisper", 102, 1, True),
    # never another pair, nor a public or officers' turn
    ("whisper", 102, 1, "dm:103", 103, 1, False),
    ("dm:102", 1, 102, "whisper", 103, 1, False),
    ("whisper", 102, 103, "dm:102", 102, 1, False),
    ("dm:102", 1, 102, "guild", 102, 0, False),
    ("dm:102", 1, 102, "officer", 102, 0, False),
    ("whisper", 102, 1, GENERAL, 102, 0, False),
    # public conversations keep to their place (R11)
    ("guild", 1, 0, "dm:102", 102, 1, False),
    (GENERAL, 1, 0, "guild", 102, 0, False),
    (GENERAL, 1, 0, GENERAL, 102, 0, True),
    ("guild", 1, 0, "whisper", 102, 1, True),
    ("party", 1, 0, "guild", 102, 0, True),
    # the officers' game lines reach only the officers' game turns now
    ("officer", 1, 0, "officer", 102, 0, True),
    ("officer", 1, 0, "voice:officers", 102, 0, True),
    ("officer", 1, 0, "guild", 102, 0, False),
    ("officer", 1, 0, "whisper", 102, 1, False),
    ("officer", 1, 0, OFFICERS, 102, 0, False),
])
def test_line_reach_by_audience(channel, frm, to, turn_channel, speaker, to_guid, ok):
    assert whisper_ok(channel, frm, to, speaker=speaker, turn_channel=turn_channel,
                      to_guid=to_guid) is ok


def test_officer_channels_are_one_audience():
    for c in ("officer", "voice:officers", OFFICERS, "discord:applications", "discord:bank-requests"):
        assert is_officer_channel(c) and is_restricted(c), c
    for c in ("guild", "whisper", DM, GENERAL, "voice:general", "discord:no-such-channel", None):
        assert not is_officer_channel(c), c
    assert hidden_ok({"officer"}, OFFICERS) and hidden_ok({OFFICERS}, "voice:officers")
    assert not hidden_ok({"officer"}, "guild") and not hidden_ok({"officer"}, "whisper")
    assert hidden_ok({"discord:no-such-channel"}, "discord:no-such-channel")
    assert not hidden_ok({"discord:no-such-channel"}, OFFICERS)
    assert not hidden_ok({"officer", "discord:no-such-channel"}, "officer")


class _M:
    def __init__(self, scope, scope_with, owner=102, key=None):
        self.scope, self.scope_with, self.owner, self.key = scope, scope_with, owner, key


@pytest.mark.parametrize("m,channel,to,ok", [
    (_M("private", 1), "whisper", 1, True),
    (_M("private", 1), "dm:102", 1, True),
    (_M("private", 1), "dm:103", 1, False),     # another member's DM is never hers
    (_M("private", 1), "dm:x", 1, False),       # a malformed DM matches nobody
    (_M("private", 1), "guild", 0, False),
    (_M("private", 0), "whisper", 1, False),
    (_M("private", 1, key="bond:1:first_kill:first"), "dm:102", 1, True),
    (_M("private", 1, key="bond:1:first_kill:first"), "guild", 0, False),
    (_M("private", OFFICER_SCOPE), "officer", 0, True),
    (_M("private", OFFICER_SCOPE), OFFICERS, 0, True),
    (_M("private", OFFICER_SCOPE), "voice:officers", 0, True),
    (_M("private", OFFICER_SCOPE), "guild", 0, False),
    (_M("private", OFFICER_SCOPE), "whisper", 1, False),
    (_M("private", OFFICER_SCOPE), "dm:102", 1, False),
    (_M("secret", OFFICER_SCOPE), "officer", 0, False),
])
def test_scope_by_audience(m, channel, to, ok):
    assert scope_ok(m, turn_channel=channel, to_guid=to) is ok


def test_a_whisper_memory_reaches_their_dm_and_a_dm_one_their_whispers(h):
    """The 2026-11-12 split: a memory resting on their whispers never reached her DMs with him,
    and one on their DMs never her whispers."""
    w = put(h, h.T, 1, "my sister is getting married next month", "whisper", to=102)
    d = put(h, h.T + 10, 1, "got the new job btw", DM, to=102)
    by_whisper = h.mem(102, "Testy told me his sister is getting married.", actors=(102, 1),
                       evidence=(f"chat:{w}",), scope="private", scope_with=1)
    by_dm = h.mem(102, "Testy got a new job.", actors=(102, 1), evidence=(f"chat:{d}",),
                  scope="private", scope_with=1)
    r = recall(h)

    def got(channel, to):
        return {m.id for m in run(r.recall(102, "sister married new job", present=[1],
                                           addressee=1, channel=channel, to_guid=to))}
    assert got("whisper", 1) == got(DM, 1) == {by_whisper, by_dm}
    for channel, to in (("guild", 0), (GENERAL, 0), ("dm:103", 1), ("whisper", 103), ("officer", 0)):
        assert not got(channel, to) & {by_whisper, by_dm}, channel
    assert not {m.id for m in run(r.recall(103, "sister married new job", present=[1],
                                           addressee=1, channel="dm:103", to_guid=1))} & {
        by_whisper, by_dm}


def test_officer_lines_scope_to_the_officers_in_either_medium(h):
    game = put(h, h.T, 102, "we should bench arkon next week", "officer")
    disc = put(h, h.T + 5, 102, "trial for the new hunter ends friday", OFFICERS,
               audience=OFFICER_AUDIENCE)
    pair = put(h, h.T + 10, 1, "dont worry about the bench", "whisper", to=102)
    conn = h.store.conn

    def scope(*ids):
        d = derive_scope(conn, 102, [f"chat:{i}" for i in ids])
        return d.scope, d.scope_with
    assert scope(game) == scope(disc) == scope(game, disc) == ("private", OFFICER_SCOPE)
    # officers' and pair evidence together reach nobody: neither audience holds the other
    assert scope(game, pair) == ("private", 0)
    # a Discord channel the layout lacks still stays with nobody (R10, fail closed)
    odd = put(h, h.T + 20, 102, "hmm", "discord:no-such-channel", audience=(1, 102))
    assert scope(odd) == scope(game, odd) == ("private", 0)
    mid = h.mem(102, "We might bench Arkon.", actors=(102, 101), evidence=(f"chat:{game}",),
                scope="private", scope_with=OFFICER_SCOPE)
    r = recall(h)

    def got(channel, to=0):
        return {m.id for m in run(r.recall(102, "bench arkon", present=[], addressee=None,
                                           channel=channel, to_guid=to))}
    for channel in ("officer", "voice:officers", OFFICERS, "discord:applications"):
        assert mid in got(channel), channel
    for channel, to in (("guild", 0), (GENERAL, 0), ("whisper", 1), (DM, 1), ("party", 0)):
        assert mid not in got(channel, to), channel
    (m,) = h.store.get_memories([mid])
    line = format_memory(m, h.T, tz=timezone.utc)
    assert line.endswith(OFFICERS_TAIL) and not line.endswith(PRIVATE_TAIL)


def test_officer_backfill_raises_game_officer_memories_and_opens_discord_ones_to_officers(h):
    game = put(h, h.T, 102, "we should bench arkon next week", "officer")
    disc = put(h, h.T + 5, 102, "trial for the new hunter ends friday", OFFICERS,
               audience=OFFICER_AUDIENCE)
    pair = put(h, h.T + 10, 1, "dont worry about the bench", "whisper", to=102)
    guild = put(h, h.T + 15, 102, "raid at eight", "guild")
    old_game = h.mem(102, "We might bench Arkon.", evidence=(f"chat:{game}",))  # stored open before
    old_disc = h.mem(102, "The hunter's trial ends Friday.", evidence=(f"chat:{disc}",),
                     scope="private", scope_with=0)  # R10's fail-closed class
    reflection = h.mem(102, "Officer talk worries me.", evidence=(f"memory:{old_game}",),
                       kind="reflection", source="reflection")
    mixed = h.mem(102, "Testy said not to worry about the bench.",
                  evidence=(f"chat:{game}", f"chat:{pair}"), scope="private", scope_with=0)
    public = h.mem(102, "Raid at eight.", evidence=(f"chat:{guild}",))
    paired = h.mem(102, "Testy reassured me.", evidence=(f"chat:{pair}",), scope="private",
                   scope_with=1)
    changed = backfill_officer_scopes(h.store.conn)
    rows = {m.id: (m.scope, m.scope_with) for m in h.store.get_memories(
        [old_game, old_disc, reflection, mixed, public, paired])}
    assert rows == {old_game: ("private", OFFICER_SCOPE), old_disc: ("private", OFFICER_SCOPE),
                    reflection: ("private", OFFICER_SCOPE), mixed: ("private", 0),
                    public: ("open", 0), paired: ("private", 1)}
    assert changed == 3
    assert backfill_officer_scopes(h.store.conn) == 0  # idempotent


def test_officer_scope_migration_is_registered(tmp_path):
    from leeroy_mind.storage.db import DATA_STEPS, latest_version
    assert DATA_STEPS["officer_scope"] == "leeroy_mind.memory.privacy:backfill_officer_scopes"
    assert latest_version() >= 39


def test_slang_learned_from_his_officer_lines_keeps_its_public_evidence(h):
    """Slang is learned from public lines only: his officer lines a slang memory cited (taken as
    public before officer chat was restricted) leave its evidence, and it stays open."""
    off = put(h, h.T, 1, "pog that went well", "officer")
    pub = put(h, h.T + 5, 1, "pog nice pull", "guild")
    slang = h.mem(102, "Zan says pog.", key="slang:pog", evidence=(f"chat:{off}", f"chat:{pub}"))
    only = h.mem(103, "Zan says pog.", key="slang:pog", evidence=(f"chat:{off}",))
    reflection = h.mem(102, "Zan has his own way of talking.", evidence=(f"memory:{slang}",),
                       kind="reflection", source="reflection")
    backfill_officer_scopes(h.store.conn)
    rows = {m.id: m for m in h.store.get_memories([slang, only, reflection])}
    assert rows[slang].evidence == (f"chat:{pub}",) and rows[slang].scope == "open"
    assert rows[reflection].scope == "open"
    # nothing public left to rest on: it stays with the officers
    assert (rows[only].scope, rows[only].scope_with) == ("private", OFFICER_SCOPE)
