"""His DM posts in every small interleaving with her answers (review of #368, P1-1): each post
is put to her in exactly one answer that was sent, whatever arrived while she was composing, and
nothing she has answered is listed as unanswered again. Before the covered ids
(discord/covered.py) one post in ten of this grid was never put to her and still counted as
answered. Invented lines only."""
import pytest

from discord.test_discord_talk import post, rig_for, talk_for
from discord.test_own_time import at, run_for
from engine_rig import numbered
from leeroy_mind.discord import covered
from leeroy_mind.health import OWNER_ANSWER_S
from leeroy_mind.talk.owed import count_unanswered

TEXTS = ["did the parcel ever turn up", "also how was the dentist", "anyway what time is raid",
         "and who is tanking"]
GAPS = [0, 1, 3, 6, 10, 15, 25, 40]
GRID = ([(0, a) for a in GAPS] + [(0, a, b) for a in GAPS for b in GAPS]
        + [(0, a, b, c) for a in (1, 6, 15) for b in (1, 6, 15, 40) for c in (1, 6, 15)])


def asked(call) -> str:
    """The part of an answer's prompt that says which of his messages to answer."""
    user = call["messages"][-1]["content"]
    i = max(user.rfind("Answer his message:"), user.rfind("have not answered yet"))
    return user[i:] if i >= 0 else ""


def play(store, tmp_path, gaps, script=None):
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path,
                  script=script or numbered("ha {n}, ok then mate"))
    talk = talk_for(rig)
    mids = []

    async def go():
        for gap, text in zip(gaps, TEXTS):
            await rig.advance(gap)
            talk.tick(rig.clock.now())
            mids.append(post(rig, "dm:101", text))
            talk.on_his_post(mids[-1])
    rig.sync(go())
    run_for(rig, talk, 900)
    return rig, talk, mids


@pytest.mark.parametrize("gaps", GRID)
def test_every_post_is_put_to_her_in_exactly_one_sent_answer(store, tmp_path, gaps):
    rig, talk, mids = play(store, tmp_path, gaps)
    try:
        conn = rig.store.conn
        answers = conn.execute(
            "SELECT m.id, m.reply_to, k.value FROM discord_message m LEFT JOIN meta k "
            "ON k.key = 'discord:built:' || m.id WHERE m.channel = 'dm:101' "
            "AND m.origin = 'mind' AND m.reply_to IS NOT NULL ORDER BY m.id").fetchall()
        assert answers, "no answer at all"
        built = [i for _, _, value in answers for i in covered.parse(value)]
        assert sorted(built) == mids, (gaps, built)  # every post once, none twice
        # ...and the prompts agree with the receipts: each post's words were in the
        # instruction of one answer
        # (a retry of one answer asks for the same posts again: one list)
        lists = {frozenset(t for t in TEXTS if t in asked(c))
                 for c in rig.chat_calls() if c["format"] is None}
        for text in TEXTS[:len(gaps)]:
            assert sum(text in x for x in lists) == 1, (gaps, text)
        # nothing is left unanswered, for the talk, the owed watch's reading and health
        assert talk.unanswered(conn, "dm:101", None, mids[-1]) == []
        done = covered.read(conn, "dm:101")
        assert all(done.has(m) for m in mids)
        got = count_unanswered(conn, 1, 0.0, rig.clock.now(), window=OWNER_ANSWER_S)
        assert got.messages.get("discord_dm") and not got.unanswered.get("discord_dm")
    finally:
        rig.close()


def test_a_post_that_arrives_while_she_types_is_never_called_answered(store, tmp_path):
    """The review's case: A, B and C six seconds apart. Her answer to A is stored after B and
    C arrived, so by id order it looked like an answer to B."""
    rig, talk, (a, b, c) = play(store, tmp_path, (0, 6, 6))
    try:
        conn = rig.store.conn
        rows = conn.execute(
            "SELECT m.reply_to, k.value FROM discord_message m JOIN meta k "
            "ON k.key = 'discord:built:' || m.id WHERE m.channel = 'dm:101' ORDER BY m.id"
        ).fetchall()
        assert [covered.parse(v) for _, v in rows] == [(a,), (b, c)]
        assert [r for r, _ in rows] == [a, c]
    finally:
        rig.close()


def test_an_answer_without_a_receipt_reads_as_before(store, tmp_path):
    """Her answers stored before receipts existed (or posted outside the pipeline) cover his
    posts up to the one they reply to; a later post is unanswered."""
    from leeroy_mind.discord import store as dstore
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path)
    talk = talk_for(rig)
    conn, now = rig.store.conn, rig.clock.now()
    a = post(rig, "dm:101", "old question one")
    b = post(rig, "dm:101", "old question two")
    dstore.post(rig.store, channel="dm:101", author=101, origin="mind", text="old answer",
                reply_to=b, now=now)
    c = post(rig, "dm:101", "a new one")
    assert [i for i, _ in talk.unanswered(conn, "dm:101", None, c)] == [c]
    done = covered.read(conn, "dm:101")
    assert done.has(a) and done.has(b) and not done.has(c) and done.floor == b
    assert covered.parse("3,4") == (3, 4) and covered.parse("3,x") == () and covered.parse("") == ()
    rig.close()


def test_a_dropped_answer_leaves_its_posts_unanswered_for_the_next(store, tmp_path):
    """Her one answer to his two posts is refused by a hard screen on every draft: nothing
    covers them, the record of it being built goes with its turn, and the second post's own
    turn is then built with both."""
    replies = iter(["as an AI language model i cannot"] * 4)

    def script(model, messages):
        return next(replies, "ha ok, parcel came and the dentist was fine")
    rig, talk, mids = play(store, tmp_path, (0, 1), script=script)
    try:
        conn = rig.store.conn
        rows = conn.execute(
            "SELECT m.reply_to, k.value FROM discord_message m JOIN meta k "
            "ON k.key = 'discord:built:' || m.id WHERE m.channel = 'dm:101'").fetchall()
        assert [(r, covered.parse(v)) for r, v in rows] == [(mids[1], tuple(mids))]
        assert talk.unanswered(conn, "dm:101", None, mids[-1]) == []
    finally:
        rig.close()


def test_a_long_dms_old_stretch_never_reads_as_owed(store, tmp_path):
    """Review follow-up F1: only her newest covered.ANSWERS answers are read; his posts older
    than all of them are past the window and count as covered, with or without receipts."""
    from leeroy_mind.discord import store as dstore
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path)
    talk = talk_for(rig)
    conn, now = rig.store.conn, rig.clock.now()
    posts = []
    for n in range(250):
        mid = post(rig, "dm:101", f"question number {n}")
        answer = dstore.post(rig.store, channel="dm:101", author=101, origin="mind",
                             text=f"answer number {n}", reply_to=mid, now=now)
        rig.store.set_meta(covered.BUILT_KEY.format(answer), str(mid))
        posts.append(mid)
    assert covered.ANSWERS == 200
    done = covered.read(conn, "dm:101")
    assert done.older == posts[50] and len(done.at) == 200
    assert all(done.has(m) for m in posts)  # the 50 past the window too
    assert all(done.by(m, 0.0, now + 1) for m in posts[:60])
    newest = post(rig, "dm:101", "and one more")
    assert [i for i, _ in talk.unanswered(conn, "dm:101", None, newest)] == [newest]
    got = count_unanswered(conn, 1, 0.0, now + OWNER_ANSWER_S + 1, window=OWNER_ANSWER_S)
    assert got.unanswered.get("discord_dm", 0) <= 1  # only the newest, never the old stretch
    rig.close()


def test_a_photo_request_answer_covers_only_his_ask(store, tmp_path):
    """Review follow-up F2: a photo-request answer writes its receipt (covers = the ask), so an
    earlier post of his whose answer was dropped stays unanswered; without the receipt the
    old reading would have called it answered."""
    from leeroy_mind.discord.transport import DiscordTarget, store_line
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn, TurnKind
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path)
    talk = talk_for(rig)
    conn = rig.store.conn
    earlier = post(rig, "dm:101", "did the parcel ever turn up")  # its answer was dropped
    ask = post(rig, "dm:101", "send me a selfie")
    turn = Turn(TurnKind.REPLY_HUMAN, 101, "dm:101", to_guid=1, addressee=1)
    meta = TurnMeta("req", why="selfie:request", answers_human=True, photo_request=ask,
                    discord=DiscordTarget(ask, None), covers=(ask,))
    import inspect
    args = inspect.signature(store_line).parameters
    kw = dict(store=rig.store, turn=turn, meta=meta, text="here you go", now=rig.clock.now(),
              audience=frozenset({1, 101}))
    mid = store_line(**{k: v for k, v in kw.items() if k in args})
    assert covered.parse(rig.store.get_meta(covered.BUILT_KEY.format(mid))) == (ask,)
    done = covered.read(conn, "dm:101")
    assert done.has(ask) and not done.has(earlier)
    assert [i for i, _ in talk.unanswered(conn, "dm:101", None, ask)] == [earlier]
    import leeroy_mind.photos.requests as requests
    assert "meta.covers = (latest.message,)" in inspect.getsource(requests)
    rig.close()
