"""The calendar a prompt knows (fix wave G1, item 6): on Halloween, with Hallow's End running,
nobody in rehearsal 2 said a word about it."""
from datetime import date, datetime, timezone

import pytest

from leeroy_mind.talk.seasons import (day_line, holiday_topics, holidays_on, thanksgiving,
                                      tonight)

UTC = timezone.utc


@pytest.mark.parametrize("when, line", [
    (datetime(2026, 10, 31, 20, 30, tzinfo=UTC),
     "It's Saturday evening. It's Halloween, and Hallow's End is on in the game."),
    (datetime(2026, 11, 1, 1, 30, tzinfo=UTC),  # 01:30 is still Halloween night
     "It's late on Saturday night. It's Halloween, and Hallow's End is on in the game."),
    (datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
     "It's Tuesday morning. Hallow's End is on in the game."),
    (datetime(2026, 11, 5, 14, 0, tzinfo=UTC), "It's the middle of the day on Thursday."),
    (datetime(2026, 12, 25, 21, 0, tzinfo=UTC),
     "It's Friday evening. It's Christmas Day, and the Feast of Winter Veil is on in the game."),
    (datetime(2027, 1, 2, 4, 0, tzinfo=UTC),  # 04:00 is still New Year's Day night
     "It's the small hours of Friday night. It's New Year's Day, and the Feast of Winter Veil "
     "is on in the game."),
    (datetime(2027, 1, 2, 20, 0, tzinfo=UTC),  # Winter Veil wraps the year
     "It's Saturday evening. The Feast of Winter Veil is on in the game."),
    (datetime(2026, 2, 14, 20, 0, tzinfo=UTC),
     "It's Saturday evening. It's Valentine's Day, and Love is in the Air is on in the game."),
    (datetime(2026, 9, 25, 20, 0, tzinfo=UTC), "It's Friday evening. Brewfest is on in the game."),
    (datetime(2026, 6, 30, 20, 0, tzinfo=UTC),
     "It's Tuesday evening. The Midsummer Fire Festival is on in the game."),
])
def test_right_now_carries_the_weekday_and_tonights_holidays(when, line):
    assert day_line(when) == line


def test_thanksgiving_is_the_fourth_thursday_and_only_for_the_us():
    assert thanksgiving(2026) == date(2026, 11, 26) and thanksgiving(2009) == date(2009, 11, 26)
    night = datetime(2026, 11, 26, 20, 0, tzinfo=UTC)
    assert [h.name for h in holidays_on(tonight(night))] == ["Thanksgiving"]
    assert day_line(night, "US") == "It's Thursday evening. It's Thanksgiving."
    assert day_line(night, "UK") == "It's Thursday evening."
    assert holiday_topics(night, "UK") == [] and holiday_topics(night, "US")


def test_halloween_topics_are_concrete_situations():
    topics = holiday_topics(datetime(2026, 10, 31, 20, 0, tzinfo=UTC), "UK")
    assert len(topics) == 2 and any("trick-or-treaters" in t for t in topics)
    assert any("Headless Horseman" in t for t in topics)
