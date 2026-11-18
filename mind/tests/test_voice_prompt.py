"""A member's voice sheet in the live prompt (2026-11-17): the block sits just before the
instruction in both prompt modes, is never cut, takes the place of the trait sentences, the
canned attachment and humour lines and the generator's sample lines, shows how she is with him
only on his turns and her stance only where only he reads, and tags the line's recipe. A member
without a sheet composes exactly what she did (the four goldens in tests/tuning hold that).
Every sheet here is invented (tests/sheets.py)."""
import random
from dataclasses import replace

import pytest

from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.human import HumanIdentity
from leeroy_mind.learn.recipe import Snapshot, voice_used
from leeroy_mind.personas import speech as sp
from leeroy_mind.personas.schema import Typing
from leeroy_mind.protocol import Group
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.prompt import (LIVE_BUDGET, SAMPLES_HEAD, PromptParts, redact_for,
                                     render_messages, section_tokens, trim_to_budget)
from leeroy_mind.talk.shape import (MIN_LINE_WORDS, Shape, ShapeContext, dm_words_for,
                                    draw_shape)
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import estimate_tokens
from sheets import GRUFF, BRIGHT, give, sheet

BLOCK = sp.voice_block(sheet(), "Zan", to_him=True, private=True,
                       samples=["depends. which boss was it"])


def parts(**over):
    base = dict(card="You are Jo.", samples=[], rules="How you talk here:\n- be yourself",
                rel_cards=["Zan: you like him"], memories=["- a memory"], mood="It's evening.",
                chat=["Zan: hey"], own_recent=["earlier line"], instruction="Answer Zan's last line.",
                awareness="AWARE CARD", voice=BLOCK)
    return PromptParts(**{**base, **over})


# -- where it sits ---------------------------------------------------------------------------------

@pytest.mark.parametrize("situation", [False, True])
def test_the_block_is_the_last_thing_before_the_instruction_in_both_modes(situation):
    system, user = (m["content"] for m in render_messages(parts(situation=situation)))
    assert BLOCK not in system
    assert user.endswith(BLOCK + "\n\nAnswer Zan's last line.")
    before = user[:user.index(BLOCK)].rstrip()
    assert before.endswith("Right now:\nIt's evening." if situation else "AWARE CARD")


def test_without_a_sheet_the_messages_are_what_they_were():
    plain = parts(voice="")
    assert "Your voice:" not in str(render_messages(plain))
    assert "voice" not in section_tokens(plain)
    assert section_tokens(parts())["voice"] == estimate_tokens(BLOCK)


def test_the_trim_never_cuts_her_voice_and_counts_it():
    big = parts(memories=[f"- memory {i} " + "m" * 200 for i in range(8)],
                chat=[f"Zan: line {i} " + "c" * 300 for i in range(20)],
                own_recent=["o" * 300] * 6)
    trimmed = trim_to_budget(big, budget=LIVE_BUDGET)
    assert trimmed.voice == BLOCK and BLOCK in render_messages(trimmed)[1]["content"]
    total = sum(estimate_tokens(m["content"]) for m in render_messages(trimmed))
    assert total <= LIVE_BUDGET
    without = trim_to_budget(replace(big, voice=""), budget=LIVE_BUDGET)
    assert len(without.chat) >= len(trimmed.chat)  # her voice takes room the chat gives up


def test_his_real_name_is_redacted_in_the_block_too():
    human = HumanIdentity(1, "Testmage", "Zan", "Quentin", True, "he")
    named = parts(voice="With Zan: calls him Quentin when cross.")
    assert "Quentin" not in redact_for(named, human, False).voice
    assert redact_for(named, human, True).voice == named.voice


# -- composed by the pipeline ----------------------------------------------------------------------

@pytest.fixture
def rig(store, tmp_path):
    r = make_rig(store, engine_clock(), tmp_path, reply="the ferry leaves at seven")
    give(store, 101)                                   # a man with a sheet
    give(store, 102, BRIGHT, player_gender="female", stance="drawn", style="bold")
    r.engine.personas.invalidate()
    r.world.apply(Group(0, 1, True, (1, 101, 102, 103)), r.clock.now())
    yield r
    r.close()


def turn(guid, channel="guild", kind=TurnKind.REPLY_HUMAN, **kw):
    private = channel == "whisper" or channel.startswith("dm:")
    return Turn(kind, guid, channel, to_guid=1 if private else 0, addressee=1, **kw)


def composed(rig, t):
    messages, _ = rig.sync(rig.engine.compose(t))
    return messages[0]["content"], messages[1]["content"]


def test_a_member_with_a_sheet_reads_it_by_the_instruction(rig):
    system, user = composed(rig, turn(101))
    voice = "Your voice: plain, clipped, a bit gruff; short flat statements, three to seven words."
    assert voice in user and voice not in system
    tail = user[user.index(voice):]
    assert "With Zan: ribs him over his sums." in tail       # the turn is his
    assert "content with it" not in tail                               # but the guild reads it too
    assert sp.SAMPLES_HEAD in tail and tail.count("\n- ") in (1, 2)
    assert tail.rstrip().splitlines()[-1].startswith("Answer Zan's last line")
    # in place of the trait sentences, the canned mind line and the generator's samples
    assert "chats easily in guild" not in system and "Your humour is" not in system
    assert "You trust people easily" not in system and SAMPLES_HEAD not in system
    assert "You are Jo, a 34-year-old man" in system            # her facts stay


def test_a_member_without_one_keeps_her_card_and_samples(rig):
    system, user = composed(rig, turn(103))
    assert "Your voice:" not in user and sp.SAMPLES_HEAD not in user
    assert "chats easily in guild" in system and SAMPLES_HEAD in system


@pytest.mark.parametrize("channel,with_him,stance", [
    ("guild", True, False), ("party", True, False), ("raid", True, False),
    ("whisper", True, True), ("dm:102", True, True), ("discord:general", True, False)])
def test_stance_only_where_only_he_reads(rig, channel, with_him, stance):
    _, user = composed(rig, turn(102, channel))
    assert ("With Zan: flirts by teasing, then changes the subject." in user) is with_him
    assert ("seeing nobody, and cheerful about it" in user) is stance


def test_a_turn_that_is_not_his_shows_neither_and_never_the_flirt_sample(rig):
    for kind, addressee in ((TurnKind.CONTINUE, 101), (TurnKind.OPEN, 0)):
        t = Turn(kind, 102, "guild", addressee=addressee)
        _, user = composed(rig, t)
        assert "Your voice: bubbly, scattered, quick to laugh" in user
        assert "With Zan" not in user and "seeing nobody" not in user
        assert BRIGHT["samples"]["flirt"] not in user


def test_the_samples_follow_the_turns_want_and_rotate_by_the_minute(rig):
    e = rig.engine

    def shown(want, minutes=0):
        if minutes:
            rig.sync(rig.advance(60 * minutes, step=60))
        meta = TurnMeta(turn_id="")
        meta.intent = want
        c = rig.sync(e._compose(turn(102, "whisper"), meta, live=False))
        return c.samples
    first = shown("tease")
    assert first[0] == BRIGHT["samples"]["tease"] and len(first) == 2
    assert shown("tease") == first                       # the same turn, the same samples
    assert shown("flirt")[0] == BRIGHT["samples"]["flirt"]
    assert shown("boundary")[0] == BRIGHT["samples"]["annoyed"]
    assert shown("")[0] == BRIGHT["samples"]["answer"]
    seconds = {shown("tease", 1)[1] for _ in range(12)}
    assert len(seconds) >= 2                             # not the same second sample every turn


def test_a_sample_she_already_sent_leaves_the_prompt(rig):
    e = rig.engine
    said = GRUFF["samples"]["answer"]
    e._own_of(101).append(("guild", 0, said))
    for minute in range(6):
        rig.sync(rig.advance(60, step=60))
        _, user = composed(rig, turn(101))
        block = user[user.index("Your voice:"):]
        assert "- " + said not in block


def test_the_shown_samples_are_the_ones_the_copy_screen_reads(rig):
    c = rig.sync(rig.engine._compose(turn(101), TurnMeta(turn_id=""), live=False))
    assert c.samples and all("- " + s in c.messages[1]["content"] for s in c.samples)
    assert set(c.samples) <= set(GRUFF["samples"].values())


def test_the_sheet_costs_at_most_about_eighty_tokens_net(rig, store):
    """The block takes the place of the trait sentences, the mind line and the sample section:
    the same turn for the same member with and without her sheet, her stored persona sized like
    the live cast's (two trait sentences of about 120 characters, five sample lines)."""
    sized = dict(
        extraversion={"score": 8, "behaviour": "talks over others in chat and starts the "
                      "voice calls, fills every silence with a running commentary on the pull"},
        neuroticism={"score": 1, "behaviour": "never rattled by a wipe, shrugs it off and "
                     "calls the next pull before anyone has finished complaining about it"},
        sample_lines=[{"text": f"hots are up, go go, number {i} and counting down now",
                       "approved": True} for i in range(5)])

    def size(t):
        return sum(estimate_tokens(m) for m in composed(rig, t))
    sizes = {}
    for label in ("with", "without"):
        if label == "with":
            give(store, 101, **sized)
            give(store, 102, BRIGHT, player_gender="female", stance="drawn", style="bold", **sized)
        else:
            save_member(store, 101, **sized)
            save_member(store, 102, player_gender="female", stance="drawn", style="bold", **sized)
        rig.engine.personas.invalidate()
        sizes[label] = [size(turn(101)), size(turn(102, "whisper")), size(turn(102, "dm:102"))]
    for a, b in zip(sizes["with"], sizes["without"]):
        assert a - b <= 80
    # and whatever the persona it replaces, the section itself is bounded
    _, user = composed(rig, turn(102, "whisper"))
    assert user  # (the "without" personas are stored now)
    worst = sp.BLOCK_TOKENS + estimate_tokens(sp.SAMPLES_HEAD) + sp.MAX_SAMPLES * (sp.SAMPLE_TOKENS + 1)
    assert worst <= 260


# -- the recipe ------------------------------------------------------------------------------------

def test_a_line_whose_prompt_carried_a_sheet_is_tagged_once():
    meta = TurnMeta(turn_id="")
    meta.recipe_snapshot = Snapshot("talk.instruction.reply_human", "abc", "d", "m")
    voice_used(meta, sp.RECIPE_TAG)
    tagged = meta.recipe_snapshot
    assert tagged.template_id == "talk.instruction.reply_human+voice.sheet-1"
    assert tagged.template_version != "abc" and tagged.model_id == "m"
    voice_used(meta, sp.RECIPE_TAG)  # a guard's retry composes again
    assert meta.recipe_snapshot == tagged
    bare = TurnMeta(turn_id="")
    voice_used(bare, sp.RECIPE_TAG)  # no snapshot: nothing to tag
    assert bare.recipe_snapshot is None


def test_only_a_live_learning_turn_of_a_member_with_a_sheet_is_tagged(rig):
    e = rig.engine
    e.learn_enabled = True

    def tag(guid, live):
        meta = TurnMeta(turn_id="x")
        meta.recipe_snapshot = Snapshot("talk.instruction.reply_human", "abc", "d", "m")
        e._voice(turn(guid), meta, e.personas.get(guid), live=live)
        return "voice.sheet-1" in meta.recipe_snapshot.template_id
    assert tag(101, True) and not tag(101, False) and not tag(103, True)


# -- her line length -------------------------------------------------------------------------------

T = Typing(case="lower", end_punct="none", emoticons=(), emoticon_rate=0.0, abbreviations={},
           typo_rate=0.0, words_per_line=(8, 14), split_rate=0.3, chars_per_sec=5.0)
GAME = ShapeContext(family="game", kind=TurnKind.CONTINUE, owed=False, intent=None, typing=T,
                    attention=None, state=None, late_by=0.0, his_words=12, discord_words=90,
                    dm_words=200, dm_mirror=5, realism=True, two_lines=False)


def _short(ctx, n=400):
    return [d for d in (draw_shape(ctx, random.Random(i)) for i in range(n))
            if d.shape is Shape.SHORT]


def test_her_shapes_range_moves_only_the_word_draw_of_her_own_line():
    plain, voiced = _short(GAME), _short(replace(GAME, voice_words=(2, 6)))
    assert all(8 <= d.words <= 14 for d in plain) and all(2 <= d.words <= 6 for d in voiced)
    # the same seeds draw the same shapes: the odds never move
    assert [draw_shape(GAME, random.Random(i)).shape for i in range(300)] == [
        draw_shape(replace(GAME, voice_words=(2, 6)), random.Random(i)).shape for i in range(300)]


def test_an_owed_line_keeps_its_floor_whatever_her_shape_says():
    owed = replace(GAME, family="whisper", kind=TurnKind.REPLY_HUMAN, owed=True, voice_words=(1, 2))
    drawn = [draw_shape(owed, random.Random(i)) for i in range(300)]
    assert all(d.words >= MIN_LINE_WORDS for d in drawn if d.shape in (Shape.SHORT, Shape.QUESTION))
    assert not {d.shape for d in drawn} & {Shape.LEAVE, Shape.REACTION, Shape.ONE_WORD}


def test_the_dm_mirror_keeps_its_formula_and_its_draws():
    assert dm_words_for(28, T, 0, 200, random.Random(3), habit=(2, 6)) == 200  # mirror 0: today's
    usual = [dm_words_for(10, T, 5, 200, random.Random(i)) for i in range(500)]
    terse = [dm_words_for(10, T, 5, 200, random.Random(i), habit=(2, 6)) for i in range(500)]
    assert sum(terse) < sum(usual) and all(3 <= w <= 200 for w in terse)
    same = [dm_words_for(10, T, 5, 200, random.Random(i), habit=(8, 14)) for i in range(500)]
    assert same == usual                          # her typing's own range: nothing moves
    a, b = random.Random(9), random.Random(9)     # and the stream is read the same either way
    dm_words_for(30, T, 5, 200, a)
    dm_words_for(30, T, 5, 200, b, habit=(2, 6))
    assert a.random() == b.random()
    dm = replace(GAME, family="dm", kind=TurnKind.REPLY_HUMAN, owed=True, his_words=10)
    full = replace(dm, dm_mirror=0)
    assert draw_shape(replace(full, voice_words=(2, 6)), random.Random(1)) == draw_shape(
        full, random.Random(1))


# -- review of #367 ----------------------------------------------------------------------------------

def test_no_stance_in_a_group_dm_or_a_whisper_between_members(rig):
    for t in (Turn(TurnKind.REPLY_HUMAN, 102, "gdm:7", addressee=1),
              Turn(TurnKind.REPLY_HUMAN, 102, "gdm:7", to_guid=1, addressee=1),
              Turn(TurnKind.CONTINUE, 102, "whisper", to_guid=101, addressee=101),
              Turn(TurnKind.REPLY_HUMAN, 102, "officer", addressee=1),
              Turn(TurnKind.REPLY_HUMAN, 102, "voice:raid", addressee=1)):
        _, user = composed(rig, t)
        assert "Your voice: bubbly, scattered, quick to laugh" in user
        assert "seeing nobody, and cheerful about it" not in user
    _, user = composed(rig, Turn(TurnKind.CONTINUE, 102, "whisper", to_guid=101, addressee=101))
    assert "With Zan" not in user  # a whisper to another member is not his turn
    system, other = composed(rig, turn(103, "whisper"))
    assert "bubbly" not in system + other and "gruff" not in system + other  # nobody else's sheet


def test_a_stance_written_for_another_household_stays_out_of_her_prompt(rig, store):
    _, user = composed(rig, turn(102, "whisper"))
    assert "seeing nobody, and cheerful about it" in user
    p = rig.engine.personas.base(102)
    from leeroy_mind.personas.io import save_persona
    save_persona(store, replace(p, real_life=replace(p.real_life, household="married, two kids")),
                 source="owner", ts=2000.0, race="Night Elf", class_name="Druid")
    rig.engine.personas.invalidate()
    system, user = composed(rig, turn(102, "whisper"))
    assert "married, two kids" in system
    assert "seeing nobody" not in system + user
    assert "With Zan: flirts by teasing, then changes the subject." in user  # the rest stays


def test_a_persona_with_a_sheet_never_composes_the_trait_sentences(rig, store):
    give(store, 101, extraversion={"score": 6, "behaviour": "TRAITONE talks over everyone"},
         neuroticism={"score": 1, "behaviour": "TRAITTWO never rattled"})
    rig.engine.personas.invalidate()
    for channel in ("guild", "whisper", "dm:101"):
        system, user = composed(rig, turn(101, channel))
        assert "TRAITONE" not in system + user and "TRAITTWO" not in system + user
        assert "Your humour is" not in system and "Opinions:" in system
