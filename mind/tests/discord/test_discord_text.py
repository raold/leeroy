import pytest

from leeroy_mind.discord.text import MAX_TEXT, TextRefused, clean_text, is_one_emoji


def test_reaction_emoji_are_one_grapheme():
    for ok in ("👍", "❤️", "👩🏽‍💻", "🇬🇧", "1️⃣", "🏴󠁧󠁢󠁳󠁣󠁴󠁿"):
        assert is_one_emoji(ok), ok
    for bad in ("", "👍👍", "a", "lol", ":)", "👍 ", "‍", "🇬", "🇬🇧🇫🇷", "1", "1️",
                "👍‍", "\U0001F3FD", "🏴󠁧󠁢󠁳󠁣󠁴"):
        assert not is_one_emoji(bad), bad


def test_clean_text_keeps_emoji_and_newlines_and_refuses_empty_or_long():
    assert clean_text("a\n\n\n\n\nb") == "a\n\n\nb"
    assert clean_text("hey 👋 \U0001F1EC\U0001F1E7\r\nok\x00\x07\t!") == "hey 👋 \U0001F1EC\U0001F1E7\nok!"
    assert clean_text("bad \ud800 half") == "bad � half"
    assert len(clean_text("😀" * MAX_TEXT)) == MAX_TEXT
    for s in ("", "  \n\x00 "):
        with pytest.raises(TextRefused) as e:
            clean_text(s)
        assert e.value.code == "empty"
    with pytest.raises(TextRefused) as e:
        clean_text("😀" * (MAX_TEXT + 1))
    assert e.value.code == "too_long"
