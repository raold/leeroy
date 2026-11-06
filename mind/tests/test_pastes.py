"""Addon paste detection (variety track D1): synthetic lines, invented names."""
import random

import pytest

from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.talk.pastes import PASTE_BURST_S, PASTE_SETTLE_S, PasteBurst, focus_row, is_addon_row, named


def row(i, text):
    return ChatLineRow(id=i, ts=100.0 + i, channel="guild", from_guid=1, from_name="Owner", to_guid=0,
                       text=text, map=0, zone=0, session_id=1, thread_id=None, origin="human")


@pytest.mark.parametrize("text", ["Recount - Damage Done for Overall Data", "3. Kath 327412 (7008.0, 1.6%)",
                                  "RBS:: missing flask: Arkon", "<Flask of the Frost Wyrm> Belra",
                                  "1. Zan 12.5k (900.1)", "Skada: Damage", "[RBS] done"])
def test_addon_rows_are_pastes(text):
    assert is_addon_row(text)


@pytest.mark.parametrize("text", ["3 more pulls", "1. pull the boss", "lol 10/10"])
def test_ordinary_lines_are_not_pastes(text):
    assert not is_addon_row(text)


def test_named_and_focus_rows():
    rows = [row(1, "Recount - Damage Done for Overall Data"), row(2, "1. Zan 327412 (7008.0, 1.6%)"),
            row(3, "2. Testmage 200000 (5000.0, 1.0%)"), row(4, "3. Zan 1 (1.0, 0.1%)")]
    names = {101: "Zan", 102: "Testmage", 103: "Quillon"}
    assert named(rows, names) == [101, 102]
    assert focus_row(rows, random.Random(1)) in (rows[1], rows[3])
    only = [row(1, "Recount - x")]
    assert focus_row(only, random.Random(1)) is only[0]
    assert PASTE_BURST_S == 60.0 and PASTE_SETTLE_S == 5.0
    assert PasteBurst("guild", 0, 100.0, rows).rows is rows


def test_named_strips_realm_suffix_and_trailing_punctuation():
    names = {101: "Zan", 102: "Testmage"}
    rows = [row(1, "1. Zan-Quillonrealm: 5000 (1.0, 1%)"), row(2, "2. Testmage, 400 (1.0, 1%)")]
    assert named(rows, names) == [101, 102]


def test_named_reads_rbs_and_flask_rows():
    names = {101: "Zan", 102: "Testmage", 103: "Quillon"}
    rows = [row(1, "RBS:: missing flask: Zan"), row(2, "<Flask of the Frost Wyrm> Testmage-Realm,")]
    assert named(rows, names) == [101, 102]
    assert named([row(3, "RBS:: missing flask: Nobody")], names) == []
