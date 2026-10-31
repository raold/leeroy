"""Extraction records what happened and never canonises a claim (fix wave G2 item 1; dress
rehearsal 2, judge A fix 4, judge B fix 4). In the rehearsal 13 talk memories were written: 7
were members' small talk and 5 a member's own line word for word, which came straight back to
them as "You remember: five minutes ago: <own line>"; one rewrite stored a member's mistake as
the guild's truth, and every one sat at confidence 1.0. The threads below mirror the
rehearsal's, with invented names; each `mNNN` is the reply item behind memory #NNN."""
import json
from datetime import datetime, timezone

from conftest import run, seed_test_cast
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.extract import (CLAIM_CONFIDENCE, EXTRACT_CONFIDENCE, EXTRACTION_SCHEMA,
                                        Extractor, ThreadFacts, build_extraction_messages,
                                        validate_extraction)
from leeroy_mind.memory.threads import ClosedThread
from leeroy_mind.memory.writer import Embedder, MemoryWriter
from leeroy_mind.protocol import Chat, Member
from leeroy_mind.storage.rows import ChatLineRow, MemoryRow
from leeroy_mind.testing.fake_llm import FakeLLM, script_from

# Invented identity: the real name lives only in the owner's local mind.toml.
HUMAN = HumanIdentity(1, "Testmage", "Testy", "Quillon", True, pronouns="he")
NAMES = {1: "Testy", 101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna", 105: "Ebbo"}
T0 = datetime(2026, 10, 29, 21, 0, tzinfo=timezone.utc).timestamp()  # night 2026-10-29
FACTS = ThreadFacts(birthdays={g: "03-14" for g in range(101, 106)}, tz=timezone.utc)


def thread(*spec) -> list[ChatLineRow]:
    """Lines L1, L2, ...: (from_guid, text) on guild chat, (from_guid, text, to_guid) a
    whisper."""
    out = []
    for i, (frm, text, *to) in enumerate(spec, 1):
        out.append(ChatLineRow(
            id=i, ts=T0 + 10 * i, channel="whisper" if to else "guild", from_guid=frm,
            from_name="Testmage" if frm == 1 else NAMES[frm], to_guid=to[0] if to else 0,
            text=text, map=0, zone=0, session_id=None, thread_id=None, origin="game"))
    return out


def memory(speaker, text, evidence, importance=5, *, about=(), what=None, disagrees=None,
           nudges=None):
    m = {"speaker": speaker, "text": text, "importance": importance, "valence": 0.3,
         "about": list(about), "evidence": list(evidence)}
    if nudges is not None:
        m["nudges"] = nudges
    if what is not None:
        m["what"] = what
    if disagrees is not None:
        m["disagrees"] = disagrees
    return m


def kept(lines, *memories, facts=FACTS):
    return validate_extraction({"memories": list(memories), "overheard": None}, lines, NAMES, 1,
                               human=HUMAN, facts=facts).memories


# -- the rehearsal's threads ------------------------------------------------------------------

A = thread((1, "where do the fishing dailies start? never bothered with them"),
           (101, "the docks! ask the old harbormaster there"),
           (101, "and you're set"))
B = thread((1, "wearing the blue cloak tonight? docks after raid, only us", 102),
           (102, "oh? the blue one it is ;)", 1),
           (102, "be there", 1))
C = thread((1, "when do i get my portrait? i'll even hold still", 103),
           (103, "hold still? you? you'll look great though", 1))
D = thread((104, "wait hold on"),
           (104, "happy birthday ebbo! /hug /hug"),
           (103, "Happy birthday Ebbo! I baked you a cake."),
           (102, "that boat's worth the grind. once it's yours, come see me lol"))
E = thread((1, "got time for the dailies with me tomorrow?", 104),
           (104, "sure. what time?", 1))
F = thread((101, "ebbo gave the boat a birthday speech and we all lived lmao"),
           (104, "happy birthday ebbo"),
           (102, "happy birthday ebbo! wish i could bake you a cake :)"),
           (103, "baking? brave."))
G = thread((1, "arkon do you still keep the flask bank on tuesdays, or did belra take it "
               "over?"),
           (101, "still mine. tuesdays go to my late shift though"),
           (102, "lol no. he has tuesdays, i do the restocking, and he keeps forgetting the mana "
                 "ones."),
           (103, "this flask bank fight has gone on forever and nobody's ahead xd"),
           (104, "any day of the week works for me, testy. the docks, later?"),
           (104, "i wrote you a poem and left out all the boring verses."))
H = thread((105, "whenever i pull threat my lag shows up"),
           (101, "your lag is older than the first expansion, man"),
           (102, "you pulled half the dungeon once, by accident"))
I = thread((102, "coros you and your poems again ;)"),
           (104, "says the one who sings in raid"),
           (103, "i swear only dunna would read testy a poem in raid chat"),
           (105, "i swear only dunna would read testy a poem in raid chat"))

# What the model gave (the rehearsal's prompt had no `what` and no `disagrees`).
m421 = memory("Arkon", "I told Testy the fishing dailies start at the docks with the old "
              "harbormaster.", ["L2", "L3"], 2, about=["Testy"])
m422 = memory("Belra", "Testy asked me to meet him by the docks after raid tonight in my blue "
              "cloak.", ["L1", "L2", "L3"], 5, about=["Testy"])
m423 = memory("Coros", "Testy keeps asking when he gets his portrait and says he'll hold still "
              "for me.", ["L1", "L2"], 3, about=["Testy"])
m424 = memory("Coros", "Happy birthday Ebbo! I baked you a cake.", ["L3"], 3,
              about=["Ebbo"])
m425 = memory("Belra", "That boat's worth the grind. Once it's yours, come see me lol", ["L4"],
              3, about=["Ebbo"])
m426 = memory("Dunna", "Testy asked if I could do the dailies with him tomorrow and I said sure.",
              ["L1", "L2"], 3, about=["Testy"])
m427 = memory("Arkon", "Ebbo gave the boat a birthday speech and we all lived lmao",
              ["L1"], 3, about=["ebbo"])
m428 = memory("Arkon", "I still keep the flask bank on Tuesdays; the late shift has it on "
              "Fridays.", ["L2"], 3, about=["Testy"])
m429 = memory("Belra", "Arkon has Tuesdays, I do the restocking, and he keeps forgetting the mana "
              "ones.", ["L3"], 3, about=["Arkon"])
m430 = memory("Dunna", "I wrote Testy a poem and left out all the boring verses.", ["L5", "L6"],
              3, about=["Testy"])
m431 = memory("Ebbo", "Arkon said my lag is older than the first expansion.", ["L2"], 3,
              about=["Arkon"])
m432 = memory("Coros", "I swear only Dunna would read Testy a poem in raid chat.", ["L3"], 3,
              about=["Dunna"])
m433 = memory("Ebbo", "I swear only Dunna would read Testy a poem in raid chat.", ["L4"], 3,
              about=["Dunna"])
REHEARSAL = [(A, [m421]), (B, [m422]), (C, [m423]), (D, [m424, m425]), (E, [m426]),
             (F, [m427]), (G, [m428, m429, m430]), (H, [m431]), (I, [m432, m433])]
# What the model is now asked for on each of them.
WHAT = {id(m421): "event", id(m422): "guild_master", id(m423): "guild_master",
        id(m424): "event", id(m425): "event", id(m426): "promise", id(m427): "event",
        id(m428): "claim", id(m429): "claim", id(m430): "plan", id(m431): "claim",
        id(m432): "event", id(m433): "event"}


def replay(*, labelled: bool) -> list:
    out = []
    for lines, items in REHEARSAL:
        if labelled:
            items = [dict(m, what=WHAT[id(m)], disagrees=False) for m in items]
        out += kept(lines, *items)
    return out


def test_the_rehearsals_13_talk_memories_keep_only_what_happened():
    """His invitation and the help she promised stay; the 7 small-talk memories, the 5 copies
    of a member's own line, the near copies, the member's mistake rewritten as a fact and "he
    keeps asking" after one ask are gone. So is her public answer to his question, which the
    model rated 2 (fix wave G3: naming him no longer lifts it over the small-talk floor)."""
    got = replay(labelled=True)
    assert [m.text for m in got] == [m422["text"], m426["text"]]
    assert {m.confidence for m in got} == {EXTRACT_CONFIDENCE}
    assert all(m.importance >= 5 and 1 in m.actors for m in got)


def test_the_rehearsals_own_replies_keep_only_his_words_and_never_at_full_confidence():
    """The rehearsal's replies as they came back, with no `what`: anything unlabelled is read
    as a claim, so only what is said as someone else's (here his words) stays, as hearsay."""
    got = replay(labelled=False)
    assert [m.text for m in got] == [m422["text"], m426["text"]]
    assert {m.confidence for m in got} == {CLAIM_CONFIDENCE}


# -- the rules one at a time ------------------------------------------------------------------

def test_a_members_small_talk_is_not_stored():
    """Importance under 4 is small talk (7 of the 13): gone, whatever the model calls it. The
    model's own rating decides it, so a member's banter is small talk even when it names him;
    only what he took part in (a line of his, a whisper between them) is worth 5 at least."""
    talk = memory("Arkon", "I joked with Ebbo that he has been lagging for years.", ["L2"], 3,
                  what="event")
    assert kept(H, talk) == []
    assert [m.importance for m in kept(H, dict(talk, importance=4))] == [4]
    assert kept(A, dict(m421, what="event")) == []  # her public answer to him, rated 2
    assert [m.importance for m in kept(A, dict(m421, what="event", importance=4))] == [5]
    assert [m.importance for m in kept(E, dict(m426, what="promise"))] == [5]


def test_her_banter_naming_him_is_small_talk_and_her_whispered_promise_is_not():
    """Rehearsal 3, check 8: four memories of members' own lines that named him ("lucky i like
    you, testy", a loot cheer at him, a joke at a guildmate) were kept, because any memory with
    him in it was raised to importance 5 before the small-talk floor. The model rated each 3,
    as it rated her whispered promise to help him tomorrow, which must stay."""
    banter = thread((102, "my bus broke down twice tonight"),
                    (102, "walked the last mile in the rain, lucky i like you testy"),
                    (103, "that sapphire ring has my name on it testy"),
                    (103, "dibs after the raid ;)"))
    shift = memory("Belra", "The bus let me down twice tonight and I got soaked on the way. "
                   "Lucky I like Testy.", ["L1", "L2"], 3, what="event")
    ring = memory("Coros", "I called dibs on the sapphire ring with Testy for after the raid.",
                  ["L3", "L4"], 3, what="plan")
    assert kept(banter, shift) == kept(banter, ring) == []
    asked = thread((1, "can you help me with the fishing dailies tomorrow?", 104),
                   (104, "yeah i can. what time?", 1))
    promise = memory("Dunna", "Testy asked if I could help with the fishing dailies tomorrow and "
                     "I said yes.", ["L2"], 3, what="promise")
    assert [(m.text, m.importance) for m in kept(asked, promise)] == [(promise["text"], 5)]


def test_a_members_own_line_is_never_stored_verbatim():
    """Word for word (the rehearsal's 5), a near copy (most of the same words, or six of them
    in a row) and a short line said back as most of the memory all go, even with him in it; a
    memory in her own words stays, and so does his question with her short answer in it."""
    for lines, m in ((D, m424), (F, m427), (I, m432), (I, m433), (G, m429), (G, m430)):
        assert kept(lines, dict(m, importance=6, what="event"), facts=None) == [], m["text"]
    short = thread((1, "need a hand at the docks?", 101), (101, "sure. what time?", 1))
    assert kept(short, memory("Arkon", "I said sure. What time?", ["L2"], what="promise")) == []
    own = memory("Arkon", "I promised Testy I'd help at the docks.", ["L1", "L2"], what="promise")
    assert [m.text for m in kept(short, own)] == [own["text"]]
    quoted = memory("Arkon", "Testy asked if I'd give him a hand at the docks tomorrow and I said "
                    "sure, what time?", ["L1", "L2"], what="guild_master")
    assert [m.text for m in kept(short, quoted)] == [quoted["text"]]


def test_a_copy_of_her_line_is_caught_through_its_typos():
    """Rehearsal 3, check 8: a memory said her line back nearly word for word, but the stored
    line's typo ("dedicaetd") broke the six-word run (5 against the line as sent, 10 against
    the model's own words). The copy rule reads the model's words before styling as well."""
    lines = thread((104, "i've got a dedicaetd bag slot for my alts, testy. it's in the contract."))
    words = {1: "i've got a dedicated bag slot for my alts, testy. it's in the contract."}
    copy = memory("Dunna", "I've got a dedicated bag slot for my alts in my contract.", ["L1"], 5,
                  what="plan")
    assert [m.text for m in kept(lines, copy)] == [copy["text"]]  # the line as sent only
    assert validate_extraction({"memories": [copy], "overheard": None}, lines, NAMES, 1,
                               human=HUMAN, facts=FACTS, said=words).memories == []
    told = memory("Dunna", "I told Testy I keep a whole bag slot for my alts.", ["L1"], 5,
                  what="plan")
    assert [m.text for m in validate_extraction(
        {"memories": [told], "overheard": None}, lines, NAMES, 1, human=HUMAN, facts=FACTS,
        said=words).memories] == [told["text"]]


def test_the_extractor_reads_the_models_words_for_its_lines(store, clock):
    """The talk engine's words for each line it said (TalkEngine.said_words) reach the copy
    rule; a line it has no words for is checked as sent."""
    sent = "i've got a dedicaetd bag slot for my alts, testy. it's in the contract."
    asked = []
    for words, stored in (("i've got a dedicated bag slot for my alts, testy. it's in the "
                           "contract.", 0), (None, 1)):
        t = _store_thread(store, thread((104, sent), (103, "lol of course you do")))
        copy = memory("Dunna", "I've got a dedicated bag slot for my alts in my contract.",
                      [f"L{t.line_ids[0]}"], 5, what="plan", disagrees=False)
        ex, _ = _rig(store, clock, [json.dumps({"memories": [copy], "overheard": None})])
        ex._said = lambda line_id, w=words: asked.append(line_id) or w
        before = len(store.memories_with_actors(104, [104]))
        run(ex.extract(t))
        assert len(store.memories_with_actors(104, [104])) - before == stored, words
        assert t.line_ids[0] in asked


def test_a_claim_is_kept_only_as_someone_elses_and_as_hearsay():
    """What someone said about how things stand is kept in another speaker's memory, said as
    theirs, below full confidence (recall shows it as heard); never as a bare fact and never
    as the claimer's own memory."""
    theirs = memory("Belra", "Arkon told me the late shift gets Tuesdays.", ["L2", "L3"],
                    what="claim")
    got = kept(G, theirs)
    assert [m.text for m in got] == [theirs["text"]] and got[0].confidence == CLAIM_CONFIDENCE
    bare = memory("Belra", "My restocking is on Fridays and Arkon has Tuesdays.", ["L3"],
                  what="claim")
    own = memory("Arkon", "I told Testy Tuesdays go to the late shift.", ["L2"],
                  what="claim")
    unlabelled = dict(m428, importance=5)
    assert kept(G, bare) == kept(G, own) == kept(G, unlabelled) == []


def test_his_words_need_his_line_or_they_are_a_claim():
    said = memory("Arkon", "Testy told me the raid starts at eight tomorrow.", ["L2"],
                  what="guild_master")
    assert [m.confidence for m in kept(G, said)] == [CLAIM_CONFIDENCE]
    asked = memory("Arkon", "Testy asked me about the flask bank and I said it's still mine.",
                   ["L1", "L2"], what="guild_master")
    assert [m.confidence for m in kept(G, asked)] == [EXTRACT_CONFIDENCE]


def test_a_memory_that_disagrees_with_what_the_guild_knows_is_dropped():
    m = memory("Belra", "Arkon told me the late shift gets Tuesdays.", ["L2", "L3"],
               what="claim")
    assert kept(G, dict(m, disagrees=True)) == []
    assert len(kept(G, dict(m, disagrees=False))) == 1


def test_a_birthday_the_cards_disagree_with_is_dropped():
    """The rehearsal's birthday nobody had: kept only on the night the card gives, or when it
    names the card's month."""
    wish = memory("Coros", "I wished Ebbo a happy birthday and promised him a cake.", ["L3"],
                  what="event")
    assert kept(D, wish) == []
    tonight = ThreadFacts(birthdays={105: "10-29"}, tz=timezone.utc)
    assert len(kept(D, wish, facts=tonight)) == 1
    assert len(kept(D, wish, facts=None)) == 1  # nothing to check it against
    asked = thread((1, "when's your birthday coros?"), (103, "march! the 14th"))
    right = memory("Coros", "Testy asked when my birthday is and I told him March 14.",
                   ["L1", "L2"], what="guild_master")
    wrong = dict(right, text="Testy asked when my birthday is and I told him in October.")
    mine = memory("Coros", "I told Testy it's my birthday tonight.", ["L1", "L2"],
                  what="event")
    assert [m.text for m in kept(asked, right)] == [right["text"]]
    assert kept(asked, wrong) == kept(asked, mine) == []


def test_who_a_memory_is_with_comes_from_the_lines_not_the_topic():
    """About follows whom the lines were said to (a whisper's other side, a name she
    addresses, a line that named her, the speaker of a line she heard), never whom the talk
    was about: the rehearsal's flirt at him became a memory with the member whose birthday it
    was."""
    flirt = memory("Belra", "I told Testy to come see me once the boat is his.", ["L4"],
                   about=["Ebbo"], what="event")
    assert kept(D, flirt)[0].actors == (102,)
    talk = thread((101, "ebbo's speech went on forever lmao"),
                  (101, "ebbo, you owe me a flask"))
    assert kept(talk, memory("Arkon", "I joked about the speech we all survived.", ["L1"],
                             about=["Ebbo"], what="event"))[0].actors == (101,)
    assert kept(talk, memory("Arkon", "I told Ebbo he owes me a flask.", ["L2"],
                             what="event"))[0].actors == (101, 105)
    assert kept(B, dict(m422, what="guild_master", about=[]))[0].actors == (102, 1)
    answer = memory("Arkon", "I told Testy I still keep the flask bank.", ["L2"], what="event")
    assert kept(G, answer)[0].actors == (101, 1)  # his line named him: it answers it
    assert kept(H, memory("Ebbo", "Arkon teased me about my lag, and I want a rematch.",
                          ["L2"], what="event"))[0].actors == (105, 101)


def test_romance_at_a_member_is_screened_with_whom_it_is_with():
    lines = thread((105, "belra here's a cake for you"),
                   (102, "ebbo you're sweet, i have a crush on you"))
    crush = memory("Belra", "I told him I have a crush on him after the cake.", ["L2"],
                   what="event")
    assert kept(lines, crush) == []  # the model named nobody; her line names Ebbo
    at_him = thread((1, "belra here's a cake for you"),
                    (102, "testy you're sweet, i have a crush on you"))
    assert len(kept(at_him, dict(crush, text="I told Testy I have a crush on him."))) == 1


# -- the prompt -------------------------------------------------------------------------------

def test_the_prompt_asks_for_records_not_facts():
    system = build_extraction_messages(G, NAMES, 1, human=HUMAN)[0]["content"]
    for words in ("in your own words: never copy a line",
                  "guild_master for something the guild master said or asked",
                  "plan or promise", "event for something that happened",
                  "is a claim: note it only in another speaker's memory and as theirs",
                  "never as a fact", "disagrees is true",
                  "3 or less is small talk and banter, which is not kept",
                  "about lists whom the speaker talked with", "not whom the talk was about",
                  'no "always" or "keeps"'):
        assert words in system, words
    item = EXTRACTION_SCHEMA["properties"]["memories"]["items"]
    assert item["properties"]["what"]["enum"] == ["guild_master", "plan", "promise", "event",
                                                  "claim"]
    assert item["properties"]["disagrees"]["type"] == "boolean"
    assert {"what", "disagrees"} <= set(item["required"])


def test_the_prompt_shows_what_the_guild_already_knows():
    msgs = build_extraction_messages(G, NAMES, 1, human=HUMAN,
                                     known=["Tonight is October 29.", "Arkon remembers: x."])
    user = msgs[1]["content"]
    assert user.startswith("What the guild already knows:\n- Tonight is October 29.\n"
                           "- Arkon remembers: x.\n\nConversation:\n[L1] Testy:")
    assert build_extraction_messages(G, NAMES, 1, human=HUMAN)[1]["content"].startswith(
        "Conversation:\n")


# -- the extractor: what the guild already knows, and what is stored ---------------------------

def _rig(store, clock, replies):
    store.upsert_cast([Member(guid=1, name="Testmage", race=1, class_=8, gender=0, level=83,
                              rank=0, online=True, map=0, zone=0, spec_tab=-1)], 1000.0)
    seed_test_cast(store)
    llm = FakeLLM(script_from(replies))
    sched = LLMScheduler(llm, clock=clock)
    writer = MemoryWriter(store, Embedder(sched, "emb"), clock, human=lambda: HUMAN)
    ex = Extractor(store, sched, writer, "m", human_guid=lambda: 1, bystanders=lambda t, s: [],
                   human=lambda: HUMAN)
    return ex, llm


def _store_thread(store, lines) -> ClosedThread:
    ids = [store.insert_chat_line(Chat(ts=0, channel=ln.channel, from_guid=ln.from_guid,
                                       from_name=ln.from_name, to_guid=ln.to_guid, text=ln.text,
                                       map=0, zone=0), session_id=None, origin="game")
           for ln in lines]
    return ClosedThread(store.open_thread("guild", 0.0), "guild", tuple(ids), "silence")


def _remember(store, owner, text, evidence, *, source="owner", key=None) -> int:
    return store.insert_memory(MemoryRow(
        id=None, owner=owner, kind="story", text=text, t_event=1000, t_created=1000,
        t_accessed=1000, access_n=0, actors=(1,), place=None, boss_id=None, raid_id=None,
        importance=6, valence=0.0, source=source, evidence=tuple(evidence), key=key))


def test_the_extractor_shows_the_cards_and_the_guilds_memories_on_the_subject(store, clock):
    """Birthday and job of the members in the thread, and their memories that share its words:
    the owner's history first, never a memory drawn from whispers."""
    ex, llm = _rig(store, clock, [json.dumps({"memories": [], "overheard": None})])
    _remember(store, 101, "Belra and I split the flask bank: Tuesdays are mine, she restocks.",
              ["owner:M1"], key="owner:M1:101")
    _remember(store, 102, "The restocking is my job, and I do it well.", ["owner:M1"],
              key="owner:M1:102")
    whisper = store.insert_chat_line(Chat(ts=0, channel="whisper", from_guid=1,
                                          from_name="Testmage", to_guid=101,
                                          text="the flask bank, keep it quiet", map=0, zone=0),
                                     session_id=None, origin="human")
    _remember(store, 101, "Testy whispered me the flask bank is his secret on Tuesdays.",
              [f"chat:{whisper}"], source="extract")
    _remember(store, 103, "Nothing to do with any of this.", ["owner:M2"], key="owner:M2:103")
    t = _store_thread(store, G)
    assert run(ex.extract(t)).status == "extracted"
    user = llm.calls[0]["messages"][1]["content"]
    known = user.split("\n\nConversation:\n")[0]
    assert known.startswith("What the guild already knows:\n- Tonight is October 29.\n")
    assert "- Arkon: birthday March 14; job: warehouse supervisor." in known
    assert "- Arkon remembers: Belra and I split the flask bank" in known
    assert "- Belra remembers: The restocking is my job" in known
    assert "secret" not in known and "Nothing to do" not in known
    assert "Ebbo:" not in known  # not in the thread


def test_extracted_memories_are_never_stored_at_full_confidence(store, clock):
    reply = json.dumps({"memories": [
        memory("Belra", "Testy asked me to come by the docks after raid tonight.",
               ["L1", "L2"], what="guild_master", disagrees=False),
        memory("Coros", "Belra told me Testy asked her to the docks after raid.", ["L2", "L3"],
               what="claim", disagrees=False)],
        "overheard": {"text": "I saw Belra and Testy making plans for after raid.",
                      "importance": 6}})
    ex, _ = _rig(store, clock, [reply])
    ex._bystanders = lambda t, s: [104]
    lines = thread((1, "come by the docks after raid?"), (102, "be there"),
                   (103, "ooh, the docks"))
    run(ex.extract(_store_thread(store, lines)))
    got = {m.owner: m.confidence
           for g in (102, 103, 104) for m in store.memories_with_actors(g, [1, 102])}
    assert got == {102: EXTRACT_CONFIDENCE, 103: CLAIM_CONFIDENCE, 104: EXTRACT_CONFIDENCE}


# -- fix round 1 (the task review of G2) ------------------------------------------------------

def validated(lines, *memories, overheard=None, facts=FACTS):
    return validate_extraction({"memories": list(memories), "overheard": overheard}, lines,
                               NAMES, 1, human=HUMAN, facts=facts)


def test_a_bare_statement_on_her_own_lines_is_hearsay_whatever_its_label():
    """Review Important 2: #428's shape, a standing fact in the first person on her own line and
    labelled event, was stored at 0.9. Resting only on her own lines with no "I told / said /
    promised / asked" frame, it is hearsay whatever `what` says."""
    bare = dict(m428, importance=5, what="event", disagrees=False)
    assert [(m.confidence, m.importance) for m in kept(G, bare)] == [(CLAIM_CONFIDENCE, 5)]
    framed = memory("Arkon", "I told Testy I still keep the flask bank on Tuesdays.", ["L2"],
                    what="event")
    assert [m.confidence for m in kept(G, framed)] == [EXTRACT_CONFIDENCE]
    his = memory("Arkon", "Testy asked about the flask bank and it's still mine.", ["L1", "L2"],
                 what="guild_master")
    assert [m.confidence for m in kept(G, his)] == [EXTRACT_CONFIDENCE]


def test_her_promise_told_as_one_is_no_copy_of_her_line():
    """Review Minor 1: six of her words in a row inside "I promised ..." are her promise, not a
    copy; without the frame they still are."""
    lines = thread((1, "can you take me through the old harbour dungeon tomorrow?", 101),
                   (101, "sure, i'll run you through the old harbour dungeon tomorrow", 1))
    promise = memory("Arkon", "I promised Testy I'd run him through the old harbour dungeon "
                     "tomorrow.", ["L1", "L2"], what="promise")
    assert [m.text for m in kept(lines, promise)] == [promise["text"]]
    bare = dict(promise, text="Testy and me, running through the old harbour dungeon tomorrow.")
    assert kept(lines, bare) == []


def test_keeps_or_always_needs_more_than_one_line_of_his():
    """Review Minor 2 (judge B: "Testy keeps asking" after one ask): an "always", "keeps" or
    "every time" of his needs more than one line of his behind it."""
    once = thread((1, "so when do i get my portrait?", 103), (103, "soon, hold still", 1))
    m = memory("Coros", "Testy keeps asking me when he gets his portrait.", ["L1", "L2"],
               what="guild_master")
    assert kept(once, m) == [] and kept(once, dict(m, text="Testy always asks me when he gets "
                                                           "his portrait.")) == []
    assert len(kept(once, dict(m, text="Testy asked me when he gets his portrait."))) == 1
    twice = thread((1, "so when do i get my portrait?", 103), (103, "soon, hold still", 1),
                   (1, "how soon is soon?", 103))
    assert len(kept(twice, dict(m, evidence=["L1", "L2", "L3"]))) == 1


def test_a_dropped_memory_keeps_its_nudges():
    """Review Important 5: #430's attraction +1 toward him rode on a memory the copy rule drops,
    and small talk lost its nudges the same way. Only the hard drops (an unknown speaker, the
    real name, romance at a member, no evidence) take them along."""
    flirt = dict(m430, what="plan", disagrees=False,
                 nudges=[{"to": "Testy", "dim": "attraction", "delta": 1}])
    out = validated(G, flirt)
    assert out.memories == [] and [(n.a, n.b, n.dim, n.delta) for n in out.nudges] == [
        (104, 1, "attraction", 1)]
    small = memory("Ebbo", "Arkon teased me about my lag and I laughed it off.", ["L2"], 3,
                   what="event", nudges=[{"to": "Arkon", "dim": "affinity", "delta": 1}])
    out = validated(H, small)
    assert out.memories == [] and [(n.a, n.b, n.dim) for n in out.nudges] == [
        (105, 101, "affinity")]
    crush = memory("Belra", "I have a crush on Ebbo.", ["L4"], what="event",
                   nudges=[{"to": "Ebbo", "dim": "affinity", "delta": 2}])
    assert validated(D, crush).nudges == []


def test_a_birthday_is_wrong_only_when_nobody_it_names_has_it_tonight():
    """Review Important 4: on her own birthday "Belra wished me a happy birthday" was checked
    against Belra's card. Many cards share a date, so: wrong only when neither the speaker nor
    a member the memory names has that birthday; talk about birthdays is not checked, nor a
    birthday of his (he has no card)."""
    lines = thread((102, "happy birthday coros!"), (103, "aww thank you belra, best night"))
    wished = memory("Coros", "Belra wished me a happy birthday and I thanked her.", ["L1", "L2"],
                    what="event")
    hers = ThreadFacts(birthdays={103: "10-29", 102: "03-14"}, tz=timezone.utc)
    assert len(kept(lines, wished, facts=hers)) == 1
    assert kept(lines, wished) == []  # nobody's birthday tonight
    talk = memory("Coros", "I teased Belra that her birthday list is longer than the raid "
                  "roster.", ["L1", "L2"], what="event")
    assert len(kept(lines, talk)) == 1
    his = thread((1, "it's my birthday today!"), (103, "happy birthday testy!!"))
    assert len(kept(his, memory("Coros", "I wished Testy a happy birthday.", ["L1", "L2"],
                                what="event"))) == 1


def test_the_overheard_line_runs_the_birthday_and_disagrees_checks():
    """Review Important 3: the rehearsal's false birthday, laundered into ten bystanders'
    memories at 0.9, must not pass as overheard."""
    said = {"text": "I saw Dunna and Coros wish Ebbo a happy birthday.", "importance": 6,
            "disagrees": False}
    assert validated(D, overheard=said).overheard is None
    tonight = ThreadFacts(birthdays={105: "10-29"}, tz=timezone.utc)
    assert validated(D, overheard=said, facts=tonight).overheard == (said["text"], 6)
    squabble = {"text": "I saw Arkon and Belra squabble over the flask bank.", "importance": 6}
    assert validated(G, overheard=dict(squabble, disagrees=True)).overheard is None
    assert validated(G, overheard=dict(squabble, disagrees=False)).overheard is not None
    item = EXTRACTION_SCHEMA["properties"]["overheard"]["anyOf"][1]
    assert item["properties"]["disagrees"]["type"] == "boolean"
    assert "disagrees" in item["required"]


def test_romance_at_a_member_reads_the_casts_names_in_any_case():
    """Review Important 6 (P1c ruling PR6): the romance screen reads the cast's names in any
    case, as typed chat has them."""
    lines = thread((102, "honestly i have a crush on someone here"), (103, "ooh who"))
    m = memory("Belra", "I said I have a crush on ebbo.", ["L1"], what="event")
    assert kept(lines, m) == []
    at_him = memory("Belra", "I said I have a crush on testy.", ["L1"], what="event")
    assert len(kept(lines, at_him)) == 1


def test_the_member_flirt_seam_drops_what_it_flags(monkeypatch):
    """One seam for G1's member-flirt rule (talk), which the controller points extraction at
    once both waves are merged. Until then it flags nothing."""
    from leeroy_mind.memory import extract
    lines = thread((105, "the boat finally dropped for me"),
                   (102, "ebbo that boat's worth the grind. once it's yours, come see me"))
    m = memory("Belra", "I told Ebbo to come see me once the boat is his, and meant it.",
               ["L2"], what="event")
    assert len(kept(lines, m)) == 1
    seen = []
    monkeypatch.setattr(extract, "member_flirt",
                        lambda text, members: seen.append(members) or "come see" in text)
    assert kept(lines, m) == [] and seen == [["Ebbo"]]


def test_the_extractor_logs_each_nights_counts_by_label(store, clock, caplog):
    """For the confirmation run: per night, what the model labelled each memory, what was kept
    (and how much as hearsay), what each rule dropped and how many it said disagree. Counts
    only, never a memory's text; a new night starts at zero."""
    import logging
    replies = [json.dumps({"memories": [
        memory("Belra", "Testy asked me to come by the docks after raid tonight.", ["L1", "L2"],
               what="guild_master", disagrees=False),
        memory("Belra", "Arkon told me the docks are closed tonight.", ["L2"], what="claim",
               disagrees=True)], "overheard": None}),
        json.dumps({"memories": [memory("Coros", "I joked about the fishing.", ["L3"], 2,
                                        what="event", disagrees=False)], "overheard": None}),
        json.dumps({"memories": [], "overheard": None})]
    ex, _ = _rig(store, clock, replies)
    first = thread((1, "come by the docks after raid?"), (102, "be there"))
    other = thread((103, "anyone up for the fishing derby"), (104, "not me, the lake is cursed"))
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(ex.extract(_store_thread(store, first)))
        run(ex.extract(_store_thread(store, other)))
        night = [r.leeroy_fields for r in caplog.records
                 if getattr(r, "leeroy_event", "") == "extraction_counts"]
        assert len(night) == 2
        assert night[-1] == {"night": "2026-10-29", "threads": 2,
                             "what": {"claim": 1, "event": 1, "guild_master": 1},
                             "kept": {"guild_master": 1}, "hearsay": 0,
                             "dropped": {"disagrees": 1, "small_talk": 1}, "disagrees": 1,
                             "overheard": {}}
        run(clock.advance(86400, 3600))
        run(ex.extract(_store_thread(store, other)))
    last = [r.leeroy_fields for r in caplog.records
            if getattr(r, "leeroy_event", "") == "extraction_counts"][-1]
    assert (last["night"], last["threads"], last["what"]) == ("2026-10-30", 1, {})
    text = " ".join(r.getMessage() for r in caplog.records if "extraction_counts" in
                    r.getMessage())
    assert "docks" not in text and "fishing" not in text


def test_the_known_block_marks_hearsay_and_keeps_to_its_budget(store, clock):
    """Review Minor 6: a memory held as hearsay must not read as known, and the block must not
    crowd the rules out of a long thread's prompt."""
    from leeroy_mind.memory.extract import KNOWN_TOKENS
    from leeroy_mind.text import estimate_tokens
    ex, llm = _rig(store, clock, [json.dumps({"memories": [], "overheard": None})])
    guild = store.insert_chat_line(Chat(ts=0, channel="guild", from_guid=102, from_name="Belra",
                                        to_guid=0, text="the bank moved", map=0, zone=0),
                                   session_id=None, origin="game")
    store.insert_memory(MemoryRow(
        id=None, owner=101, kind="conversation", text="Belra told me the flask bank moved to "
        "Fridays.", t_event=1000, t_created=1000, t_accessed=1000, access_n=0, actors=(102,),
        place=None, boss_id=None, raid_id=None, importance=6, valence=0.0, source="extract",
        evidence=(f"chat:{guild}",), confidence=CLAIM_CONFIDENCE))
    for g in (101, 102, 103, 104):
        for i in range(3):
            _remember(store, g, f"The flask bank on Tuesdays, story {i}: " + "it went long " * 12,
                      ["owner:M9"], key=f"owner:M9{i}:{g}")
    run(ex.extract(_store_thread(store, G)))
    known = llm.calls[0]["messages"][1]["content"].split("\n\nConversation:\n")[0]
    lines = known.splitlines()[1:]
    assert lines[0] == "- Tonight is October 29." and lines[1].startswith("- Arkon: birthday")
    assert estimate_tokens("\n".join(lines)) <= KNOWN_TOKENS
    heard = [x for x in lines if "moved to Fridays" in x]
    assert heard == [] or heard[0].endswith("(heard)")
    ex2, llm2 = _rig(store, clock, [json.dumps({"memories": [], "overheard": None})])
    store.conn.execute("UPDATE memory SET status = 'archived' WHERE source = 'owner'")
    store.conn.commit()
    run(ex2.extract(_store_thread(store, G)))
    known2 = llm2.calls[0]["messages"][1]["content"].split("\n\nConversation:\n")[0]
    assert "- Arkon remembers: Belra told me the flask bank moved to Fridays. (heard)" in known2


def test_a_wish_to_someone_it_is_with_counts_their_birthday():
    """Re-review: "I wished him a happy birthday" names nobody, but the memory is with the
    member whose birthday it is (he said so in the thread); the people a memory is with are
    among those it can mean."""
    lines = thread((105, "it's my birthday tonight lol"), (103, "happy birthday ebbo!"))
    wish = memory("Coros", "I wished him a happy birthday.", ["L1", "L2"], what="event")
    his_night = ThreadFacts(birthdays={105: "10-29", 103: "03-14"}, tz=timezone.utc)
    assert len(kept(lines, wish, facts=his_night)) == 1
    assert kept(lines, wish) == []  # nobody's birthday tonight
