"""His lead, through the engine: below heat 9 she is one tier under her ceiling until his own
line turns it up, and that line counts whichever of the turn's readers asks first."""
import asyncio

from engine_rig import make_rig, save_member
from heat.rig import attach, set_values
from leeroy_mind.heat.live import LED_WINDOW_S
from leeroy_mind.talk.romance import BUILTIN_ROMANCE


def _rig(store, clock, tmp_path, heat):
    save_member(store, 104, player_gender="female", stance="lust", style="bold")
    r = make_rig(store, clock, tmp_path, reply="ok then")
    r.engine.whispers.set_rate(0.0)
    r.engine.budget.set_to_him_rate(0.0)
    live = attach(r.engine, store, clock, tmp_path)
    set_values(store, r.engine, [("world", "", heat)])
    return r, live


def test_she_reaches_her_ceiling_only_after_his_own_line_turns_it_up(store, clock, tmp_path):
    r, live = _rig(store, clock, tmp_path, 6)  # suggestive, after one line of his

    async def go():
        await r.session()
        await r.human("how was work today", channel="whisper", to_guid=104)
        await r.advance(40, step=2)
        first = r.prompts()[-1]
        assert BUILTIN_ROMANCE["flirty"] in first and BUILTIN_ROMANCE["suggestive"] not in first
        assert live.led(104, clock.now()) == 0

        await r.human("can't stop thinking about you", channel="whisper", to_guid=104)
        await r.advance(40, step=2)
        assert BUILTIN_ROMANCE["suggestive"] in r.prompts()[-1]  # on the very turn it answers
        assert live.led(104, clock.now()) == 1

        await r.human("anyway, how was work", channel="whisper", to_guid=104)
        await r.advance(40, step=2)
        assert BUILTIN_ROMANCE["suggestive"] in r.prompts()[-1]  # it stays for the conversation
        assert live.led(104, clock.now()) == 1  # a plain line adds nothing
    asyncio.run(go())


def test_the_same_line_counts_once_and_the_lead_fades(store, clock, tmp_path):
    r, live = _rig(store, clock, tmp_path, 2)  # warm: needs two
    now = clock.now()
    live.heard(104, "miss you", now)
    live.heard(104, "miss you", now)  # the same line asked about twice in one turn
    live.heard(104, "what a day", now)
    assert live.led(104, now) == 1
    live.heard(104, "wish you were here", now + 5)
    assert live.led(104, now + 5) == 2 and live.led(102, now) == 0
    assert live.led(104, now + LED_WINDOW_S + 10) == 0


def test_a_public_line_of_his_never_counts(store, clock, tmp_path):
    r, live = _rig(store, clock, tmp_path, 6)

    async def go():
        await r.session()
        await r.human("can't stop thinking about you Dunna")  # guild chat
        await r.advance(40, step=2)
        assert live.led(104, clock.now()) == 0
    asyncio.run(go())
