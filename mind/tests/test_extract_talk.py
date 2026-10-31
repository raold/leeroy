"""Extraction's prompt and its small-talk rule (part B of the final fix wave; the rest of the
extractor is tested in test_extract.py)."""
from leeroy_mind.human import HumanIdentity
from leeroy_mind.memory.extract import build_extraction_messages, validate_extraction
from leeroy_mind.storage.rows import ChatLineRow

# Invented identity: the real name lives only in the owner's local mind.toml.
HUMAN = HumanIdentity(1, "Testmage", "Testy", "Quillon", True, pronouns="he")
NAMES = {1: "Testy", 101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna"}


def lines(*spec):
    return [ChatLineRow(id=i, ts=1000.0 + i, channel="guild", from_guid=g,
                        from_name=NAMES.get(g, "Testmage"), to_guid=0, text=text, map=0, zone=0,
                        session_id=None, thread_id=None, origin="game")
            for i, (g, text) in enumerate(spec, 1)]


def memory(speaker, text, evidence, importance=5):
    return {"speaker": speaker, "text": text, "what": "event", "disagrees": False,
            "importance": importance, "valence": 0.1, "about": ["Testy"], "evidence": evidence}


def kept(thread, memories, overheard=None):
    out = validate_extraction({"memories": memories, "overheard": overheard}, thread, NAMES, 1,
                              human=HUMAN)
    return out


def test_the_prompt_says_most_small_talk_leaves_nothing():
    """Prompts I11: the prompt asked what members would remember and never that most
    exchanges leave nothing."""
    system = build_extraction_messages(lines((1, "evening all")), NAMES, 1,
                                       human=HUMAN)[0]["content"]
    assert "Most short exchanges leave nothing to remember" in system
    assert "an empty memories list is a good answer" in system
    assert "3 or less is small talk and banter, which is not kept" in system


def test_greetings_gz_and_goodnights_leave_no_memory():
    """Prompts I11: a login's hellos became "I said hey to Testy when he logged on." at
    importance 5 for every greeter, every night. A memory resting only on small talk (names
    aside) is dropped, and so is one the model itself rates 1."""
    thread = lines((1, "evening all"), (102, "hey testy!"), (101, "yo"), (103, "evening boss"),
                   (104, "gz on the drake"), (102, "lol"), (1, "gn all"), (103, "night, sleep well"))
    out = kept(thread, [
        memory("Belra", "I said hey to Testy when he logged on.", ["L1", "L2"]),
        memory("Arkon", "I said yo to Testy.", ["L3"]),
        memory("Coros", "I said goodnight to Testy.", ["L7", "L8"]),
        memory("Belra", "I laughed at something.", ["L6"]),
        memory("Dunna", "I congratulated Testy on the drake.", ["L5"], importance=1),
    ])
    assert out.memories == []
    out = kept(thread, [memory("Dunna", "I saw Testy get the drake from the weekly.", ["L5"])])
    assert [m.owner for m in out.memories] == [104]  # gz on something real is not small talk


def test_overheard_is_first_person():
    """Spec minor 7: the overheard memory goes to bystanders as their own memory, so it is
    asked for in the first person."""
    system = build_extraction_messages(lines((1, "evening all")), NAMES, 1,
                                       human=HUMAN)[0]["content"]
    assert "overheard is one first-person sentence" in system
    assert "third-person" not in system
