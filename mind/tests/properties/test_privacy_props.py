"""Audiences (memory/recall.py, groups/audience.py; memory/AUDIENCES.md) over generated memories,
chat lines, turns and group reader sets: what was said to a pair, the officers or a group DM
never reaches a turn the whole guild reads."""
from types import SimpleNamespace

import pytest

pytest.importorskip("hypothesis", reason='Hypothesis comes with the test extra: pip install -e ".[test]"')

from hypothesis import given, strategies as st

from leeroy_mind.discord.channels import is_officer_channel, is_private, is_public
from leeroy_mind.groups.audience import GroupSource, parse_readers
from leeroy_mind.memory.privacy import GROUP, OFFICERS
from leeroy_mind.memory.recall import hidden_ok, scope_ok, whisper_ok

GUIDS = st.integers(1, 6)
GAME = ("guild", "say", "party", "raid", "yell", "officer", "voice:officers", "voice:raid", "whisper")
DISCORD = ("discord:general", "discord:memes", "discord:loot-and-dkp", "discord:officer-chat",
           "discord:applications", "discord:bank-requests", "discord:not-in-the-layout")
channels = st.one_of(st.sampled_from(GAME + DISCORD), GUIDS.map(lambda g: f"dm:{g}"),
                     st.integers(1, 3).map(lambda n: f"gdm:{n}"), st.just("dm:x"))
readers = st.one_of(st.frozensets(GUIDS, max_size=6), st.lists(GUIDS, max_size=6),
                    st.sampled_from(("[1,2]", "[2,3,4]", "not json", "[1]", "[0,1]")))
memories = st.builds(
    lambda owner, scope, scope_with, bond: SimpleNamespace(owner=owner, scope=scope, scope_with=scope_with,
                                                           key="bond:1" if bond else None),
    GUIDS, st.sampled_from(("open", "private", "secret")),
    st.one_of(GUIDS, st.sampled_from((0, OFFICERS, GROUP))), st.booleans())


@given(memories, channels, GUIDS)
def test_a_scoped_memory_reaches_only_its_own_audience(m, turn_channel, to_guid):
    """scope_ok, for any memory and any turn:
    - a private or secret memory never reaches a turn the whole guild reads (is_public);
    - one the officers may know reaches only the officers' turns, in either medium;
    - a pair's (scope_with a member) reaches only a whisper or DM turn between its owner and that
      partner, never another member's DM; one shared with nobody reaches no turn;
    - a group DM's reaches only a pair or group turn (hidden_ok then checks its readers);
    - a bond moment reaches only a private turn."""
    ok = scope_ok(m, turn_channel=turn_channel, to_guid=to_guid)
    if m.key and not is_private(turn_channel):
        assert not ok
    if not ok or m.scope == "open":
        return
    assert not is_public(turn_channel)
    if m.scope_with == OFFICERS:
        assert m.scope == "private" and is_officer_channel(turn_channel)
    elif m.scope_with == GROUP:
        assert m.scope == "private" and (turn_channel.startswith("gdm:") or is_private(turn_channel))
    else:
        assert m.scope_with > 0 and is_private(turn_channel)
        assert to_guid == m.scope_with or to_guid == m.owner == m.scope_with
        if turn_channel.startswith("dm:"):
            assert turn_channel[3:] in (str(m.owner), str(m.scope_with))


@given(channels, GUIDS, GUIDS, GUIDS, channels, GUIDS, readers, readers)
def test_a_private_or_restricted_line_never_reaches_a_public_turn(channel, frm, to, speaker, turn_channel,
                                                                  to_guid, source_readers, current):
    """whisper_ok, for any line, turn and reader sets (captured and current):
    - a line from a whisper, a DM, the officers' rooms, a group DM or a Discord channel the
      layout lacks never reaches a turn the whole guild reads;
    - a private line reaches only a private turn between the same two people;
    - a group DM line reaches only a pair or group turn whose current readers (the pair, or the
      group DM's readers) include the speaker and are all among the line's captured readers, a
      group of 2-5; a DM turn only its own member's."""
    ok = whisper_ok(channel, frm, to, speaker=speaker, turn_channel=turn_channel, to_guid=to_guid,
                    source_readers=source_readers, readers=current)
    if not ok:
        return
    if not is_public(channel):
        assert not is_public(turn_channel)
    if is_private(channel):
        assert is_private(turn_channel) and {frm, to} == {speaker, to_guid}
    if channel.startswith("gdm:"):
        assert is_private(turn_channel) or turn_channel.startswith("gdm:")
        before = parse_readers(source_readers)
        now = frozenset({speaker, to_guid}) if is_private(turn_channel) else parse_readers(current)
        assert 2 <= len(before) <= 5 and speaker in now and now <= before
        if turn_channel.startswith("dm:"):
            assert turn_channel[3:] in (str(speaker), str(to_guid))


restricted = st.one_of(
    st.sampled_from(("officer", "voice:officers", "discord:officer-chat", "discord:applications",
                     "discord:bank-requests", "discord:not-in-the-layout", "gdm:1")),
    st.builds(GroupSource, st.integers(1, 3).map(lambda n: f"gdm:{n}"), st.frozensets(GUIDS, min_size=2, max_size=5)))


@given(st.frozensets(restricted, min_size=1, max_size=4), channels, GUIDS, GUIDS, readers)
def test_a_memory_on_restricted_lines_never_reaches_a_public_turn(sources, turn_channel, speaker, to_guid,
                                                                  current):
    """hidden_ok, for any set of restricted channels a memory rests on and any turn: never a turn
    the whole guild reads; the officers' lines only the officers' turns; a group DM's lines only
    a turn whose readers all read them (bare group channels never: their audience is unproven)."""
    ok = hidden_ok(sources, turn_channel, speaker=speaker, to_guid=to_guid, readers=current)
    if not ok:
        return
    assert not is_public(turn_channel)
    bare = [c for c in sources if isinstance(c, str)]
    groups = [c for c in sources if isinstance(c, GroupSource)]
    assert not (groups and bare) and not any(c.startswith("gdm:") for c in bare)
    if bare and set(bare) != {turn_channel}:
        assert is_officer_channel(turn_channel) and all(is_officer_channel(c) for c in bare)
