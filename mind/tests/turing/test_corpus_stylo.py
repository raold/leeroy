"""Which lines the judge compares, by family, and part A end to end on invented rows."""
import random

from leeroy_mind.protocol import Chat
from leeroy_mind.turing import corpus, stylo

HUMAN = 1
T0 = 1_800_000_000.0


def chat(store, ts, channel, sender, text, *, to=0, origin=None, name=None):
    origin = origin or ("human" if sender == HUMAN else "mind")
    return store.insert_chat_line(Chat(int(ts * 1000), channel, sender, name or f"M{sender}", to,
                                       text, 0, 0), session_id=None, origin=origin, ts=ts)


def discord(store, ts, channel, author, text, *, origin=None, deleted=None):
    origin = origin or ("human" if author == HUMAN else "mind")
    bot = "signups" if origin == "bot" else None
    with store.transaction():
        return store.conn.execute(
            "INSERT INTO discord_message(channel, author, origin, text, ts, deleted, bot) "
            "VALUES (?,?,?,?,?,?,?)", (channel, author, origin, text, ts, deleted, bot)).lastrowid


def test_families_and_exclusions(store):
    keep = {
        chat(store, T0 + 1, "guild", HUMAN, "anyone up for voa"): ("game", "guild#-", True),
        chat(store, T0 + 2, "guild", 101, "Sure, I'm in."): ("game", "guild#-", False),
        chat(store, T0 + 3, "officer", 102, "bench him"): ("game", "officer#-", False),
        chat(store, T0 + 4, "whisper", HUMAN, "hey", to=101): ("private", "w:101", True),
        chat(store, T0 + 5, "whisper", 101, "hi you", to=HUMAN): ("private", "w:101", False),
    }
    dropped = [
        chat(store, T0 + 6, "whisper", 101, "between members", to=102),
        chat(store, T0 + 7, "voice:tavern", 101, "spoken line"),
        chat(store, T0 + 8, "party", HUMAN, "Recount - Damage Done for Thing"),
        chat(store, T0 + 9, "discord:general", 101, "the mirror of a Discord row"),
        chat(store, T0 + 10, "guild", 0, "server notice", origin="game"),
        chat(store, T0 + 11, "emote", 101, "waves"),
    ]
    dkeep = {
        discord(store, T0 + 12, "dm:101", HUMAN, "you there"): ("private", "dm:101", True),
        discord(store, T0 + 13, "general", 103, "posting in general"): ("discord", "discord:general", False),
        discord(store, T0 + 14, "gdm:4", 104, "group chat line"): ("discord", "gdm:4", False),
    }
    ddropped = [discord(store, T0 + 15, "general", 103, "deleted", deleted=T0 + 16),
                discord(store, T0 + 16, "general", 0, "signups are up", origin="bot"),
                discord(store, T0 + 17, "dm:101", 101, "a voice note")]
    with store.transaction():
        store.conn.execute("INSERT INTO discord_voice(message, origin, ms, bytes, transcript_ok) "
                           "VALUES (?, 'mind', 1000, 10, 1)", (ddropped[-1],))
    lines = corpus.read_lines(store.conn, human=HUMAN, start=T0, end=T0 + 100)
    got = {line.ref: (line.family, line.conv, line.human) for line in lines}
    want = {f"c:{k}": v for k, v in keep.items()} | {f"d:{k}": v for k, v in dkeep.items()}
    assert got == want
    assert not any(f"c:{d}" in got for d in dropped) and not any(f"d:{d}" in got for d in ddropped)
    assert [line.ts for line in lines] == sorted(line.ts for line in lines)


def test_saved_window_refs_read_back_in_order_or_not_at_all(store):
    a = chat(store, T0 + 1, "guild", HUMAN, "one")
    b = discord(store, T0 + 2, "general", 103, "two")
    got = corpus.read_refs(store.conn, [f"d:{b}", f"c:{a}"], human=HUMAN)
    assert [line.text for line in got] == ["two", "one"]
    with store.transaction():
        store.conn.execute("UPDATE discord_message SET deleted=? WHERE id=?", (T0 + 3, b))
    assert corpus.read_refs(store.conn, [f"c:{a}", f"d:{b}"], human=HUMAN) is None
    assert corpus.read_refs(store.conn, ["c:999999"], human=HUMAN) is None


def test_an_older_database_without_the_tables_reads_empty():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    assert corpus.read_lines(conn, human=HUMAN, start=0, end=1) == []
    assert stylo.compute(conn, human=HUMAN, end=1, days=14)["families"]["game"]["status"] == "too_few"


def fill(store, *, his_style, member_style, n=80, seed=0):
    """Whisper stretches of a few lines in the given styles. Each line's speaker is drawn on
    its own and every line comes after the same spread of seconds, so only the styles differ."""
    rng = random.Random(seed)
    ts = T0
    for i in range(n):
        member = 101 + i % 4
        ts += rng.uniform(2000, 4000)  # each stretch its own cross-validation group
        for k in range(rng.randint(2, 4)):
            ts += rng.uniform(5, 60) if k else 0
            his = rng.random() < 0.5
            chat(store, ts, "whisper", HUMAN if his else member,
                 (his_style if his else member_style)(rng), to=member if his else HUMAN)
    return ts + 1


def casual(rng):
    return rng.choice(["im down", "dont care lol", "yeah sure", "thats fine", "omw", "k"])


def test_same_style_reads_close_to_indistinguishable(store):
    end = fill(store, his_style=casual, member_style=casual, n=120)
    r = stylo.compute(store.conn, human=HUMAN, end=end, days=60)["families"]["private"]
    assert r["status"] == "ok" and r["sample"] >= 120
    assert abs(r["auc"] - 0.5) < 0.1
    assert r["low"] <= 0.5 + 0.05 and r["high"] >= 0.5 - 0.05


def test_a_different_style_is_told_apart_with_its_tells_in_words(store):
    def polished(rng):
        return rng.choice(["I'm not sure, honestly. It's late.", "That's lovely. Don't stop.",
                           "I can't wait. You're sweet."])
    end = fill(store, his_style=casual, member_style=polished)
    r = stylo.compute(store.conn, human=HUMAN, end=end, days=60)["families"]["private"]
    assert r["auc"] > 0.95 and r["low"] is not None and r["high"] >= r["auc"]
    words = [stylo.describe(f) for f in r["features"]]
    assert any(w.startswith("contractions with an apostrophe (i'm, don't): him 0%, members 100%")
               for w in words), words
    assert all("Testmage" not in w for w in words)


def test_too_few_lines_of_his_are_not_measured(store):
    for i in range(40):
        chat(store, T0 + i, "guild", 101, "members only")
    chat(store, T0 + 50, "guild", HUMAN, "just me once")
    r = stylo.compute(store.conn, human=HUMAN, end=T0 + 100, days=1)["families"]["game"]
    assert (r["status"], r["human_lines"], r["member_lines"], r["auc"]) == ("too_few", 1, 40, None)


def test_describe_formats_each_kind():
    assert stylo.describe({"key": "words", "human": 5.0, "member": 8.5}) == "words per line: him 5, members 8.5"
    assert stylo.describe({"key": "latency", "human": 40.0, "member": 600.0}) == \
        "seconds after the line before: him 40 s, members 10 min"
    assert stylo.describe({"key": "hour", "human": 21.5, "member": 3.0}) == \
        "hour of day: him around 21:30, members around 03:00"
    assert stylo.describe({"key": "apostrophe", "human": 0.004, "member": 0.31}) == \
        "contractions with an apostrophe (i'm, don't): him <1%, members 31%"
    assert stylo.describe({"key": "nonsense"}) == ""


def test_spoken_whispers_are_not_typed_lines(store):
    typed = chat(store, T0 + 1, "whisper", HUMAN, "typed by hand", to=101)
    heard = chat(store, T0 + 2, "whisper", HUMAN, "Spoken into the mic, then transcribed.", to=101)
    voiced = chat(store, T0 + 3, "whisper", 101, "a turn the voice service spoke", to=HUMAN)
    answer = chat(store, T0 + 4, "whisper", 101, "typed back", to=HUMAN)
    with store.transaction():
        store.conn.execute("INSERT INTO voice_heard(line_id, raw, confidence, speech_ms, latency_ms) "
                           "VALUES (?, 'spoken', 0.9, 900, 120)", (heard,))
        store.conn.execute("INSERT INTO voice_line(speak_id, line_id, room, state, ts) "
                           "VALUES ('run.t1', ?, 'whisper', 'done', ?)", (voiced, T0 + 3))
    refs = [line.ref for line in corpus.read_lines(store.conn, human=HUMAN, start=T0, end=T0 + 10)]
    assert refs == [f"c:{typed}", f"c:{answer}"]
    assert corpus.read_refs(store.conn, [f"c:{typed}", f"c:{heard}"], human=HUMAN) is None
    assert corpus.read_refs(store.conn, [f"c:{voiced}"], human=HUMAN) is None


def test_game_conversations_are_kept_apart_by_session_place_and_group(store):
    one, two = store.start_session(T0), store.start_session(T0 + 50)

    def game(ts, channel, sender, *, session, audience=None, zone=10):
        return store.insert_chat_line(Chat(0, channel, sender, f"M{sender}", 0, "words here", 0, zone),
                                      session_id=session, audience=audience,
                                      origin="human" if sender == HUMAN else "mind", ts=ts)
    rows = {
        game(T0 + 1, "guild", 101, session=one): "guild#1",
        game(T0 + 2, "guild", 101, session=two): "guild#2",
        game(T0 + 3, "say", 101, session=one, zone=10): "say#1#0:10",
        game(T0 + 4, "say", 102, session=one, zone=11): "say#1#0:11",
        game(T0 + 5, "party", HUMAN, session=one): "party#1",                      # his own line
        game(T0 + 6, "party", 101, session=one, audience=[101, 102]): "party#1",   # his group
    }
    hidden = [game(T0 + 7, "party", 103, session=one, audience=[104]),  # not his group
              game(T0 + 8, "raid", 101, session=None, audience=[101]),  # no session: offline raid
              game(T0 + 9, "raid", 102, session=one)]                   # audience unknown
    got = {line.ref: line.conv for line in corpus.read_lines(store.conn, human=HUMAN, start=T0, end=T0 + 20)}
    want = {f"c:{i}": conv.replace("#1", f"#{one}", 1).replace("#2", f"#{two}", 1) for i, conv in rows.items()}
    assert got == want
    assert not any(f"c:{i}" in got for i in hidden)


def test_one_chatty_member_cannot_fill_the_sample():
    rng = random.Random(0)
    speaker_of = [1] * 600 + [2] * 600 + [3 + i // 20 for i in range(600)]  # two chatty, 30 with 20 each
    members = list(range(len(speaker_of)))
    pick = stylo.member_sample(members, speaker_of, 400, rng)
    assert len(pick) == 400 and len(set(pick)) == 400
    from collections import Counter
    shares = Counter(speaker_of[i] for i in pick)
    assert max(shares.values()) <= 20  # 5% of 400
    # few members: an equal share each, and the sample shrinks to what the cap allows
    few = [7] * 300 + [8] * 10 + [9] * 10
    pick = stylo.member_sample(list(range(len(few))), few, 200, random.Random(1))
    counts = Counter(few[i] for i in pick)
    assert counts[8] == counts[9] == 10 and counts[7] == max(counts.values())
    assert counts[7] <= len(pick) / 3 + 1
