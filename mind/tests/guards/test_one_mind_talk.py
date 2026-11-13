"""One mind per member in talk (2026-11-12): a private turn between her and him shows their lines
from the other medium, labelled with where they were said, and her repeats count across both.
Public conversations keep to their own place. Invented members only (101-105, him 1)."""
import pytest

from guards.test_live import rig, turn  # noqa: F401  (the fixture)
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.talk.pipeline import PAIR_LINES, TurnMeta
from leeroy_mind.talk.prompt import chat_line_text
from leeroy_mind.talk.turns import Turn, TurnKind


def line(rig, text, channel, who, to=0, ago=60.0):
    audience = rig.world.audience(channel, from_guid=who, to_guid=to)
    at = rig.clock.now() - ago
    chat = Chat(int(at * 1000), channel, who, "Invented", to, text, 0, 10)
    lid = rig.store.insert_chat_line(chat, session_id=None, origin="human" if who == 1 else "mind",
                                     audience=audience)
    rig.store.conn.execute("UPDATE chat_line SET ts = ? WHERE id = ?", (at, lid))
    return lid


def prompt(rig, t):
    return str(rig.sync(rig.engine._compose(t, TurnMeta("one-mind"), live=False)).messages)


def test_a_whisper_to_him_shows_their_dm_lines_labelled(rig):
    line(rig, "HIS_DM_SENTINEL", "dm:101", 1, 101, ago=3600)
    line(rig, "HER_DM_SENTINEL", "dm:101", 101, 1, ago=3500)
    line(rig, "OTHER_DM_SENTINEL", "dm:102", 102, 1, ago=3400)
    line(rig, "OLD_DM_SENTINEL", "dm:101", 101, 1, ago=3 * 86400)
    text = prompt(rig, turn("whisper"))
    assert "HIS_DM_SENTINEL" in text and "HER_DM_SENTINEL" in text and "(Discord DM" in text
    assert "OTHER_DM_SENTINEL" not in text and "OLD_DM_SENTINEL" not in text
    # never in a public turn, nor a whisper to someone else
    for t in (turn("guild"), Turn(TurnKind.REPLY_HUMAN, 101, "whisper", to_guid=102, addressee=102),
              turn("discord:general")):
        body = prompt(rig, t)
        assert "HIS_DM_SENTINEL" not in body and "HER_DM_SENTINEL" not in body, t.channel


def test_a_dm_shows_their_whispers_after_a_restart(rig):
    line(rig, "HIS_WHISPER_SENTINEL", "whisper", 1, 101, ago=7200)
    line(rig, "OTHER_PAIR_SENTINEL", "whisper", 1, 102, ago=7100)
    rig.world.history.clear()  # a restart: only the store remembers
    text = prompt(rig, turn("dm:101"))
    assert "HIS_WHISPER_SENTINEL" in text and "OTHER_PAIR_SENTINEL" not in text


def test_an_opener_sees_only_her_own_lines_from_the_other_medium(rig):
    line(rig, "HIS_DM_SENTINEL", "dm:101", 1, 101, ago=600)
    line(rig, "HER_DM_SENTINEL", "dm:101", 101, 1, ago=500)
    t = Turn(TurnKind.OPEN, 101, "whisper", to_guid=1, addressee=1)
    text = prompt(rig, t)
    assert "HER_DM_SENTINEL" in text and "HIS_DM_SENTINEL" not in text


def test_the_pair_window_is_bounded(rig):
    for n in range(PAIR_LINES + 4):
        line(rig, f"DM_LINE_{n:02d}", "dm:101", 101, 1, ago=5000 - n)
    text = prompt(rig, turn("whisper"))
    shown = [n for n in range(PAIR_LINES + 4) if f"DM_LINE_{n:02d}" in text]
    assert shown == list(range(4, PAIR_LINES + 4))  # the newest PAIR_LINES


def test_her_repeat_counts_across_whispers_and_dms(rig):
    e = rig.engine
    said = "the copper lantern hangs above the doorway"
    own = line(rig, said, "dm:101", 101, 1)
    verdict = rig.sync(e.live_review.check(turn("whisper"), TurnMeta("test"), said))
    assert verdict.action == "drop" and "own_repeat_cross_channel" in verdict.codes
    assert any(r.id == own and r.kind == "line" for r in verdict.related)
    other = Turn(TurnKind.REPLY_HUMAN, 101, "whisper", to_guid=102, addressee=102)
    verdict = rig.sync(e.live_review.check(other, TurnMeta("test"), said))
    assert not any(r.id == own for r in verdict.related)


@pytest.mark.parametrize("channel,to,tag", [
    ("dm:101", 1, "(Discord DM to Zan)"), ("dm:101", 0, "(Discord DM)"),
    ("discord:general", 0, "(Discord #general)"), ("whisper", 1, "(whisper to Zan)"),
    ("guild", 0, ""), ("party", 0, "(party)")])
def test_lines_are_labelled_with_where_they_were_said(channel, to, tag):
    row = ChatLineRow(id=1, ts=0, channel=channel, from_guid=101, from_name="Invented", to_guid=to,
                      text="hi", map=0, zone=0, session_id=None, thread_id=None, origin="mind")
    assert chat_line_text(row, {1: "Zan", 101: "Invented"}) == f"Invented{(' ' + tag) if tag else ''}: hi"
