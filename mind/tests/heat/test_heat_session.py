"""The session override: fixed phrases only, who it reaches, and how it ends. Then a rehearsal
through the talk rig (FakeLLM): his whispers to a lust woman through "keep it clean tonight",
"back to normal", "no limits tonight" and his logout, read in the prompts she was given."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from conftest import HUMAN
from engine_rig import OVERLAY, make_rig, save_member
from heat.rig import attach
from leeroy_mind.heat import session
from leeroy_mind.heat.live import ACK
from leeroy_mind.heat.session import ALL, IDLE_S, KEY, SessionBook, parse
from leeroy_mind.talk.romance import BUILTIN_ROMANCE


@pytest.mark.parametrize("text,code", [
    ("keep it clean tonight", "clean"), ("Keep it CLEAN ok", "clean"), ("not tonight", "clean"),
    ("behave tonight please", "clean"), ("no limits tonight", "open"),
    ("don't hold back tonight", "open"), ("don’t hold back tonight", "open"),
    ("dont hold back tonight", "open"), ("ok back to normal", "clear"),
    ("keep it clean, no limits tonight", "clean"),
])
def test_the_listed_phrases_and_only_those(text, code):
    assert parse(text) == code


@pytest.mark.parametrize("text", [
    "", "hey you", "no limits", "tonight was fun", "clean your bags", "hold back the adds",
    "you never hold back", "be good", "keep it down", "normal night", "nothing tonight?",
])
def test_anything_else_is_not_an_override(text):
    assert parse(text) is None


def test_set_read_and_clear_in_meta_with_no_words_of_his(store, clock):
    book = SessionBook(store, clock)
    book.set(104, "clean", medium="game", ack=True)
    rec = json.loads(store.get_meta(KEY.format(guid=104)))
    assert set(rec) == {"mode", "medium", "at", "last", "until", "ack"} and rec["mode"] == "clean"
    assert book.mode(104) == "clean" and book.mode(102) is None
    assert book.take_ack(104) == "clean" and book.take_ack(104) is None  # once
    assert SessionBook(store, clock).mode(104) == "clean"  # a restart reads it back
    assert book.view() == {"all": None, "members": {"104": {"mode": "clean", "ends": "logout"}}}
    assert session.read(store.conn, clock.now()) == book.view()
    assert book.clear(104) == 1 and book.mode(104) is None
    assert store.get_meta(KEY.format(guid=104)) is None and book.clear(104) == 0


def test_everyones_and_hers_together_clean_wins(store, clock):
    book = SessionBook(store, clock)
    book.set(None, "open", medium="game")
    assert book.mode(104) == "open" and book.mode(102) == "open"
    book.set(104, "clean", medium="game")
    assert book.mode(104) == "clean" and book.mode(102) == "open"
    assert store.get_meta(ALL) is not None
    assert session.mode_in(book.view(), 104) == "clean" and session.mode_in(book.view(), 9) == "open"
    assert book.clear(everything=True) == 2 and book.view() == {"all": None, "members": {}}


def test_said_in_game_it_ends_at_his_logout_and_not_by_time(store, clock):
    book = SessionBook(store, clock)
    book.set(104, "open", medium="game")
    asyncio.run(clock.advance(3 * IDLE_S, 3600))
    assert book.mode(104) == "open"
    assert book.end_game() == 1 and book.mode(104) is None


def test_said_on_discord_it_ends_after_six_idle_hours_or_at_six_in_the_morning(store, clock):
    book = SessionBook(store, clock)  # 20:00 UTC on the test clock
    book.set(104, "clean", medium="discord")
    asyncio.run(clock.advance(IDLE_S - 60, 600))
    assert book.mode(104) == "clean"
    book.touch(104)  # a line of his in that conversation: the idle limit starts again
    asyncio.run(clock.advance(3 * 3600, 600))
    assert book.mode(104) == "clean"
    assert book.end_game() == 0  # his logout is not its end
    local = clock.local()
    to_six = ((6 - local.hour) % 24) * 3600 - local.minute * 60
    asyncio.run(clock.advance(to_six + 60, 600))
    assert book.mode(104) is None and store.get_meta(KEY.format(guid=104)) is None
    book.set(102, "open", medium="discord")
    asyncio.run(clock.advance(IDLE_S + 1, 3600))
    assert book.mode(102) is None  # six quiet hours
    assert session.read(store.conn, clock.now()) == {"all": None, "members": {}}


def test_damaged_rows_are_no_override(store, clock):
    store.set_meta(KEY.format(guid=104), "{not json")
    store.set_meta(KEY.format(guid=105), json.dumps({"mode": "wild", "medium": "game"}))
    assert SessionBook(store, clock).view() == {"all": None, "members": {}}
    assert session.read(store.conn, clock.now()) == {"all": None, "members": {}}


# -- through the engine -----------------------------------------------------------------------------

def _cast(store):
    save_member(store, 104, player_gender="female", stance="lust", style="bold")
    save_member(store, 105, player_gender="female", stance="none")
    save_member(store, 101, stance="none")


def _rig(store, clock, tmp_path):
    _cast(store)
    r = make_rig(store, clock, tmp_path, reply="ok then")
    r.engine.whispers.set_rate(0.0)
    r.engine.budget.set_to_him_rate(0.0)
    live = attach(r.engine, store, clock, tmp_path)
    return r, live


def _last_prompt(r) -> str:
    return r.prompts()[-1]


def test_a_whisper_conversation_through_clean_normal_open_and_logout(store, clock, tmp_path):
    r, live = _rig(store, clock, tmp_path)
    explicit, warm = OVERLAY, BUILTIN_ROMANCE["warm"]

    async def go():
        await r.session()
        await r.human("hey you", channel="whisper", to_guid=104)
        await r.advance(40, step=2)
        assert explicit in _last_prompt(r)  # heat 9: as before the dial

        await r.human("keep it clean tonight ok", channel="whisper", to_guid=104)
        await r.advance(40, step=2)
        text = _last_prompt(r)
        assert explicit not in text and warm in text
        assert ACK["clean"].format(name=r.engine.human.called) in text  # she says so, once
        assert live.sessions.mode(104) == "clean"

        await r.human("so how was work", channel="whisper", to_guid=104)
        await r.advance(40, step=2)
        text = _last_prompt(r)
        assert explicit not in text and "asked you to keep things clean" not in text

        await r.human("ok back to normal", channel="whisper", to_guid=104)
        await r.advance(40, step=2)
        assert explicit in _last_prompt(r) and live.sessions.mode(104) is None

        await r.human("no limits tonight", channel="whisper", to_guid=104)
        await r.advance(40, step=2)
        text = _last_prompt(r)
        assert explicit in text and ACK["open"].format(name=r.engine.human.called) in text
        assert live.resolve(r.engine.personas.get(104)).heat == 10

        await r.engine.end_session()  # his logout ends it
        assert live.sessions.mode(104) is None
        assert live.resolve(r.engine.personas.get(104)).heat == 9
    asyncio.run(go())


def test_it_reaches_only_the_member_he_said_it_to_and_never_her_stance(store, clock, tmp_path):
    r, live = _rig(store, clock, tmp_path)

    async def go():
        await r.session()
        assert live.his_line(104, "keep it clean tonight") == "clean"
        assert live.sessions.mode(104) == "clean" and live.sessions.mode(102) is None
        # a woman with stance none and a man: nothing to override, and "open" changes nothing
        assert live.his_line(105, "no limits tonight") is None
        assert live.his_line(101, "no limits tonight") is None
        assert live.sessions.view()["members"].keys() == {"104"}
        for guid in (105, 101):
            rec = live.resolve(r.engine.personas.get(guid))
            assert not rec.applies and rec.tier == "none" and rec.session is None
        assert live.his_line(104, "what a night") is None  # no phrase: nothing
        assert live.his_line(104, "back to normal") == "clear"
        assert live.his_line(104, "back to normal") is None  # nothing left to end
    asyncio.run(go())


def test_a_public_line_of_his_starts_nothing(store, clock, tmp_path):
    r, live = _rig(store, clock, tmp_path)

    async def go():
        await r.session()
        await r.human("no limits tonight")  # guild chat
        await r.human("keep it clean tonight", channel="party")
        await r.advance(20, step=2)
        assert live.sessions.view() == {"all": None, "members": {}}
    asyncio.run(go())


def test_the_console_button_sets_everyone_and_clear_ends_all(store, clock, tmp_path):
    r, live = _rig(store, clock, tmp_path)

    async def go():
        out = live.set_all("clean")
        assert out["ok"] and out["sessions"]["all"] == {"mode": "clean", "ends": "morning"}
        assert live.resolve(r.engine.personas.get(104)).heat == 2
        await r.session()  # in game now: one set here ends at his logout
        live.set_all("open", guid=104)
        assert live.sessions.view()["members"]["104"] == {"mode": "open", "ends": "logout"}
        assert live.resolve(r.engine.personas.get(104)).heat == 2  # clean wins
        assert live.set_all("clear")["ended"] == 2
        assert live.resolve(r.engine.personas.get(104)).heat == 9
    asyncio.run(go())


def test_a_hello_with_him_offline_clears_at_a_mind_start_only(store, clock, tmp_path):
    r, live = _rig(store, clock, tmp_path)
    live.sessions.set(104, "clean", medium="discord")
    live.sessions.set(None, "open", medium="game")
    live.on_hello(False)  # the first hello of this start, him offline: all of it
    assert live.sessions.view() == {"all": None, "members": {}}
    live.sessions.set(104, "clean", medium="discord")
    live.sessions.set(None, "open", medium="game")
    live.on_hello(False)  # a later one: only what was said in game (he left meanwhile)
    assert live.sessions.view() == {"all": None, "members": {"104": {"mode": "clean",
                                                                      "ends": "morning"}}}
    live.on_hello(True)
    assert live.sessions.mode(104) == "clean"


def test_the_clock_fixture_is_the_evening():
    assert datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc).hour == 20
