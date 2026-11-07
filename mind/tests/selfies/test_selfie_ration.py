"""The photo ration (P4 Task 5). Every person here is invented."""
from leeroy_mind.selfies.ration import (BASE, DAY_S, MEMBER_GAP_S, MEMBER_STOCK, WEEK_S, chance,
                                        per_day, per_week, room, send_weight, stock_target)
from leeroy_mind.storage.rows import Relationship

from conftest import make_persona


def rel(affinity=20.0, romance_stage="none"):
    return Relationship(a=101, b=1, affinity=affinity, romance_stage=romance_stage)


def lust_woman(guid):
    return make_persona(guid, "Belra", player_gender="female", stance="lust")


def test_the_dial_maps_to_photos_a_week_and_a_day():
    assert [per_week(d) for d in range(11)] == [0, 1, 2, 3, 5, 7, 10, 14, 20, 28, 40]
    assert (per_day(0), per_day(3), per_day(10)) == (0, 1, 14)
    assert stock_target(3) == 6 and stock_target(10) == 40 and stock_target(0) == 0


def test_the_constants():
    assert (MEMBER_STOCK, MEMBER_GAP_S, WEEK_S, DAY_S) == (2, 172800.0, 604800.0, 86400.0)
    assert BASE == {"whisper": 0.35, "reply": 0.25, "guild": 0.04, "guild_photo": 0.5}


def test_cold_and_ex_members_send_fewer_and_flirty_women_more():
    man, woman = make_persona(101, "Testa", player_gender="male"), lust_woman(102)
    warm, cold, ex = rel(affinity=20), rel(affinity=-5), rel(affinity=20, romance_stage="ex")
    assert send_weight(man, warm) == 1.0 and send_weight(man, cold) == 0.25
    assert send_weight(man, ex) == 0.1
    assert send_weight(woman, warm) == 2.0
    assert send_weight(man, None) == 1.0


def test_drawn_and_fond_women_send_less_than_lust():
    drawn = make_persona(103, "Dunna", player_gender="female", stance="drawn")
    fond = make_persona(104, "Cora", player_gender="female", stance="fond")
    none = make_persona(105, "Eda", player_gender="female", stance="none")
    assert send_weight(drawn, None) == 1 + 2.0 / 3
    assert send_weight(fond, None) == 1 + 0.25 * 3.0 / 3
    assert send_weight(none, None) == 1.0


def test_chance_is_capped_and_room_needs_both_the_week_and_the_day():
    assert chance("whisper", 1.0) == 0.35 and chance("guild", 100.0) == 0.9
    assert room(3, week=2, day=0) and not room(3, week=3, day=0) and not room(3, week=0, day=1)
    assert not room(0, week=0, day=0)
