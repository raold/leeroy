"""The talk pipeline's half of fix-wave part F's store work: every utterance row names the
line it answered, the model's time, whom a whisper went to and its turn; a bit of several rows
asks the model without the blank-line stop; and after a restart a member's own last lines
come back from the utterance log."""
import asyncio

import pytest

from conftest import HUMAN, run, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.personas.schema import Bit
from leeroy_mind.talk.turns import Turn, TurnKind


@pytest.fixture
def clock():
    return engine_clock()


async def spoken(r, turn: Turn, seconds: float = 40, step: float = 1.0) -> list[str]:
    task = asyncio.create_task(r.engine.speak(turn))
    await r.advance(seconds, step)
    assert task.done()
    return task.result()


def rows(store) -> list:
    return store.conn.execute(
        "SELECT guid, channel, dropped, trigger_line, llm_s, to_guid, turn_id FROM utterance "
        "ORDER BY id").fetchall()


def plain(store, guid: int) -> None:
    save_member(store, guid, typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0)


def test_a_said_reply_row_names_his_line_the_model_time_and_its_turn(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        plain(store, 101)
        r = make_rig(store, clock, tmp_path, reply="fair enough")
        line = await r.human("anyone around tonight?")
        said = await spoken(r, Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild",
                                    addressee=HUMAN, trigger_line=line.id))
        assert said
        mine = [u for u in rows(store) if u["guid"] == 101]
        assert [u["dropped"] for u in mine] == [None]
        assert mine[0]["trigger_line"] == line.id
        assert mine[0]["llm_s"] is not None and mine[0]["llm_s"] >= 0
        assert mine[0]["turn_id"] and mine[0]["to_guid"] == 0
    run(main())


def test_a_whispered_reply_row_says_whom_it_went_to(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        plain(store, 101)
        r = make_rig(store, clock, tmp_path, reply="sure thing")
        line = await r.human("you about?", channel="whisper", to_guid=101)
        await spoken(r, Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="whisper",
                             addressee=HUMAN, to_guid=HUMAN, trigger_line=line.id))
        mine = [u for u in rows(store) if u["guid"] == 101 and u["dropped"] is None]
        assert mine and all(u["to_guid"] == HUMAN and u["trigger_line"] == line.id
                            for u in mine)
    run(main())


def test_a_dropped_turn_row_keeps_his_line_and_the_model_time(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        r = make_rig(store, clock, tmp_path, reply="(nothing)")
        line = await r.human("anyone around tonight?")
        assert await spoken(r, Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild",
                                    addressee=HUMAN, trigger_line=line.id)) == []
        mine = [u for u in rows(store) if u["guid"] == 101]
        assert [u["dropped"] for u in mine] == ["silent"]
        assert mine[0]["trigger_line"] == line.id and mine[0]["llm_s"] is not None
        assert mine[0]["turn_id"]
    run(main())


def rows_bit(lines: int) -> Bit:
    return Bit(id="feasts", instruction="bring up the fish feasts nobody eats",
               min_days_between=3.0, voice_bit=False, contexts=("any",), lines=lines, dates=())


def test_a_bit_of_several_rows_asks_without_the_blank_line_stop(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        plain(store, 102)
        r = make_rig(store, clock, tmp_path,
                     reply="who made fish feasts again\n\nnobody eats them\n\nnobody ever does")
        said = await spoken(r, Turn(kind=TurnKind.BIT, speaker=102, channel="guild",
                                    bit=rows_bit(3), max_lines=3), seconds=90)
        assert "\n\n" not in r.chat_calls()[-1]["options"].get("stop", [])
        assert len(said) == 3
        # positive control: an ordinary reply keeps the live preset's stop
        await spoken(r, Turn(kind=TurnKind.REPLY_HUMAN, speaker=103, channel="guild",
                             addressee=HUMAN))
        assert r.chat_calls()[-1]["options"]["stop"] == list(LIVE.options["stop"])
    run(main())


def test_a_one_row_bit_keeps_the_live_stop(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        r = make_rig(store, clock, tmp_path, reply="who made fish feasts again")
        await spoken(r, Turn(kind=TurnKind.BIT, speaker=102, channel="guild", bit=rows_bit(1),
                             max_lines=1))
        assert r.chat_calls()[-1]["options"]["stop"] == list(LIVE.options["stop"])
    run(main())


def test_after_a_restart_her_last_lines_come_back_from_the_log(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        plain(store, 101)
        first = make_rig(store, clock, tmp_path, reply="the bridge repair quest is bugged again")
        said = await spoken(first, Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild",
                                        addressee=HUMAN))
        assert said
        await first.advance(120)
        again = make_rig(store, clock, tmp_path, reply="anyway")  # a fresh mind, same db
        await spoken(again, Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild",
                                 addressee=HUMAN))
        prompt = again.prompts()[-1]
        assert "Your last lines" in prompt
        own = prompt.split("Your last lines", 1)[1].split("\n\n", 1)[0]
        assert said[0] in own  # in her own lines, not only in the chat window
    run(main())


def test_after_a_restart_a_whispered_line_stays_out_of_her_guild_prompt(store, clock,
                                                                        tmp_path):
    async def main():
        seed_test_cast(store)
        plain(store, 101)
        first = make_rig(store, clock, tmp_path, reply="meet me by the bank later")
        line = await first.human("you about?", channel="whisper", to_guid=101)
        said = await spoken(first, Turn(kind=TurnKind.REPLY_HUMAN, speaker=101,
                                        channel="whisper", addressee=HUMAN, to_guid=HUMAN,
                                        trigger_line=line.id))
        assert said
        await first.advance(120)
        again = make_rig(store, clock, tmp_path, reply="anyway")
        await spoken(again, Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild",
                                 addressee=HUMAN))
        assert said[0] not in again.prompts()[-1]
    run(main())
