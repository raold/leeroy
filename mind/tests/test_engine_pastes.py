"""One reply per addon paste burst (variety track D2): synthetic lines, invented names."""
import pytest

from conftest import run
from engine_rig import engine_clock, make_rig
from leeroy_mind.talk.pastes import PASTE_BURST_S


@pytest.fixture
def clock():
    return engine_clock()


def rows_for(names):
    out = ["Recount - Damage Done for Overall Data"]
    for i, n in enumerate(names, 1):
        out.append(f"{i}. {n} {400000 - i * 1000} ({9000 - i * 10}.0, {20 - i}.0%)")
    return out


async def paste(r, lines, gap=0.25):
    ids = []
    for text in lines:
        ids.append((await r.human(text)).id)
        await r.advance(gap, step=0.25)
    return ids


def trigger(r):
    return [(u["trigger_line"], u["why"]) for u in r.store.conn.execute(
        "SELECT trigger_line, why FROM utterance WHERE dropped IS NULL ORDER BY id")]


def test_a_paste_burst_gets_one_reply_from_a_named_member(store, clock, tmp_path):
    async def main():
        for seed in range(6):
            r = make_rig(store, clock, tmp_path, seed=seed)
            await paste(r, rows_for(["Arkon", "Belra", "Coros", "Zed", "Yan", "Xo", "Wim", "Vox",
                                     "Ula", "Tor", "Sef"]))
            await r.advance(60, step=2)
            says = r.sent.says()
            assert len(says) == 1, (seed, says)
            assert says[0].guid in (101, 102, 103), seed
            assert trigger(r)[-1][1] == "reply:paste"
            await r.engine.end_session()
    run(main())


def test_a_paste_naming_nobody_gets_one_reply_about_the_top_or_bottom_line(store, clock, tmp_path):
    async def main():
        for seed in range(6):
            r = make_rig(store, clock, tmp_path, seed=seed)
            ids = await paste(r, rows_for(["Foo", "Bar", "Baz", "Qux"]))
            await r.advance(60, step=2)
            assert len(r.sent.says()) == 1, seed
            line, why = trigger(r)[-1]
            assert why == "reply:paste" and line in (ids[1], ids[4]), (seed, line, ids)
            assert "pasted an addon's output" in r.prompts()[-1]
            await r.engine.end_session()
    run(main())


def test_a_second_burst_after_sixty_seconds_gets_its_own_reply(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        await paste(r, rows_for(["Foo", "Bar"]))
        await r.advance(PASTE_BURST_S + 5, step=2)
        await paste(r, rows_for(["Foo", "Bar"]))
        await r.advance(60, step=2)
        assert [w for _, w in trigger(r)] == ["reply:paste", "reply:paste"]
    run(main())


def test_his_ordinary_lines_between_pastes_are_answered_as_before(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        await paste(r, rows_for(["Foo", "Bar"]))
        await r.human("arkon you up?")
        await r.advance(60, step=2)
        whys = [w for _, w in trigger(r)]
        assert whys.count("reply:paste") == 1 and "reply:named" in whys
    run(main())
