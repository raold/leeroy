"""Recall diversity ([talk] recall_diversity, memory/diversity.py): past intentions read as past
and rank lower, a memory shown in a conversation is not shown again there unless his line is
about it, one memory per subject, and a subject she keeps raising to him rests. Every rule runs
after the scope and audience rules, so it can only drop or lower what those let in. Invented
memories and lines for the test cast only."""
import pytest

from conftest import HUMAN, make_mem, run, seed_test_cast
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory import diversity as d
from leeroy_mind.memory.diversity import Diversity
from leeroy_mind.memory.recall import Recall, format_memory
from leeroy_mind.memory.writer import Embedder
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_llm import FakeLLM, fake_embed

DAY = 86400.0
HOUR = 3600.0


def vec(text):
    return pack_vec(fake_embed(text))


@pytest.fixture
def rig(store, clock):
    seed_test_cast(store)
    return Recall(store, Embedder(LLMScheduler(FakeLLM(), clock=clock), "emb-model"), clock,
                  tau_days=5.0)


def add(store, clock, text, *, age=0.0, **kw):
    now = int(clock.now())
    kw.setdefault("owner", 102)
    kw.setdefault("source", "extract")
    kw.setdefault("evidence", ())
    return store.insert_memory(make_mem(text=text, embedding=vec(text), t_event=int(now - age),
                                        t_created=int(now - age), t_accessed=now, **kw))


def mem(clock, text, *, age=0.0, **kw):
    now = int(clock.now())
    kw.setdefault("source", "extract")
    return make_mem(id=1, owner=102, text=text, t_event=int(now - age), t_created=int(now - age),
                    t_accessed=now, **kw)


def ids(rows):
    return [m.id for m in rows]


def recall(r, query, *, diversity=None, channel="whisper", to_guid=HUMAN, k=5, owner=102):
    extra = {"diversity": diversity} if diversity is not None else {}
    return run(r.recall(owner, query, present=[], addressee=None, k=k, touch=False,
                        channel=channel, to_guid=to_guid, **extra))


def said(store, clock, guid, ids_, *, channel="whisper", to_guid=HUMAN, ago=HOUR, dropped=None):
    store.log_utterance(clock.now() - ago, guid, channel, "reply_human", "invented", ids_,
                        dropped, None, to_guid=to_guid)


def line(store, clock, frm, to, text, *, channel="whisper", ago=HOUR):
    store.insert_chat_line(Chat(ts=0, channel=channel, from_guid=frm, from_name="x", to_guid=to,
                                text=text, map=0, zone=0), session_id=None,
                           origin="human" if frm == HUMAN else "mind", ts=clock.now() - ago)


# -- words ------------------------------------------------------------------------------------

def test_words_are_stemmed_content_words_without_names_or_contractions():
    names = d.name_words(["Belra", "Zan Quill"])
    assert d.words("Belra benched me AGAIN for the bridge runs, you've seen it", names) == {
        "bench", "bridg", "run"}
    assert d.words("sitting by the kilns, stopped making jam", names) == {"sit", "kiln", "stop", "jam"}
    assert d.words("zan's lantern", names) == {"lantern"}
    assert d.jaccard(frozenset({"a1", "b1"}), frozenset({"a1", "c1"})) == pytest.approx(1 / 3)
    assert d.jaccard(frozenset(), frozenset({"a1"})) == 0.0


# -- rule 1: time-bound intentions -------------------------------------------------------------

@pytest.mark.parametrize("text,kind,source,nights", [
    ("Finish the quilt border tomorrow so I can show it at the fair.", "plan", "reflection", 2),
    ("Polish the brass lantern tonight.", "plan", "reflection", 1),
    ("I need to fix the gate hinge by Friday.", "conversation", "extract", 7),
    ("We're going to repaint the shed next week.", "reflection", "reflection", 14),
    ("I'll sort the seed packets tonight, then plant them next week.", "reflection", "reflection", 14),
    ("Rain is forecast for tomorrow.", "observation", "extract", None),
    ("Keep the evening routine steady.", "plan", "reflection", None),
    ("I will polish the lantern tonight.", "observation", "owner", None),
    ("Tonight: we cleared the lower halls.", "event", "game", None),
    ("I will polish the lantern tonight.", "story", "reflection", None),
])
def test_horizon_finds_time_bound_intentions(clock, text, kind, source, nights):
    assert d.horizon(mem(clock, text, kind=kind, source=source)) == nights


def test_a_past_intention_reads_as_past_and_the_stored_text_stays(clock):
    tz = clock.local().tzinfo
    plan = "Finish the quilt border tomorrow so I can show it at the fair."
    for age, lead in ((HOUR, ""), (1 * DAY, ""), (2 * DAY, d.PAST_SOON), (5 * DAY, d.PAST_SOON),
                      (9 * DAY, d.PAST_LONG)):
        m = mem(clock, plan, age=age, kind="plan", source="reflection")
        assert d.lead(m, clock.now(), tz) == lead
        assert format_memory(m, clock.now(), tz=tz, aged=True).endswith(f": {lead}{plan}")
        plain = format_memory(m, clock.now(), tz=tz)
        assert plain == format_memory(m, clock.now(), tz=tz, aged=False)
        assert d.PAST_SOON not in plain and d.PAST_LONG not in plain
    old = mem(clock, plan, age=5 * DAY, kind="plan", source="reflection")
    assert format_memory(old, clock.now(), tz=tz, aged=True) == (
        "- five days ago: (I was going to, but that time has passed) " + plan)
    assert old.text == plan


def test_tonight_is_past_the_next_night_and_stale_after_two_days(clock):
    tz = clock.local().tzinfo
    tonight = "Polish the brass lantern tonight."
    assert not d.lead(mem(clock, tonight, age=HOUR, kind="plan"), clock.now(), tz)
    yesterday = mem(clock, tonight, age=DAY, kind="plan")
    assert d.lead(yesterday, clock.now(), tz) and not d.stale(yesterday, clock.now(), tz)
    assert d.stale(mem(clock, tonight, age=3 * DAY, kind="plan"), clock.now(), tz)
    # a private memory keeps its tail after the words
    private = mem(clock, tonight, age=3 * DAY, kind="plan", scope="private", scope_with=HUMAN)
    assert format_memory(private, clock.now(), tz=tz, aged=True).endswith(
        d.PAST_SOON + tonight + " (keep it between you two)")


QUILT = "Finish the quilt border tomorrow so I can show it at the fair."
BOOTH = "The quilt fair had a booth of tin lanterns."


def test_a_stale_intention_ranks_below_a_current_memory(rig, store, clock):
    # equal in every score (a query neither shares a word with); the tie goes to the older id
    plan = add(store, clock, QUILT, age=5 * DAY, kind="plan", source="reflection")
    other = add(store, clock, BOOTH, age=5 * DAY)
    assert ids(recall(rig, "anything new", k=1)) == [plan]
    assert ids(recall(rig, "anything new", k=1, diversity=Diversity(human=HUMAN))) == [other]


def test_an_intention_whose_time_has_not_passed_keeps_its_rank(rig, store, clock):
    plan = add(store, clock, QUILT, age=HOUR, kind="plan", source="reflection")
    add(store, clock, BOOTH, age=HOUR)
    assert ids(recall(rig, "anything new", k=1, diversity=Diversity(human=HUMAN))) == [plan]


# -- rule 2: no repeats within a conversation ---------------------------------------------------

ROPE = "The lighthouse keeper taught me to splice rope."


@pytest.fixture
def four(store, clock):
    return [add(store, clock, t) for t in (
        ROPE, "My cousin keeps bees behind the bakery.",
        "The ferry timetable changed for the winter.", "I found a cracked teapot at the market.")]


def test_a_memory_shown_in_this_conversation_is_left_out(rig, store, clock, four):
    rope = four[0]
    said(store, clock, 102, [rope], ago=HOUR)
    on = Diversity(human=HUMAN)
    assert rope in ids(recall(rig, "what's new"))
    assert rope not in ids(recall(rig, "what's new", diversity=on))
    assert set(ids(recall(rig, "what's new", diversity=on))) == set(four[1:])


def test_his_line_about_it_brings_it_back(rig, store, clock, four):
    rope = four[0]
    said(store, clock, 102, [rope], ago=HOUR)
    assert rope in ids(recall(rig, ROPE, diversity=Diversity(human=HUMAN, his_line=ROPE)))
    # his line on something else, or no line of his (an opener), keeps it out
    other = "how was the market today"
    assert rope not in ids(recall(rig, other, diversity=Diversity(human=HUMAN, his_line=other)))
    assert rope not in ids(recall(rig, ROPE, diversity=Diversity(human=HUMAN)))


def test_the_measured_threshold():
    assert d.REPEAT_COSINE == 0.6 and d.CONVERSATION_S == 3 * HOUR


@pytest.mark.parametrize("logged,turn,kept", [
    # (channel, to_guid, ago, dropped, guid) of the earlier line; (channel, to_guid) of the turn
    (("whisper", HUMAN, 4 * HOUR, None, 102), ("whisper", HUMAN), True),   # outside 3 h
    (("whisper", HUMAN, HOUR, "guard_echo", 102), ("whisper", HUMAN), True),  # never sent
    (("whisper", HUMAN, HOUR, None, 103), ("whisper", HUMAN), True),  # another member's prompt
    (("guild", 0, HOUR, None, 102), ("whisper", HUMAN), True),  # another conversation
    (("whisper", 103, HOUR, None, 102), ("whisper", HUMAN), True),  # her whisper to someone else
    (("dm:102", HUMAN, HOUR, None, 102), ("whisper", HUMAN), False),  # her DM with him: one pair
    (("whisper", HUMAN, HOUR, None, 102), ("dm:102", HUMAN), False),
    (("guild", 0, HOUR, None, 102), ("guild", 0), False),
    (("discord:general", 0, HOUR, None, 102), ("discord:general", 0), False),
    (("discord:general", 0, HOUR, None, 102), ("discord:food", 0), True),
])
def test_what_counts_as_this_conversation(rig, store, clock, four, logged, turn, kept):
    channel, to, ago, dropped, guid = logged
    said(store, clock, guid, [four[0]], channel=channel, to_guid=to, ago=ago, dropped=dropped)
    got = ids(recall(rig, "what's new", channel=turn[0], to_guid=turn[1],
                     diversity=Diversity(human=HUMAN)))
    assert (four[0] in got) is kept


def test_one_memory_per_subject(rig, store, clock):
    twin_a = add(store, clock, "We baked rye bread with caraway seeds at dawn.")
    twin_b = add(store, clock, "We baked rye bread with caraway seeds again at dawn today.")
    lone = add(store, clock, "The ferry timetable changed for the winter.")
    assert d.jaccard(d.words("We baked rye bread with caraway seeds at dawn."),
                     d.words("We baked rye bread with caraway seeds again at dawn today.")) >= 0.5
    assert {twin_a, twin_b} <= set(ids(recall(rig, "rye bread ferry", k=3)))
    got = ids(recall(rig, "rye bread ferry", k=3, diversity=Diversity(human=HUMAN)))
    assert lone in got and len({twin_a, twin_b} & set(got)) == 1


# -- rule 3: subject fatigue --------------------------------------------------------------------

KILN = "My sister's pottery kiln cracked during the glaze firing."
FERRY = "The harbor ferry now leaves at noon on market days."
HERS = ("ugh the kiln cracked again", "still mad about the kiln, the whole glaze batch is ruined",
        "my sister says the kiln can be fixed")


@pytest.fixture
def kiln(store, clock):
    return (add(store, clock, KILN, importance=9), add(store, clock, FERRY, importance=5))


def test_a_subject_she_raised_three_times_rests(rig, store, clock, kiln):
    for i, text in enumerate(HERS):
        line(store, clock, 102, HUMAN, text, ago=(i + 1) * DAY)
    on = Diversity(human=HUMAN, his_line="anything fun today?")
    assert ids(recall(rig, "anything fun today?", k=1)) == [kiln[0]]
    assert ids(recall(rig, "anything fun today?", k=1, diversity=on)) == [kiln[1]]
    # her DM with him counts as her lines to him too
    assert ids(recall(rig, "anything fun today?", k=1, channel="dm:102", diversity=on)) == [kiln[1]]


def test_not_when_he_raises_it(rig, store, clock, kiln):
    for i, text in enumerate(HERS):
        line(store, clock, 102, HUMAN, text, ago=(i + 1) * DAY)
    asked = "how's the kiln holding up?"
    assert ids(recall(rig, asked, k=1, diversity=Diversity(human=HUMAN, his_line=asked))) == [kiln[0]]
    # or did, in this conversation: his whisper an hour ago
    line(store, clock, HUMAN, 102, "is the kiln fixed yet", ago=HOUR)
    assert ids(recall(rig, "anything fun today?", k=1,
                      diversity=Diversity(human=HUMAN, his_line="anything fun today?"))) == [kiln[0]]


@pytest.mark.parametrize("case", ["two_lines", "old_lines", "guild_turn", "other_member_lines",
                                  "lines_to_someone_else"])
def test_fatigue_needs_three_of_her_recent_lines_to_him_in_a_turn_with_him(rig, store, clock,
                                                                          kiln, case):
    ago = 8 * DAY if case == "old_lines" else DAY
    for i, text in enumerate(HERS[:2] if case == "two_lines" else HERS):
        frm = 103 if case == "other_member_lines" else 102
        to = 104 if case == "lines_to_someone_else" else HUMAN
        line(store, clock, frm, to, text, ago=ago + i * HOUR)
    channel, to_guid = ("guild", 0) if case == "guild_turn" else ("whisper", HUMAN)
    assert ids(recall(rig, "anything fun today?", k=1, channel=channel, to_guid=to_guid,
                      diversity=Diversity(human=HUMAN))) == [kiln[0]]


# -- audiences ----------------------------------------------------------------------------------

TURNS = [("guild", 0), ("whisper", HUMAN), ("whisper", 103), ("dm:102", HUMAN), ("dm:103", HUMAN),
         ("officer", 0), ("discord:general", 0), ("gdm:5", 0), ("party", 0)]


def test_no_memory_crosses_an_audience_because_of_diversity(rig, store, clock):
    """Diversity on picks from the very candidates the scope and audience rules let into each
    turn with it off, however her lines, prompts and his lines read."""
    add(store, clock, "Zan told me he is restoring an old sailboat.", scope="private",
        scope_with=HUMAN, actors=(HUMAN,))
    add(store, clock, "Arkon and I planned the officers' bank sorting.", scope="private",
        scope_with=-1)
    add(store, clock, "Polish the brass lantern tonight.", kind="plan", source="reflection",
        age=4 * DAY)
    add(store, clock, KILN)
    for t in ("sailboat", "bank sorting officers", "kiln glaze sister"):
        add(store, clock, t + " in the open")
    for i, text in enumerate(HERS):
        line(store, clock, 102, HUMAN, text + " sailboat", ago=(i + 1) * HOUR)
    on = Diversity(human=HUMAN, his_line="sailboat kiln bank")
    for channel, to in TURNS:
        allowed = set(ids(recall(rig, "sailboat kiln bank", channel=channel, to_guid=to, k=50)))
        got = set(ids(recall(rig, "sailboat kiln bank", channel=channel, to_guid=to, k=50,
                             diversity=on)))
        assert got <= allowed, (channel, to)
    private = store.conn.execute("SELECT id FROM memory WHERE text LIKE 'Zan told me%'").fetchone()[0]
    assert private not in ids(recall(rig, "sailboat", channel="guild", to_guid=0, k=50,
                                     diversity=on))
    assert private in ids(recall(rig, "sailboat", channel="whisper", to_guid=HUMAN, k=50,
                                 diversity=on))


def test_without_a_channel_only_past_intentions_are_lowered(rig, store, clock):
    add(store, clock, QUILT, age=5 * DAY, kind="plan", source="reflection")
    other = add(store, clock, BOOTH, age=5 * DAY)
    said(store, clock, 102, [other], ago=HOUR)  # no conversation without a turn's channel
    got = run(rig.recall(102, "anything new", present=[], addressee=None, k=1, touch=False,
                         diversity=Diversity(human=HUMAN)))
    assert ids(got) == [other]


def test_with_romance_realism_too_the_mark_leads_and_its_time_words_read_as_past(clock):
    tz = clock.local().tzinfo
    m = mem(clock, QUILT, age=5 * DAY, kind="plan", source="reflection")
    assert format_memory(m, clock.now(), tz=tz, past=True, aged=True) == (
        "- five days ago: " + d.PAST_SOON
        + "Finish the quilt border the next day so I can show it at the fair.")
