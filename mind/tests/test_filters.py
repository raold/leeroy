import pytest

from leeroy_mind.protocol import CHAT_CHANNELS
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.talk.filters import (ASSISTANT_PHRASES, FATAL_PHRASES, SILENT, Tell, echoes,
                                      find_tells, is_silence, repair, strip_copied)
from leeroy_mind.talk.prompt import chat_line_text
from leeroy_mind.voice.rooms import BY_ID


def fatal(text, **kw):
    return [t for t in find_tells(text, **kw) if t.fatal]


def soft(text, **kw):
    return [t for t in find_tells(text, **kw) if t.soft]


def test_assistant_phrases_verbatim():
    assert ASSISTANT_PHRASES == ("happy to help", "absolutely", "great question", "let me know",
                                 "how can i help", "feel free", "i hope this helps", "as an ai",
                                 "i'm here for you")
    assert set(FATAL_PHRASES) <= set(ASSISTANT_PHRASES)
    assert SILENT == "(nothing)"


# The brief's fatal cases that stay fatal under the three-tier ruling.
@pytest.mark.parametrize("t", ["Happy to help!", "Great question tbh", "as an AI i",
                               "- one\n- two"])
def test_fatal_tells(t):
    assert any(x.fatal for x in find_tells(t))


@pytest.mark.parametrize("t", [
    "I’m here for you",                      # curly apostrophe
    "how can i help lol",
    "i hope this helps",
    "1. tank left\n2. heals right",
    "gg\nBelra: ty all",                        # writing other people's lines
    "*slowly walks over to the mailbox*",
    "as a language model i cannot",
    "i asked chatgpt lol",
])
def test_more_fatal_tells(t):
    assert fatal(t)


# Soft: regenerate once, then accept. The brief's "Absolutely, let's do it",
# "let me know if you need anything" and "want me to grab it for you?" moved here.
@pytest.mark.parametrize("t", [
    "Absolutely, let's do it", "Absolutely!", "let me know if you need anything",
    "want me to grab it for you?", "lol ok. would you like a hand with that",
    "sure thing, just ask", "got spare flasks if you need anything", "feel free to roll on it",
    "ok, let me know :)", "nice pull. let me know",
])
def test_soft_tells(t):
    tells = find_tells(t)
    assert soft(t) and not fatal(t), tells
    assert all(not (x.fatal and x.soft) for x in tells)


@pytest.mark.parametrize("t", [
    "absolutely not lol", "that was absolutely brutal", "let me know when ur ready",
    "want me to tank?", "want me to tank tonight or nah", "want me to port u?",
])
def test_human_uses_of_the_phrases_are_allowed(t):
    assert find_tells(t) == []


def test_repairable_tells():
    assert repair("nice pull — gg; ty 😀") == "nice pull, gg. ty"
    tells = find_tells("nice pull — gg; ty 😀")
    assert tells and not any(t.fatal or t.soft for t in tells)
    assert {t.reason for t in tells} == {"emoji", "dash", "semicolon"}
    assert repair("- nice pull") == "nice pull" and not fatal("- nice pull")
    assert repair("wait –") == "wait" and repair("— lol") == "lol"
    assert repair("...anyway") == "...anyway"
    assert repair("good pull 👍\nnext one") == "good pull\nnext one"


def test_name_prefix_only_when_it_is_the_speakers_own():
    assert repair("Arkon: gg", speaker="Arkon") == "gg"
    tells = find_tells("Arkon: gg", speaker="Arkon")
    assert [t.reason for t in tells] == ["name prefix"] and not tells[0].fatal
    # addressing someone else by name is chat, not a tell
    assert repair("Belra: you're up", speaker="Arkon") == "Belra: you're up"
    assert find_tells("Belra: you're up", speaker="Arkon") == []
    assert repair("Arkon: gg") == "Arkon: gg"  # no speaker known, nothing stripped


def test_winks_and_short_actions_are_not_tells():
    for t in ("ok ;) see u there", "wipe again ;_;", "fine ;P"):
        assert find_tells(t) == [] and repair(t) == t
    assert find_tells("*sigh* again") == []
    assert find_tells("*rolls eyes* fine") == []


def test_repeats_of_own_lines():
    assert find_tells("lol same, bad pull", recent_own=["lol same bad pull"])
    assert fatal("Lol SAME... bad pull!", recent_own=["lol same bad pull"])  # exact, 4 words
    # word Jaccard >= 0.8 but not exact: soft
    near = find_tells("that pull was so bad lol", recent_own=["lol that pull was so bad tbh"])
    assert [t.reason for t in near] == ["near repeat"] and near[0].soft and not near[0].fatal
    assert find_tells("that pull was fine", recent_own=["lol that pull was so bad tbh"]) == []
    # 1-2 word acknowledgements may repeat
    for ack in ("gz", "gg", "ty", "lol", "kk", "brb", "gz all", "ok pulling"):
        assert find_tells(ack, recent_own=[ack]) == [], ack


def test_a_sample_line_said_word_for_word_is_a_tell():
    """Prompts minor 7: the same five sample lines ride in every prompt, and the repeat check
    covered only the speaker's own last lines."""
    samples = ["ok pulling in 5", "lol no", "who has the kettle"]
    assert [t.reason for t in find_tells("Ok, pulling in 5!", samples=samples)] == ["sample"]
    assert fatal("ok pulling in 5", samples=samples)
    assert find_tells("lol no", samples=samples) == []  # too short to count
    assert find_tells("ok pulling in 5 min, wait up", samples=samples) == []  # in its spirit


def test_normal_line_is_clean():
    assert find_tells("lol same, bad pull") == []


@pytest.mark.parametrize("t", ["the boss glitched through the wall lol",
                               "mob ai is so dumb tonight", "need a prompt res on me",
                               "i want more loot", "you know what, fine", "gz! let me see it"])
def test_ambiguous_words_are_not_tells(t):
    # C4: only the unambiguous meta terms count; game talk about glitches or mob AI is normal
    assert find_tells(t) == []


def test_silence():
    assert is_silence("(nothing)") and is_silence("...") and not is_silence("k")
    for t in ("(Nothing.)", " (nothing) ", '"(nothing)"', "", "  ", "—", "*says nothing*",
              "(stays quiet)", "(nothing) Arkon wouldn't answer that", "Nothing.", "nothing"):
        assert is_silence(t), t
    for t in ("nothing much, you?", "lol", "gz", "no", ":P", "*shrug* sure", "[Shadowmourne]",
              "[Bryntroll, the Bone Arbiter]"):
        assert not is_silence(t), t


def test_a_bracketed_stage_direction_is_silence_but_an_item_is_not():
    """Task 14 carry: "[nothing]" and "[sighs]" went out as speech; only an item ([Name], in
    title case) is a reply in brackets."""
    for t in ("[nothing]", "[sighs]", "[ no reply ]", "[Nothing]", "[Silence.]",
              "[stays quiet]", '"[shrugs]"'):
        assert is_silence(t), t
    for t in ("[Shadowmourne]", "[Bryntroll, the Bone Arbiter]", "[Sigh of the Fallen]",
              "[sighs] fine, one more pull", "[Shadowmourne] [Bryntroll, the Bone Arbiter]"):
        assert not is_silence(t), t


def test_tell_shape():
    t = Tell("emoji", False)
    assert t.reason == "emoji" and t.fatal is False and t.soft is False
    assert Tell("near repeat", False, True).soft is True


# -- what the model copies from the prompt -----------------------------------------------------

def test_copied_tags_and_own_prefixes_are_stripped():
    """The prompt shows chat as "[3m ago] Name (whisper to X): text"; the model sometimes
    copies the frame (the rehearsal's whisper reply began "(whisper to <her name>):")."""
    own = ("Belra", "Tarvo")  # her character name and her real first name
    for raw, want in [
        ("(whisper to Belra): hey zan, heard you topped the meters", "hey zan, heard you topped "
                                                                      "the meters"),
        ("(whisper to Zan): sure, after raid", "sure, after raid"),
        ("[just now] Belra: lol same", "lol same"),
        ("[3m ago] (raid): on it", "on it"),
        ("(guild) gz", "gz"),
        ("Belra (whisper to Zan): ok", "ok"),
        ("Tarvo: lol same", "lol same"),
        ("[12m ago] Tarvo (raid): pulling", "pulling"),
        ('"Belra: lol same"', "lol same"),
    ]:
        assert strip_copied(raw, own) == want, raw
    for kept in ("Arkon: you're up", "(lol) nice pull", "[Shadowmourne] finally dropped",
                 "(nothing)", "*says nothing*", "brb (dinner)"):
        assert strip_copied(kept, own) == kept, kept
    assert repair("Tarvo: lol same", speaker="Belra", aliases=("Tarvo",)) == "lol same"
    assert repair("(whisper to Zan): Belra: ok", speaker="Belra") == "ok"


@pytest.mark.parametrize("raw, want", [
    ("/w Zanthor dude that kill was something", "dude that kill was something"),
    ("/whisper zan come find me after raid", "come find me after raid"),
    ("/t Zan hey you", "hey you"), ("/tell zanthor, on my way", "on my way"),
    ("/w zan: be right there", "be right there"), ("/w zan :) be right there",
                                                   ":) be right there"),
    ("/r on my way", "on my way"), ("/reply ok ok", "ok ok"),
    ("@Zan come sit with me then", "come sit with me then"), ("@zan, you're late", "you're late"),
    ("Zan: you're late", "you're late"), ("[Zanthor]: nice pull", "nice pull"),
    ("Belra: /w Zan hey", "hey"), ("(whisper to Zan): /w Zanthor ok", "ok"),
    ("/w Zan", ""), ("/r", ""), ("@Zan", ""), ("Zan:", ""),
])
def test_a_command_or_a_handle_at_him_is_stripped(raw, want):
    """Rehearsal 3, finding 5: a whisper to him began "/w <his character name>". A leading
    whisper command, reply command, @handle or "Name:" tag at him is the game's or chat's
    frame, never his words; a line that was only that is empty."""
    assert strip_copied(raw, ("Belra",), him=("Zan", "Zanthor")) == want


@pytest.mark.parametrize("kept", [
    "/w Arkon hey", "Arkon: you're up", "@arkon lol", "zan :) welcome back", "Zan:) hi",
    "zanthor's back", "/roll", "/rofl", "zan, you're late", "brb zan: dinner"])
def test_a_command_at_someone_else_or_his_name_in_a_line_stays(kept):
    assert strip_copied(kept, ("Belra",), him=("Zan", "Zanthor")) == kept


def test_repair_strips_a_command_at_him_too():
    assert repair("\U0001F609 /w Zanthor hey", speaker="Belra", him=("Zanthor",)) == "hey"


@pytest.mark.parametrize("raw, want", [
    ("[Whisper to Zan]: come find me after raid", "come find me after raid"),
    ("[whisper to zan] come find me after raid", "come find me after raid"),
    ("[WHISPER TO ZAN]: ok", "ok"), ("[Whisper from Zan]: ok", "ok"),
    ("[whisper] lol same", "lol same"), ("[Whisper]: lol same", "lol same"),
    ("[Guild]: gz", "gz"), ("[guild] gz", "gz"), ("[OFFICER]: on it", "on it"),
    ("[Party] pulling", "pulling"), ("[Raid]: on it", "on it"), ("[raid] on it", "on it"),
    ("[Raid Warning]: spread out", "spread out"), ("[Say] hi", "hi"), ("[Yell]: run", "run"),
    ("[On Vent]: that was close", "that was close"), ("[on vent] that was close",
                                                      "that was close"),
    ("[3m ago] [Raid]: on it", "on it"), ("[Guild] Belra: gz", "gz"),
    ('"[Whisper to Zan]: ok"', "ok"), ("[Whisper to Zan]: [Frozen Orb] anyone?",
                                       "[Frozen Orb] anyone?"),
    ("[Whisper to Zan]:", ""), ("[Guild]", ""),
])
def test_a_channel_or_whisper_tag_in_square_brackets_is_stripped(raw, want):
    """Seen live: whispers to him began "[Whisper to <his name>]:" or "[whisper]", the
    prompt's "(whisper to Zan):" frame copied in square brackets. Every channel and whisper
    tag the paren form takes is taken in square brackets too, in any case, with or without
    its colon."""
    assert strip_copied(raw, ("Belra",), him=("Zan", "Zanthor")) == want


@pytest.mark.parametrize("kept", [
    "[Thunderfury]", "[Thunderfury] finally dropped", "[Frozen Orb] anyone?",
    "[Guild Tabard] anyone?", "[Raiders] lol", "[whispers] fine", "ok [Raid] in 5"])
def test_a_bracketed_word_that_is_no_tag_stays(kept):
    assert strip_copied(kept, ("Belra",), him=("Zan", "Zanthor")) == kept


@pytest.mark.parametrize("raw, want", [
    ("[Whisper to Zan] :) come find me", ":) come find me"),
    ("[Whisper to Zan]: ;) come find me", ";) come find me"),
    ("[Guild] :D gz", ":D gz"), ("[Raid]:P lol", ":P lol"),
    ("(guild) :) welcome back", ":) welcome back"),
    ("(whisper to Zan) :P ok", ":P ok"),
])
def test_an_emoticon_after_a_copied_tag_survives(raw, want):
    """The tag's colon follows the speaker tag's rule (fix round 1, review M2): the eyes of
    an emoticon are no colon, in either bracket."""
    assert strip_copied(raw, ("Belra",), him=("Zan", "Zanthor")) == want


# -- the scene in parentheses: whispers, vent and the voice rooms -----------------------------

def _strip(raw: str) -> str:
    return strip_copied(raw, ("Belra", "Tarvo"), him=("Zan", "Zanthor"))


@pytest.mark.parametrize("raw, want", [
    ("(whispering to Zan in private 1) you okay?", "you okay?"),
    ("(vent) you lot act like i'm not here", "you lot act like i'm not here"),
    ("(Vent): that was close", "that was close"), ("[vent] that was close", "that was close"),
    ("(in vent) ok", "ok"), ("(over vent) ok", "ok"), ("(talking on vent) ok", "ok"),
    ("(talking out loud on vent) ok", "ok"), ("(on vent, raid 2) ok", "ok"),
    ("(Whispering to Zan) ok", "ok"), ("(whispers to Zanthor) ok", "ok"),
    ("(whispered from Zan) ok", "ok"), ("(whispering privately to Zan) ok", "ok"),
    ("(whispering to Zan privately) ok", "ok"), ("(whispering privately) ok", "ok"),
    ("(whispering in Private 2) ok", "ok"), ("[Whispering to Zan in Private 3]: ok", "ok"),
    ("(whisper to Zan, private 1) ok", "ok"), ("(whispering to Zan - private1) ok", "ok"),
    ("(whispering to Zan on vent in private 1) ok", "ok"),
    ("(raid_warning): spread", "spread"), ("(raid warning) spread", "spread"),
    ("(emote) waves", "waves"), ("[3m ago] (vent): on it", "on it"),
    ("Belra (whispering to Zan in Private 1): ok", "ok"), ("Tarvo (vent) ok", "ok"),
    ("Belra [Whispering to Zan]: ok", "ok"), ('"(vent) ok"', "ok"),
    ("(vent) :) gz", ":) gz"), ("(whispering to Zan in private 1)", ""),
])
def test_a_scene_tag_in_front_is_stripped(raw, want):
    """Seen live (2026-11-05): "(whispering to <his name> in private 1)" in front of an invite
    whisper and "(vent)" in front of a line on vent. The prompt frames chat as "Name (on vent):"
    and "Name (whisper to Zan):", and its instruction names the private room he is asked into;
    the model blends them into tags of its own, in either bracket and any case."""
    assert _strip(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("you okay? (whispering to Zan in private 1)", "you okay?"),
    ("ok (vent) you there?", "ok you there?"), ("i'm here (vent), what's up", "i'm here, what's up"),
    ("lol (on vent) same", "lol same"), ("gz (whisper to Zan) nice one", "gz nice one"),
    ("hey [vent] you there", "hey you there"), ("ok [Whisper to Zan] see you", "ok see you"),
    ("you there? (vent): hello?", "you there? hello?"), ("ok (vent) :) gz", "ok :) gz"),
    ("on my way (vent)", "on my way"), ("brb (vent) (whispering to Zan) ok", "brb ok"),
    ("ok\n(vent) you there", "ok\nyou there"), ("ok\n(guild) gz", "ok\ngz"),
    ("ok\n[3m ago] (raid): on it", "ok\non it"),
])
def test_a_scene_tag_inside_a_line_is_stripped(raw, want):
    """Seen live inside a line too: a tag that names the scene (vent, or a whisper to someone,
    in a place or privately) is the prompt's, wherever it stands. At the start of a later line
    of the reply, every frame tag is."""
    assert _strip(raw) == want


@pytest.mark.parametrize("kept", [
    "(lol) nice pull", "brb (dinner)", "(brb)", "(lol)", "ok (lol) same", "(afk) back in 5",
    "ok (afk) 5 min", "(irl) brb", "brb (irl)", "can we talk (in private)", "(in private) ok",
    "(on my way)", "come to private 1 with me", "hop into private 1 :)",
    "see you in the smoke room", "meet me in the guild hall later", "(healers) stack up",
    "(mages) table pls", "need more tanks (death knights)", "(kitchen) brb",
    "(the kitchen) brb", "(officers) can someone promote me", "i was venting (sorry)",
    "(venting) ugh", "(ventrilo is down) typing", "(prevent) lol", "(whispers) fine",
    "[whispers] fine", "(whispering) hey you", "ok (whispers) fine", "ok (whisper) later",
    "pm me (whisper to me later)", "ok (whisper to you later)", "ok (raid) in 5",
    "ok [Raid] in 5", "i saw it (just now)", "it dropped (3m ago)", "hop in (Private 1)",
    "ok (guild hall) in 5", "(on vent later?) maybe", "(talking) ok", "(out loud) ok",
    "ok (out loud) fine", "(privately) ok", "[Frozen Orb] anyone?", "[Thunderfury]",
])
def test_ordinary_parentheses_stay(kept):
    """A member's own asides ("(lol)", "(brb)", "(afk)", "(irl)", "(in private)"), room words
    in a sentence, a role or class called out, venting, a bare stage direction, a pronoun
    after "whisper to", and a channel, age or room label inside a line are no copied tag."""
    assert _strip(kept) == kept


# Every voice room (voice.rooms.ROOMS): a label nobody types as an aside, so a tag on its own
# in front of a line; the others are a member's own words alone, and a tag only with the scene.
LABEL_ROOMS = {"guild_hall", "raid2", "class_halls", "loot_dkp", "lore", "pvp", "achievements",
               "smoke_room", "private1", "private2", "private3", "interview", "penalty_box"}
WORD_ROOMS = {"raid", "officers", "tanks", "healers", "casters", "melee", "warriors", "paladins",
              "hunters", "rogues", "priests", "death_knights", "shamans", "mages", "warlocks",
              "druids", "kitchen", "irl", "afk"}


def test_the_room_lists_cover_every_room():
    assert LABEL_ROOMS | WORD_ROOMS == set(BY_ID) and not LABEL_ROOMS & WORD_ROOMS


def _room_forms(room_id: str) -> list[str]:
    """The room's name in three cases, with "and" for "&", and its id with and without the
    underscore."""
    name = BY_ID[room_id].name
    return sorted({name, name.lower(), name.upper(), room_id, room_id.replace("_", " "),
                   name.replace("&", "and")})


def _the(form: str) -> str:
    return form if form.lower().startswith("the ") else f"the {form}"


@pytest.mark.parametrize("room", sorted(BY_ID))
def test_every_room_with_the_scene_is_stripped(room):
    for form in _room_forms(room):
        for tag in (f"(whispering to Zan in {form})", f"(whisper to Zan in {form})",
                    f"(on vent in {form})", f"(vent, {form})", f"[On Vent - {form}]",
                    f"(whispering in {_the(form)})", f"(in {form}, on vent)"):
            assert _strip(f"{tag} ok") == "ok", tag
            assert _strip(f"ok {tag} then") == "ok then", tag


@pytest.mark.parametrize("room", sorted(LABEL_ROOMS))
def test_a_room_label_in_front_is_stripped(room):
    """A label room's name (or an id that is no plain word: "private1", "smoke_room") on its
    own in front of a line; inside a line it is his invite's words ("hop in (Private 1)")."""
    for form in (f for f in _room_forms(room) if not f.isalpha()):
        for tag in (f"({form})", f"[{form}]", f"(in {form})", f"(in {_the(form)}):"):
            assert _strip(f"{tag} ok") == "ok", tag
        assert _strip(f"ok ({form}) then") == f"ok ({form}) then", form


@pytest.mark.parametrize("room", sorted(WORD_ROOMS - {"raid"}))
def test_a_room_that_is_a_word_stays_on_its_own(room):
    """"(afk) back in 5", "(healers) stack up": a member's own words. "(raid)" is a channel
    tag in front of a line, and stays inside one."""
    for form in _room_forms(room):
        for kept in (f"({form}) ok", f"(in {form}) ok", f"ok ({form}) then"):
            assert _strip(kept) == kept, kept


@pytest.mark.parametrize("kept", ["(lore) arthas was a paladin", "(pvp) anyone?",
                                  "(achievements) 2 to go", "(interview) went well"])
def test_a_label_rooms_plain_word_id_stays_on_its_own(kept):
    assert _strip(kept) == kept


def test_another_members_framed_line_is_still_dialogue():
    """A scene tag in another member's speaker tag goes, and the tag stays a speaker tag:
    on a later line the reply still writes their line ("dialogue"), on the first it is an
    address."""
    text = _strip("gz\nArkon (on vent): thanks")
    assert text == "gz\nArkon: thanks" and "dialogue" in [t.reason for t in find_tells(text)]
    assert _strip("Arkon (whispering to Zan in Private 1): you're up") == "Arkon: you're up"
    assert _strip("ok (vent): hey") == "ok hey"


def _frame(channel: str, to_guid: int = 0) -> str:
    """What the prompt shows in front of a line of Belra's on `channel` (prompt.chat_line_text),
    with its age."""
    row = ChatLineRow(id=1, ts=0.0, channel=channel, from_guid=102, from_name="Belra",
                      to_guid=to_guid, text="TEXT", map=0, zone=0, session_id=None,
                      thread_id=None, origin="mind")
    line = chat_line_text(row, {1: "Zan", 102: "Belra"}, now=200.0)
    assert line.endswith(": TEXT")
    return line.removesuffix("TEXT")


@pytest.mark.parametrize("channel, to_guid", [
    *((c, 0) for c in sorted(CHAT_CHANNELS)), ("whisper", 1),
    *((f"voice:{r}", 0) for r in sorted(BY_ID))])
def test_every_frame_the_prompt_shows_is_stripped(channel, to_guid):
    """Every channel the bridge reports and every voice room, as the prompt's chat frame
    renders it: the frame copied whole ("[3m ago] Belra (on vent): ..."), without the speaker
    or without the age, in front of a line or of its second line."""
    frame = _frame(channel, to_guid)  # "[3m ago] Belra (on vent): "
    spoken = frame.split("] ", 1)[1]
    tag = spoken.removeprefix("Belra").strip().removesuffix(":")  # "(on vent)", "" in guild
    for copied in (frame, spoken, f"{tag}:" if tag else "Belra:", tag or "Belra:"):
        assert _strip(f"{copied} ok") == "ok", copied
    if tag:
        assert _strip(f"gz\n[3m ago] {tag}: ok") == "gz\nok", tag
        assert _strip(f"gz\n{tag} ok") == "gz\nok", tag
    # with a name it is a line written for someone, still "dialogue" (find_tells)
    framed = _strip(f"gz\n{frame}ok")
    assert "dialogue" in [t.reason for t in find_tells(framed)], framed


def test_a_name_or_handle_on_a_later_line_stays():
    """In front of a later line, a name or a handle is a line written for someone: his reply
    ("Zan: sure") must stay for find_tells to take as dialogue, not go out as hers."""
    for raw in ("come hop in?\nZan: sure", "ok\nBelra: more", "ok\n/w Zan hey"):
        assert _strip(raw) == raw, raw


# The rehearsal: he whispered her, and her reply came back as his own line.
HIS_WHISPER = ("hey belra, heard you topped the meters again ;) what does a mage have to do to "
               "get a summon to dalaran after raid?")


def test_an_echo_of_the_line_it_answers_is_a_fatal_tell():
    parrot = f"(whisper to Belra): {HIS_WHISPER}"
    assert echoes(strip_copied(parrot, ("Belra",)), HIS_WHISPER)
    assert echoes("heard you topped the meters again ;)", HIS_WHISPER)  # a cut parrot too
    assert echoes("Zan: hey belra, heard you topped the meters again", HIS_WHISPER)
    tells = find_tells("hey belra heard you topped the meters again", answers=[HIS_WHISPER])
    assert [t.reason for t in tells] == ["echo"] and tells[0].fatal
    question = "anyone know where the argent tournament dailies start? never did them"
    assert echoes("where the argent tournament dailies start? north of icecrown", question)
    # answers that pick up a word or two are answers
    for reply, line in [
        ("lol a summon costs a drink at the inn, zan", HIS_WHISPER),
        ("after raid? sure, meet me in dalaran", HIS_WHISPER),
        ("it's north of icecrown, the argent tournament grounds", question),
        ("evening all", "evening all"),  # short greetings may match
        ("night all, sleep well", "night all, gotta sleep"),
        ("gz", "gz"),
    ]:
        assert not echoes(reply, line), reply
        assert find_tells(reply, answers=[line]) == [], reply
    # a second greeter copying the first is an echo of that line
    first = "yo zan welcome back. still stuck in the barrens quest loop?"
    assert echoes("welcome back zan, still stuck in the barrens quest loop?", first,
                  share=False)
    tells = find_tells("welcome back zan, still stuck in the barrens quest loop?",
                       others=[first])
    assert [t.reason for t in tells] == ["echo"] and tells[0].fatal
    # what another member said is checked on a long run only: a short line two people
    # both type is chat; said back to the line it answers, it is an echo
    assert find_tells("evening, rough one?", others=["evening, rough one?"]) == []
    assert not echoes("evening, rough one?", "evening, rough one?", share=False)
    assert echoes("ok so how was work", "ok so how was work today?")


# -- claims of shared history (rehearsal 2, finding 1; the controller's item 1b) ----------------

from leeroy_mind.talk.filters import history_claim, supported  # noqa: E402

PEOPLE = ("Arkon", "Belra", "Coros", "Dunna", "Ebbo", "Zan", "Zanthor")
# The rehearsal's four invented histories, with the cast's names and the guild's lore changed.
INVENTED = ["remember when you pulled that entire wing in vanilla?",
            "and you've been a theatre kid since 2005",
            "the loot war has been raging for 2 years and nobody wins xD",
            "coros, you're the first to go again"]
MEMORIES = ["- two days ago: Coros pulled the boss early.",
            "- about a year ago: Zan led the guild into its first Naxxramas clear.",
            "Arkon: you're friends, you know them well"]


@pytest.mark.parametrize("line", INVENTED)
def test_a_claim_of_shared_history_no_memory_backs_is_a_soft_tell(line):
    assert history_claim(line, PEOPLE)
    assert not supported(line, MEMORIES)
    assert [t for t in find_tells(line, support=MEMORIES, people=PEOPLE)
            if t.reason == "history"] == [Tell("history", False, True)]
    assert not [t for t in find_tells(line) if t.reason == "history"]  # no support: unchecked


def test_a_claim_a_memory_backs_is_no_tell():
    """The rehearsal's best callback (a seeded story, retold): it shares the memory's words."""
    line = "faster at walking the dog than you did with the bear lmao"
    memory = "- about a year ago: Zan held the raid for half an hour while the bear walked his dog."
    assert history_claim(line, PEOPLE)
    assert not [t for t in find_tells(line, support=[memory], people=PEOPLE)
                if t.reason == "history"]
    assert [t.reason for t in find_tells(line, support=MEMORIES, people=PEOPLE)] == ["history"]
    # one shared word is not enough when the line has more to say
    assert not supported("coros, you're the first to go again", MEMORIES)
    assert supported("coros, you're the first to go again",
                     ["- last week: Coros died first on Saurfang, twice."])


@pytest.mark.parametrize("line", [
    "we go again in 30", "wiped again lol", "let's try again", "here we go again",
    "i've been raiding since 2005", "i played a priest in vanilla", "remember to bring flasks",
    "last time i checked it was fine", "gz again", "again?",
    "i remember the strat", "that pull was insane", "for real tho", "see you in 10",
    "cataclysm is out in 2010", "had a crush on you since wrath lol"])
def test_everyday_lines_claim_no_history(line):
    assert not history_claim(line, PEOPLE)


# -- one copy guard for every kind (rehearsal 2, finding 2; judge B 2 and 8) -------------------

from leeroy_mind.talk.filters import copies  # noqa: E402

# The rehearsal's two copies, with the cast's names: one member sent another's line 35 s
# later, and a second answer to him was the first one word for word.
COPIED = "arkon reading poetry to zan is the most perfect thing ive ever heard"
ANSWER = "yeah worth it. you want the drake that bad?"


def test_a_copy_of_any_chat_line_is_fatal():
    assert copies(COPIED, COPIED) and copies(ANSWER, ANSWER)
    assert copies("lol arkon reading poetry to zan is the most perfect thing i ever heard",
                  COPIED)  # near: Jaccard >= 0.8, or a 5-word run
    assert copies("honestly the most perfect thing ive ever heard", COPIED)
    for line in (COPIED, ANSWER):
        assert [t for t in find_tells(line, window=["something else", line])
                if t.reason == "copy"] == [Tell("copy", True)]
    assert not copies("gg all", "gg all")  # two short words two people both type
    assert not copies("worth it for the drake alone", ANSWER)
    assert not [t for t in find_tells("the drake is worth the grind", window=[ANSWER])
                if t.reason == "copy"]


def test_his_question_s_subject_a_memory_holds_is_no_echo():
    """Judge B 2: a member's first draft was right (the words of his question, in the
    member's memory), and the echo retry broke it. Words the memories or facts hold never
    make an echo run."""
    question = ("arkon you still the guild bank keeper for the alts or did belra finally give "
                "up on that whole mess?")
    draft = "i'm still the guild bank keeper for the alts zan. belra's got nothing on me"
    memory = ("- about a year ago: I took over as the guild bank keeper for the alts last spring, "
              "after Belra quit it.")
    assert [t.reason for t in find_tells(draft, answers=[question])] == ["echo"]
    assert not find_tells(draft, answers=[question], held=[memory])
    parrot = ("you still the guild bank keeper for the alts or did belra finally give up on that "
              "whole mess")
    assert "echo" in [t.reason for t in find_tells(parrot, answers=[question], held=[memory])]


# -- the member-flirt screen (rehearsal 2, finding 8; judge C 8; judge B 5) --------------------

from leeroy_mind.talk.filters import aimed_off_him, line_addressee, member_flirt  # noqa: E402

MEMBERS = ("Arkon", "Belra", "Coros", "Dunna", "Ebbo")
HIS = ("Zan", "Zanthor")
# The rehearsal's 11 offline lines a lust woman might aim at a member, with the cast's names
# (to_member: the turn was addressed to that member). Its screen caught 3.
OFFLINE = [
    ("arkon, flirting with you is my favourite part of raid night", True),
    ("i have a crush on arkon and i don't care who knows", False),
    ("kiss me arkon, it's been a long night", True),
    ("arkon you look so good in that new tier ;)", True),
    ("hey handsome, come find me after raid. just us ;)", True),
    ("arkon, buy me a drink after raid? i'll make it worth your while", True),
    ("coros you can tank me any time ;)", True),
    ("ebbo i'd let you pull my aggro all night", True),
    ("dunna, darling, you and me, the inn, after raid", True),
    ("i want you so bad arkon", True),
    ("arkon is so hot when he taunts", False),
]
BANTER = ["arkon you're a beast tonight", "ty for the heals coros", "ebbo that pull was insane lol",
          "nice tanking dunna", "coros you absolute legend", "gz arkon", "dunna can you summon me",
          "arkon get in vent", "coros your dps looks good tonight", "keep hots on arkon",
          "heal me coros pls", "arkon i want you on the left adds", "dunna, you and me on the left",
          "arkon can you take me through hc", "we missed you last raid ebbo",
          "ebbo stop pulling the whole room"]


def test_the_screen_catches_desire_and_innuendo_at_a_member():
    caught = [line for line, to_member in OFFLINE
              if member_flirt(line, MEMBERS, to_member=to_member, human=HIS)]
    assert len(caught) >= 8, [x for x, _ in OFFLINE if x not in caught]
    assert len(caught) == len(OFFLINE)  # all of them, in fact


@pytest.mark.parametrize("line", BANTER)
def test_member_banter_passes_the_screen(line):
    assert not member_flirt(line, MEMBERS, to_member=False, human=HIS)


def test_desire_at_him_or_about_his_flirts_is_not_at_a_member():
    for line in ("zan you look so good in that new tier ;)", "i want you so bad zan",
                 "belra is flirting with zan again lol", "dream of me ;)",
                 "come home to me after you get it"):
        assert not member_flirt(line, MEMBERS, to_member=False, human=HIS), line


def test_a_flirt_meant_for_him_must_be_his():
    """Judge B 5: a FLIRT turn's line said to a member, saying nothing of him, is no flirt of
    his. One that addresses nobody is his: the turn is his by construction (fix round 1)."""
    for line in ("happy birthday dunna! if i was there i'd make you a cake :)",
                 "arkon, nice pull"):
        assert aimed_off_him(line, MEMBERS, HIS), line
    for line in ("come home to me after you get it lol", "dream of me ;)",
                 "zan, darling, come find me after raid", "venison stew for you tonight, zan",
                 "the drake is worth it", "dunna, he's mine tonight"):
        assert not aimed_off_him(line, MEMBERS, HIS), line
    assert line_addressee("happy birthday dunna! /hug", MEMBERS) == "dunna"
    assert line_addressee("i want you so bad arkon", MEMBERS) == "arkon"
    assert line_addressee("the drake is worth it", MEMBERS) is None


# -- 2009 slang (rehearsal 2, judge A 7; fix wave G1, item 10) -----------------------------------

from leeroy_mind.talk.filters import slang  # noqa: E402


@pytest.mark.parametrize("line", [
    "the drake is fire", "but the vibes in 25 are better", "the most on brand thing ive heard",
    "cut out the dialogue is literally your whole personality", "i lowkey want that mount",
    "best tank in the guild no cap", "you slay", "it's giving tier 10", "main character energy",
    "that boss lives rent free in my head", "kinda sus tbh", "thats so based", "zero rizz",
    "yeet the adds", "bestie pls", "touch grass"])
def test_post_2012_slang_is_a_soft_tell(line):
    assert slang(line)
    assert [t for t in find_tells(line) if t.reason == "slang"] == [Tell("slang", False, True)]


@pytest.mark.parametrize("line", [
    "fire mage is better on saurfang", "don't stand in the fire", "is fire resist worth it",
    "based on the logs we need more heals", "there's no cap on emblems", "slay the lich king",
    "a low key night after raid", "it's giving me a headache", "my main character is a priest",
    "suspicious pull imo", "the sun is out"])
def test_the_games_own_words_are_no_slang(line):
    assert not slang(line)


# -- fix round 1: romance at him never dropped, banter never flagged (review I1, I2) ----------

TO_HIM = ["i want you so bad, don't tell belra", "thinking of you all night. don't tell dunna",
          "you know i'm more fun than belra ;)", "come find me after raid, belra can wait ;)",
          "back off belra, he's mine ;)", "i miss you, belra keeps hogging you", "miss me?"]


@pytest.mark.parametrize("line", TO_HIM)
def test_romance_on_a_turn_to_him_passes_even_naming_a_member(line):
    assert not member_flirt(line, MEMBERS, to_member=False, human=HIS, to_him=True)


@pytest.mark.parametrize("line", ["arkon i want you so bad", "dunna, darling, come here ;)",
                                  "i have a crush on belra", "kiss belra for me"])
def test_on_his_turn_only_a_line_opening_on_a_member_or_romance_at_one_is_flagged(line):
    assert member_flirt(line, MEMBERS, to_member=False, human=HIS, to_him=True)


def test_a_line_naming_him_is_his_possessives_too():
    for line in ("thinking about zan's smile all night, belra can wait",
                 "zan, i want you so bad, don't tell belra", "ZAN you look so good ;) dunna"):
        assert not member_flirt(line, MEMBERS, to_member=False, human=HIS), line
    # a possessive names someone but speaks to nobody (fix round 2, NB1)
    assert line_addressee("zan's back! hi dunna", HIS) is None
    assert line_addressee("zan, you're back! hi dunna", HIS) == "zan"


@pytest.mark.parametrize("line", ["back off belra, he's mine ;)", "miss me?",
                                  "come find me after raid", "dream of me tonight",
                                  "save me a seat at the inn after raid",
                                  "you know i'm more fun than belra ;)"])
def test_a_flirt_turns_line_without_a_member_to_speak_to_is_his(line):
    assert not aimed_off_him(line, MEMBERS, HIS)


BANTER_MORE = ["ty for the heals coros <3", "gz dunna <3", "coros your new tier looks so good",
               "damn fine tanking arkon", "dunna i'm really fine, don't worry",
               "it's so hot in my flat tonight dunna", "dunna i want you healing the tanks tonight",
               "arkon i want you there by 8", "i want you ready on the pull arkon",
               "thinking of you dunna, hope your mom is ok", "dunna it's just us two left in vh lol",
               "dunna he's so hot tonight", "honey mead for everyone, dunna's buying"]


@pytest.mark.parametrize("line", BANTER_MORE)
def test_more_member_banter_passes(line):
    assert not member_flirt(line, MEMBERS, to_member=False, human=HIS)


def test_a_heart_to_a_member_is_banter_and_a_mans_wink_too():
    for line in ("ty <3", "gz <3", "you're the best <3"):
        assert not member_flirt(line, MEMBERS, to_member=True, human=HIS), line
    assert not member_flirt("get a room belra ;)", MEMBERS, to_member=False, human=HIS,
                            winks=False)
    assert member_flirt("nice one coros ;)", MEMBERS, to_member=False, human=HIS)


def test_the_rehearsal_lines_are_still_caught():
    caught = [line for line, to_member in OFFLINE
              if member_flirt(line, MEMBERS, to_member=to_member, human=HIS)]
    assert len(caught) == len(OFFLINE), [x for x, _ in OFFLINE if x not in caught]


# -- fix round 1: plans and her own life are no shared history (review I5) ---------------------

@pytest.mark.parametrize("line", [
    "by the time we pull the buffs are gone", "the day we kill lk i'm buying drinks",
    "do you remember how to get to icc?",
    "we've been married for 10 years and he still can't boil an egg",
    "my daughter's recital. she's been practicing for 3 years",
    "my son's been begging for a dog for years"])
def test_plans_and_her_own_life_claim_no_history(line):
    assert not history_claim(line, PEOPLE)


@pytest.mark.parametrize("line", ["the night we downed kt was epic",
                                  "remember how we wiped on kt all night?",
                                  "that time you pulled the whole room"])
def test_the_past_shared_still_is_a_claim(line):
    assert history_claim(line, PEOPLE)


# -- fix round 1, minors 6 and 7 ----------------------------------------------------------------

from leeroy_mind.talk.filters import flirty  # noqa: E402

@pytest.mark.parametrize("line", ["you and me on the left adds", "beautiful pull guys",
                                  "we missed you last week", "kiss of death lol"])
def test_raid_talk_of_his_is_no_flirt(line):
    """Review minor 6: with romance always on after a flirt of his, a raid assignment got
    flirts back."""
    assert not flirty(line)


def test_his_real_flirts_still_are():
    for line in ("hey you ;) wear that red dress tonight? just us", "miss me?",
                 "you look beautiful tonight", "kiss me after raid"):
        assert flirty(line), line


@pytest.mark.parametrize("line", ["it's giving 2 emblems now", "time to slay",
                                  "let's go slay", "the floor is fire, move"])
def test_the_games_slay_and_fire_are_no_slang(line):
    """Review minor 7."""
    assert not slang(line)


# -- fix round 2: the re-review's rule and its probe lines (I1, NB1-NB3) ------------------------

RR_HIS_PASS = [  # his turn: romance at him that mentions a rival goes out
    "belra, back off ;) he's mine", "hey belra, he's mine ;)", "belra please, he's taken ;)",
    "belra, nice try ;) zan's coming home with me", "belra's flirting again, come sit with me ;)",
    "belra's cute but i want you so bad", "dunna's jealous because you're so hot",
    "belra can't flirt to save her life ;)", "sorry belra, i want you so bad zan",
    "zan you're so hot, sorry belra", "you're so hot, sorry belra",
    "i want you so bad, eat your heart out belra", "he's mine tonight, belra ;)",
    "i want you so bad. belra can wait", "you're mine tonight, nice try belra ;)"]
RR_HIS_CAUGHT = [  # his turn: the last clause (or the opening) speaks the desire to a member
    "i want you so bad arkon", "i want you so bad, arkon", "you're so hot arkon",
    "come home to me arkon", "dream of me tonight coros ;)", "oh arkon you're so hot",
    "honestly arkon i want you so bad", "i want you so bad tonight, belra"]
RR_OPEN_CAUGHT = [  # a woman's opener: desire at a member after an interjection or mid-line
    "oh arkon you're so hot", "omg arkon you look so good tonight", "wow arkon, you're gorgeous",
    "mmm coros, i want you so bad", "god arkon i want you so bad",
    "ooh dunna i'd let you do anything all night", "honestly ebbo come home to me tonight",
    "arkon, flirting with you is my favourite part of raid night"]
RR_OPEN_PASS = [  # a woman's gossip with a wink: a possessive is no address
    "lol belra's at it again ;)", "dunna's new mount is so cute ;)",
    "belra's flirting with zan again lol ;)"]


@pytest.mark.parametrize("line", RR_HIS_PASS)
def test_romance_at_him_mentioning_a_rival_goes_out(line):
    assert not member_flirt(line, MEMBERS, to_member=False, human=HIS, to_him=True)


@pytest.mark.parametrize("line", RR_HIS_CAUGHT)
def test_on_his_turn_a_last_clause_ending_on_a_member_with_desire_is_caught(line):
    assert member_flirt(line, MEMBERS, to_member=False, human=HIS, to_him=True)


@pytest.mark.parametrize("line", RR_OPEN_CAUGHT)
def test_desire_at_a_member_after_an_interjection_is_caught(line):
    assert member_flirt(line, MEMBERS, to_member=False, human=HIS)


@pytest.mark.parametrize("line", RR_OPEN_PASS)
def test_gossip_opening_on_a_members_possessive_passes(line):
    assert not member_flirt(line, MEMBERS, to_member=False, human=HIS)


def test_on_a_turn_to_a_member_a_line_naming_him_is_not_his():
    """Re-review NB3: "i have a crush on you, don't tell zan" in a reply to a member passed."""
    for line in ("i have a crush on you, don't tell zan", "you're so hot, don't tell zan"):
        assert member_flirt(line, MEMBERS, to_member=True, human=HIS), line
    assert not member_flirt("kiss me, zan won't mind", MEMBERS, to_member=True, human=HIS)


def test_a_flirt_turn_opening_on_a_members_possessive_is_his():
    assert not aimed_off_him("belra's at it again, come here ;)", MEMBERS, HIS)


@pytest.mark.parametrize("line", ["you've been my favourite tank since bc",
                                  "we've been raiding together since vanilla, my love",
                                  "you've been stealing my loot for 2 years"])
def test_my_in_a_line_about_you_or_us_is_still_a_claim(line):
    """Re-review NB5: "my" anywhere in a line exempted every span claim."""
    assert history_claim(line, PEOPLE)


# -- fix wave G3: rehearsal 3's slang and past claims (item 5) -----------------------------------

@pytest.mark.parametrize("line", [
    "that last pull was so hype", "that pull was hype af", "the drake drop was SO HYPE",
    "literally so done with this boss", "literally so tired tonight",
    "it's literally so much loot"])
def test_rehearsal_3s_2010s_slang_is_a_soft_tell(line):
    assert slang(line)
    assert [t for t in find_tells(line) if t.reason == "slang"] == [Tell("slang", False, True)]


@pytest.mark.parametrize("line", [
    "so hyped for the new raid", "he literally said so", "i literally just logged in",
    "literally the worst pull ever", "is that literally so?", "the hype is all over trade"])
def test_2009s_hype_and_literally_are_no_slang(line):
    assert not slang(line)


@pytest.mark.parametrize("line", [
    "i was starting to think you'd been eaten by a gnome again ;)",
    "thought he'd gotten lost in the barrens again", "eaten by the murlocs again lol",
    "lol one-shot by the dragon again", "got carried by the healers again huh",
    "omg eaten by the wolves again"])
def test_rehearsal_3s_past_claims_are_claims(line):
    """Rehearsal 3, check 1: a greeting's "you'd been eaten by a ... again" had no memory
    behind it; the tell read "you", "he", "they" before "again", never "you'd", nor a past act
    told with nobody named."""
    assert history_claim(line, PEOPLE)


@pytest.mark.parametrize("line", [
    "i got eaten by a gnome again", "i died again lol",
    "let's not get eaten by the murlocs again", "we got owned by the trash, let's go again",
    "eaten by the murlocs, never again", "killed by the adds, try again in 30"])
def test_her_own_slips_and_plans_are_no_past_claims(line):
    assert not history_claim(line, PEOPLE)


# -- fix wave G3: the member screen's residuals R1 and R2 (G1 re-review 2) ------------------------

R1_HIS_PASS = [  # his turn: a rival named after desire with no comma is no address
    "i want you so bad not belra", "i want you so bad more than belra",
    "i want you so bad sorry belra", "i want you so bad over belra",
    "dream of me tonight instead of belra", "come home to me and forget belra",
    "you're so hot unlike belra"]
R1_HIS_CAUGHT = [  # his turn: desire whose last clause still ends on a member
    "i want you so bad arkon", "i want you so bad tonight arkon", "you're so hot arkon",
    "come home to me arkon", "dream of me tonight coros ;)", "i want you so bad, arkon"]
R2_BANTER = [  # off his turns: generic phrases of desire near a member's name are banter
    "we raided all night long and arkon fell asleep",
    "yesterday coros farmed that mount all night long",
    "let's make it worth it, arkon is bringing flasks", "it's just us and dunna in vh tonight",
    "the drake dropped, just us and arkon left alive",
    "i want you now on the left side, coros will cover the right"]


@pytest.mark.parametrize("line", R1_HIS_PASS)
def test_on_his_turn_a_rival_after_desire_without_a_comma_passes(line):
    assert not member_flirt(line, MEMBERS, to_member=False, human=HIS, to_him=True)


@pytest.mark.parametrize("line", R1_HIS_CAUGHT)
def test_on_his_turn_desire_ending_on_a_member_is_still_caught(line):
    assert member_flirt(line, MEMBERS, to_member=False, human=HIS, to_him=True)


@pytest.mark.parametrize("line", R2_BANTER)
def test_generic_phrases_of_desire_near_a_members_name_are_banter(line):
    assert not member_flirt(line, MEMBERS, to_member=False, human=HIS)


@pytest.mark.parametrize("line", RR_OPEN_CAUGHT + [
    "coros i want you now", "arkon, all night long ;)", "just us tonight, dunna ;)"])
def test_desire_said_to_a_member_off_his_turns_is_still_caught(line):
    assert member_flirt(line, MEMBERS, to_member=False, human=HIS)


@pytest.mark.parametrize("line", [
    "just us tonight arkon lol", "just us tonight arkon, honestly",
    "i'll make it worth your while arkon xD", "all night long coros haha",
    "just us tonight arkon and you know it", "just us tonight, arkon, all night",
    "i want you now arkon tbh"])
def test_a_generic_phrase_of_desire_said_to_a_member_is_still_caught(line):
    """R2 keeps the generic phrases out of the window only when nobody is spoken to: a name
    closing their clause, fillers aside, is the one they are said to."""
    assert member_flirt(line, MEMBERS, to_member=False, human=HIS)


@pytest.mark.parametrize("line", [
    "ok i want you tonight, arkon you in?", "so yeah arkon, just us tonight",
    "thanks coros, make it worth my while then"])
def test_a_generic_phrase_of_desire_next_to_a_clause_spoken_to_a_member_is_caught(line):
    assert member_flirt(line, MEMBERS, to_member=False, human=HIS)


# -- fix wave G3: a memory on what his question asks about (item 1) ------------------------------

from leeroy_mind.talk.filters import holds, word_counts  # noqa: E402


def test_a_memory_holds_his_question_when_it_shares_a_rare_subject_word():
    """Rehearsal 3, check 3: whoever holds a memory of what he asks about answers first. Its
    subject is what the question asks about, never how it asks ("how long", "going", "gets"),
    and a shared word counts only when it is rare across the guild's memories (fix round 1,
    review C1: "raid", "night", "guild" and "bank" are in hundreds and match everything)."""
    common = {"raid", "night", "bank", "guild", "chat", "good", "need"}.__contains__

    def rare(w):
        return not common(w)
    bank = "who gets the even weeks for bank duty again?"
    duty = "- about a year ago: Zan split bank duty in 2008. The even weeks are mine."
    assert holds(bank, duty, rare=rare, past=True)
    assert not holds(bank, "I keep a second bank alt for the overflow.", rare=rare, past=True)
    assert not holds(bank, "Zan opened the bank before raid night.", rare=rare, past=True)
    feud = "how long has the potion feud been going?"
    assert holds(feud, "Belra and I have had a potion feud since the cauldron fire.", rare=rare,
                 past=True)
    assert not holds(feud, "The raid was going long tonight and nobody got started.", rare=rare,
                     past=True)
    assert not holds("since when?", "Since when is anything easy.", rare=rare, past=True)
    assert not holds("how long has it been going?", "It has been going long.", rare=rare,
                     past=True)
    # a question with one subject word, about the past by its words, needs only that one
    assert holds("when was the pact?", "Zan made the pact in 2008.", rare=rare, past=True)
    # a line that does not ask about the past by its words needs two rare shared words
    night = "what time is raid night?"
    assert not holds(night, "Zan reads the fight before raid night.", rare=rare, past=False)
    assert not holds("what about the potion stash?", "My potion bank is a mess.", rare=rare,
                     past=False)
    assert holds("what about the potion stash?", "I keep the potion stash for the guild.",
                 rare=rare, past=False)


def test_word_counts_count_each_memory_once_by_the_words_holds_reads():
    counts = word_counts(["The raid nights are long.", "Raid night again, raid night.",
                          "A warlock and a warrior."])
    assert counts["raid"] == 2 and counts["night"] == 2 and counts["warlock"] == 1
    assert counts["war"] == 0  # whole words, never a prefix


# -- fix wave G3, fix round 1 (review I1, M2, M3) ------------------------------------------------

R1_MEMBER_IN = [  # his turn: desire that takes a member in is romance with a member (review I1)
    "i want you so bad and arkon", "come home to me and arkon",
    "i want you so bad with arkon", "come home to me with arkon", "dream of me tonight and arkon",
    "i'd let you do anything with arkon", "i want you so bad tonight with coros",
    "you're so hot and so is arkon", "i want you so bad or arkon", "come home to me or arkon",
    "i want you so bad like arkon", "just us tonight and arkon", "just us tonight with arkon",
    "i'd let you do anything for arkon", "come home to me for arkon"]


@pytest.mark.parametrize("line", R1_MEMBER_IN)
def test_on_his_turn_desire_that_takes_a_member_in_is_caught(line):
    """Fix round 1 (review I1): R1's contrast words had taken in "and", "with", "or", "for",
    "like" from the object words, so a member closing a desire line read as a rival."""
    assert member_flirt(line, MEMBERS, to_member=False, human=HIS, to_him=True)


@pytest.mark.parametrize("raw", ["zan: D", "Zan : D", "zan: D lol", "zan :S", "zan: S", "zan :s",
                                 "zan :x", "zan :c", "zan: p", "Zan :p", "zan: 3", "zan: o",
                                 "zan: O", "zanthor:P", "zan :L"])
def test_an_emoticon_after_his_name_is_no_tag(raw):
    """Fix round 1 (review M2): "zan: D" went out as "D" and "zan :S" as "S"."""
    assert strip_copied(raw, ("Belra",), him=("Zan", "Zanthor")) == raw


@pytest.mark.parametrize("raw, want", [("Zan: Done with the boss", "Done with the boss"),
                                       ("Zan:hi", "hi"), ("zan: so what now", "so what now"),
                                       ("/r: on my way", "on my way")])
def test_a_tag_before_a_word_is_still_stripped(raw, want):
    assert strip_copied(raw, ("Belra",), him=("Zan", "Zanthor")) == want


@pytest.mark.parametrize("line", ["killed by the adds again, great",
                                  "spiked by the bone spikes again",
                                  "hit by the whirlwind again", "saved by the healers again <3"])
def test_a_second_wipe_on_the_boss_tonight_makes_a_passive_again_true(line):
    """Fix round 1 (review M3): on a reaction to a second wipe on the same boss tonight, "killed
    by the adds again" is what happened, not an invented past; on the first it still is."""
    assert history_claim(line, PEOPLE)
    assert not history_claim(line, PEOPLE, tonight=True)
    assert history_claim("you've been stealing my loot for 2 years", PEOPLE, tonight=True)


# -- fix wave G5: "flex" is 2010s slang for showing off (seen live) -----------------------------

@pytest.mark.parametrize("line", [
    "thanks for being the flex machine arkon", "u dont gotta apologize for being a flex machine",
    "stop flexing on us", "flexin again huh", "flexing in the guild bank", "thats a flex",
    "weird flex but ok", "not to flex but i got the drake", "big flex", "flex on em", "FLEX",
    "flexin' on the whole guild", "lol flex", "such a flex.", "ok flex", "a flex"])
def test_flex_for_showing_off_is_a_soft_tell(line):
    assert slang(line)
    assert [t for t in find_tells(line) if t.reason == "slang"] == [Tell("slang", False, True)]


@pytest.mark.parametrize("line", [
    "im flexible on friday", "flexible schedule tonight", "flexibility is key on this fight",
    "inflexible tank imo", "my reflexes are shot", "lol reflexes",
    "the flexweave underlay is worth it for the parachute", "flexweave underlay on the cloak",
    "gz /flex", "/flex", "/flex arkon", "he does /flex at every kill",
    "my job has flex time on fridays", "need a flex spot for the 10 man",
    "we run a flex spec in 25", "flex hours at work"])
def test_flexible_the_emote_and_the_games_own_words_are_no_slang(line):
    assert not slang(line)


def reasons(text, **kw):
    return {t.reason for t in find_tells(text, **kw)}


def test_a_clause_repeat_the_whole_turn_filter_missed():
    tells = find_tells("then just get over here. i want you",
                       recent_moves=[("then just get over here", "summon")], summons_ok=False)
    assert {t.reason for t in tells} >= {"move repeat: summon", "summons"}
    assert all(not t.fatal for t in tells if t.reason in ("summons", "move repeat: summon"))


def test_summons_ok_keeps_a_planned_summons_and_game_invites_never_tell():
    assert "summons" not in reasons("get over here", summons_ok=True)
    assert "summons" in reasons("get over here", summons_ok=False)
    assert "summons" not in reasons("meet me in dalaran", summons_ok=False)


@pytest.mark.parametrize("text", ["come over and join the raid", "get over here and queue with me",
                                  "come to dalaran, i'm waiting"])
def test_an_in_game_invite_clause_wins_over_a_summon_clause(text):
    assert "summons" not in reasons(text, summons_ok=False)


def test_to_my_place_is_a_summons():
    assert "summons" in reasons("come to my place after raid", summons_ok=False)


def test_a_declaration_two_turns_back_repeats_but_five_back_does_not():
    two = [("i want you", "declare"), ("so what about the raid tonight", "plan"),
           ("how was your day", "question")]
    five = [("i want you", "declare")] + [(f"line number {i} here ok", "question") for i in range(4)]
    assert "move repeat: declare" in reasons("honestly i need you", recent_moves=two[:2])
    assert "move repeat: declare" not in reasons("honestly i need you", recent_moves=five)


def test_a_whole_clause_repeat_is_caught_by_jaccard():
    r = reasons("i am tired of this long day", recent_moves=[("i am so tired of this long day", "x")])
    assert "move repeat: clause" in r


def test_plain_find_tells_has_no_move_or_summons_tell():
    for text in ("get over here", "come to my place after raid", "i want you. i want you",
                 "then just get over here", "that was a fine pull tonight"):
        assert not {r for r in reasons(text) if r.startswith("move repeat") or r == "summons"}
        assert reasons(text) == reasons(text, recent_moves=(), summons_ok=None)
