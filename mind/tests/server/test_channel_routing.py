"""server/topics.py: a conversation's channel from its subject, and each channel's daily room
([server] channel_topics). Invented seeds only."""
from leeroy_mind.server import topics
from leeroy_mind.server.seeds import Seed


def seed(kind, channels, fact="", key=None, **kw):
    return Seed(key or f"{kind}:1", kind, tuple(channels), fact, **kw)


def test_a_life_beat_goes_where_its_words_take_it():
    food = topics.route(seed("life", ("irl", "general", "food"), "made lasagna for the kids", about=(101,)))
    assert food.channels == ("food", "irl", "general")
    work = topics.route(seed("life", ("irl", "general", "food"), "long day at the office"))
    assert work.channels == ("irl", "general")  # never #food without food
    dog = topics.route(seed("life", ("irl", "general", "food"), "the dog chewed my shoes"))
    assert dog.channels == ("pets", "irl", "general")
    # her day is never a WoW channel's, whatever its words
    alt = topics.route(seed("life", ("irl", "general", "food"), "levelling my alt after work"))
    assert alt.channels == ("irl", "general")


def test_a_plan_goes_to_its_game_channel_or_general():
    gold = topics.route(seed("plan", ("general", "lfg-and-alts", "professions-and-gold"),
                             "farming saronite tomorrow for the auction house"))
    assert gold.channels == ("professions-and-gold", "general")
    plain = topics.route(seed("plan", ("general", "lfg-and-alts", "professions-and-gold"),
                              "gonna try the new raid strat on saturday"))
    assert plain.channels == ("general",)  # no #lfg-and-alts or #professions-and-gold by default
    life = topics.route(seed("plan", ("general", "lfg-and-alts", "professions-and-gold"),
                             "dentist appointment on monday"))
    assert life.channels == ("irl", "general")


def test_the_weekend_never_opens_in_food_without_food():
    assert topics.route(seed("weekend", ("irl", "food", "general"), "It's the weekend")).channels \
        == ("irl", "general")


def test_only_memes_open_in_memes():
    joke = topics.route(seed("joke", ("general", "memes"), "the running joke"))
    assert joke.channels == ("general",)
    for kind in ("meme", "meme_image"):
        assert topics.route(seed(kind, ("memes",), "a meme")).channels == ("memes",)


def test_purpose_seeds_skip_channels_whose_talk_rests_on_a_post():
    for cid in ("memes", "pics", "logs", "applications", "bank-requests"):
        assert topics.route(seed("purpose", (cid,), "topic", key=f"purpose:{cid}:d")) is None, cid
    for cid in ("general", "food", "irl", "pets", "strategy", "mage", "officer-chat"):
        assert topics.route(seed("purpose", (cid,), "topic", key=f"purpose:{cid}:d")).channels \
            == (cid,)


def test_officers_business_and_its_own_seeds():
    attendance = topics.route(seed("officer", ("officer-chat", "applications"), "attendance",
                                   key="officer:attendance:2026-W47", officers_only=True))
    assert attendance.channels == ("officer-chat",)
    trial = topics.route(seed("officer", ("applications", "officer-chat"), "trial",
                              key="officer:trial:101:2026-W47", officers_only=True))
    assert trial.channels == ("applications", "officer-chat")
    raid = topics.route(seed("raid_after", ("logs", "loot-and-dkp", "strategy", "general"), "x"))
    assert raid.channels == ("logs", "loot-and-dkp", "strategy", "general")
    # the guild's own business goes where its module put it, even #memes
    for kind in sorted(topics.EXEMPT):
        s = seed(kind, ("memes",), "x")
        assert topics.route(s) is s


def test_a_public_seed_with_nowhere_left_falls_back_to_general():
    assert topics.route(seed("rival", ("pics",), "a rival guild")).channels == ("general",)
    # an officers' one never does
    assert topics.route(seed("officer", ("pics",), "x", officers_only=True)) is None


def test_group_dm_seeds_are_untouched():
    s = seed("life", ("gdm:3",), "made lasagna")
    assert topics.route(s) is s


def test_daily_room():
    assert topics.daily("general") == topics.DAILY["general"]
    assert topics.daily("mage") == topics.CLASS_DAILY
    assert topics.daily("lfg-and-alts") == topics.DEFAULT_DAILY
    assert topics.room("memes", {"memes": 3}) == topics.DAILY["memes"] - 3
    assert topics.room("memes", {"memes": 99}) == 0
    assert topics.room("gdm:4", {"gdm:4": 99}) > 1000  # a group DM has its own limits
    assert topics.room("dm:101", {}) > 1000


def test_routed_keeps_only_channels_with_room():
    posted = {"food": topics.DAILY["food"], "irl": topics.DAILY["irl"] - 2}
    out = topics.routed([seed("life", ("irl", "general", "food"), "pasta for dinner")], posted)
    assert out[0].channels == ("general",)  # #food full, #irl short of MIN_ROOM
    full = {"memes": topics.DAILY["memes"]}
    assert topics.routed([seed("meme", ("memes",), "a meme")], full) == []
    # the guild's own business is never held back
    intent = seed("intent", ("memes",), "his ask")
    assert topics.routed([intent], full) == [intent]
    bank = seed("bank_request", ("bank-requests",), "x", officers_only=True)
    assert topics.routed([bank], {"bank-requests": 99}) == [bank]


def test_posted_today_counts_members_posts_since_midnight(store):
    from leeroy_mind.discord import store as dstore
    for channel, origin, ts in (("food", "mind", 10.0), ("food", "mind", 200.0),
                                ("food", "human", 200.0), ("irl", "mind", 300.0)):
        with store.transaction():
            dstore.post(store, channel=channel, author=101, origin=origin, text="an invented line",
                        now=ts)
    assert topics.posted_today(store.conn, 100.0) == {"food": 1, "irl": 1}
