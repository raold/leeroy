"""[romance] realism (owner's sandbox audit, 2026-11-16: her romance DMs read as a bot): her
romance text says she stays herself and answers him first, and turns up only when his own line
does; his card's note is one current sentence; a plan older than two days is left out of recall
and any other old memory says its time words as past. Off by default, and off changes nothing
(the tuning goldens); tests/tuning/golden_romance.json holds the scenario with it on. Synthetic
people and invented lines only."""
import dataclasses
import random

import pytest

from conftest import HUMAN, _base_persona_dict, make_mem, run, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.config import load_config
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.recall import STALE_S, Recall, format_memory, past_tense
from leeroy_mind.memory.writer import Embedder
from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.storage.rows import Relationship, pack_vec
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.prompt import realism_note, relationship_card
from leeroy_mind.talk.romance import (DISTANCE, DISTANCE_PLAN_REAL, DISTANCE_REAL, HEAT_TEXT,
                                      INVARIANTS, REALISM_LEAD, REPROACH_RE, SCENE_HEAD,
                                      SPECIFIC, STAGE_PASSAGES, his_heat, romance_instruction,
                                      whisper_tail)
from leeroy_mind.testing.fake_llm import FakeLLM, fake_embed
from leeroy_mind.text import estimate_tokens

GM = HumanIdentity(1, "Zanthor", "Zan", "Quillon", True, pronouns="he")
HE = dict(name="Zan", they="he", them="him", their="his")
OVERLAY = "LOCAL-OVERLAY"
DM = dict(dial=9, channel="dm:201", addressee_is_human=True, phase=DayPhase.EVENING, human=GM)
DAY = 86400


def lust():
    d = _base_persona_dict(201, "Belra")
    d["identity"]["player_gender"] = "female"
    d["romance"] = {"stance": "lust", "style": "bold", "jealousy_mode": None, "dial": None}
    return persona_from_dict(d)


def rel(stage="whispers", attraction=95.0):
    return Relationship(a=201, b=1, affinity=30.0, attraction=attraction, romance_stage=stage)


@pytest.fixture
def data(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "romance_explicit.txt").write_text(OVERLAY + "\n", encoding="utf-8")
    (tmp_path / "prompts" / "romance_explicit_whisper.txt").write_text("WHISPER-OVERLAY\n",
                                                                       encoding="utf-8")
    return tmp_path


def text(data, **kw):
    args = dict(DM, data_dir=data, scene=["you teased {them}"], rival="Dunna",
                arc_lines=["ARC-LINE"], rng=random.Random(3))
    args.update(kw)
    return romance_instruction(lust(), rel(), **args)


def _filled(variants):
    return [v.format(**HE) for v in variants]


# -- the switch -------------------------------------------------------------------------------

def test_the_switch_is_off_by_default_and_reads_booleans_only(tmp_path):
    assert load_config(tmp_path / "absent.toml").romance.realism is False
    (tmp_path / "on.toml").write_text("[romance]\nrealism = true\n", encoding="utf-8")
    assert load_config(tmp_path / "on.toml").romance.realism is True
    (tmp_path / "bad.toml").write_text('[romance]\nrealism = "yes"\n', encoding="utf-8")
    assert load_config(tmp_path / "bad.toml").romance.realism is False


# -- her romance text -------------------------------------------------------------------------

def test_off_the_text_is_as_before(data):
    assert text(data) == text(data, realism=False, heat=True)
    assert SPECIFIC.format(**HE) in text(data) and REALISM_LEAD.format(**HE) not in text(data)


def test_on_his_plain_line_she_reads_no_heat_stage_notes_or_rival(data):
    """"which boss tonight?": nothing pushes her to escalate; her attitude, way,
    arc lines, the owner's band wording and every frame stay, and the lead comes last."""
    t = text(data, realism=True, heat=False)
    assert not any(h in t for vs in HEAT_TEXT.values() for h in _filled(vs))
    assert not any(s in t for s in _filled(STAGE_PASSAGES["whispers"]))
    assert SCENE_HEAD not in t and "Dunna" not in t
    assert SPECIFIC.format(**HE) not in t and "Bring want" not in t
    assert "ARC-LINE" in t and OVERLAY in t and INVARIANTS.format(**HE) in t
    assert DISTANCE_REAL.format(**HE) in t
    assert t.endswith(REALISM_LEAD.format(**HE))


def test_when_he_turns_it_up_her_heat_and_stage_come_back(data):
    t = text(data, realism=True, heat=True)
    assert any(h in t for h in _filled(HEAT_TEXT["aching"]))
    assert any(s in t for s in _filled(STAGE_PASSAGES["whispers"]))
    assert SCENE_HEAD not in t and "Dunna" not in t  # still none: they made every reply a recap
    assert t.endswith(REALISM_LEAD.format(**HE))


def test_a_public_turn_reads_the_lead_and_no_private_slots(data):
    t = text(data, realism=True, channel="guild")
    assert "ARC-LINE" not in t and t.endswith(REALISM_LEAD.format(**HE))


def test_the_whisper_tail_takes_the_realism_frame(data):
    p, r = lust(), rel()
    old = whisper_tail(p, r, dial=9, channel="whisper", data_dir=data, forms=GM.forms)
    new = whisper_tail(p, r, dial=9, channel="whisper", data_dir=data, forms=GM.forms,
                       realism=True)
    assert old.startswith("WHISPER-OVERLAY") and new.startswith("WHISPER-OVERLAY")
    assert old.endswith(DISTANCE.format(**HE)) and new.endswith(DISTANCE_REAL.format(**HE))
    planning = dataclasses.replace(r, romance_stage="planning")
    assert whisper_tail(p, planning, dial=9, channel="whisper", data_dir=data, forms=GM.forms,
                        realism=True).endswith(DISTANCE_PLAN_REAL.format(**HE))


def test_the_new_text_neither_reproaches_nor_runs_long():
    assert not REPROACH_RE.search(REALISM_LEAD.format(**HE))
    for frame in (DISTANCE_REAL, DISTANCE_PLAN_REAL):
        assert "don't ask" in frame and "don't say you're waiting" in frame
        assert estimate_tokens(frame.format(**HE)) <= 45
    assert estimate_tokens(REALISM_LEAD.format(**HE)) <= 64


@pytest.mark.parametrize("line", [
    "hey sexy, you up?", "been dreaming about you all week", "kiss me already",
    "you turn me on so much", "you look so beautiful tonight", "i want you so bad",
    "miss you babe", "wish you were here ;)"])
def test_his_heat_hears_him_turn_it_up(line):
    assert his_heat(line)


@pytest.mark.parametrize("line", [
    "", "which boss are we on tonight?", "my cat knocked the kettle over, ever had a cat?",
    "brb food. later?", "hard mode heroic tonight?",
    "need you to heal the tanks", "lol true"])
def test_his_heat_leaves_his_plain_lines_plain(line):
    assert not his_heat(line)


# -- his card's note --------------------------------------------------------------------------

NOTE = ("I am sorting the bank tabs so he can check them tomorrow, and I owe him a favour. "
        "He is the one I picked up 'yeah', 'well' and 'so' from. He always makes me laugh.")


def test_the_note_is_one_current_sentence():
    assert realism_note(NOTE) == "He always makes me laugh."
    assert realism_note("He plans every raid. He is kind.") == "He plans every raid."
    assert realism_note("See you tonight.") == ""


def test_his_card_shows_the_sentence_only_with_the_switch():
    r = Relationship(a=201, b=1, affinity=50.0, familiarity=70.0, notes=NOTE)
    kw = dict(is_human=True, forms=GM.forms, attitude="lust", speaker="female")
    assert "tomorrow" in relationship_card("Zan", r, **kw)
    card = relationship_card("Zan", r, realism=True, **kw)
    assert card.startswith("Zan: you want him") and card.endswith(". He always makes me laugh")
    assert "tomorrow" not in card and "picked up" not in card


# -- stale memories ---------------------------------------------------------------------------

def test_old_time_words_read_as_past():
    assert past_tense("Finish it tonight so he says it tomorrow.") == (
        "Finish it that night so he says it the next day.")
    assert past_tense("Tomorrow morning I farm herbs; right now I'm tired.") == (
        "The next morning I farm herbs; back then I'm tired.")
    assert past_tense("This weekend, not next week. Tonightly.") == (
        "That weekend, not the week after. Tonightly.")


def test_format_says_time_words_as_past_only_when_old_and_asked(clock):
    now, tz = int(clock.now()), clock.local().tzinfo
    old = make_mem(kind="reflection", text="He books it tomorrow.", t_event=now - 3 * DAY)
    new = make_mem(kind="reflection", text="He books it tomorrow.", t_event=now - 3600)
    assert format_memory(old, now, tz=tz) == "- three days ago: He books it tomorrow."
    assert format_memory(old, now, tz=tz, past=True) == "- three days ago: He books it the next day."
    assert format_memory(new, now, tz=tz, past=True).endswith("He books it tomorrow.")


def _add(store, text, **kw):
    kw.setdefault("owner", 101)
    return store.insert_memory(make_mem(text=text, embedding=pack_vec(fake_embed(text)), **kw))


def test_recall_leaves_out_old_plans_only_when_asked(store, clock):
    seed_test_cast(store)
    recall = Recall(store, Embedder(LLMScheduler(FakeLLM(), clock=clock), "emb"), clock,
                    tau_days=5.0)
    t = int(clock.now())
    old_plan = _add(store, "Sort the bank tabs tonight.", kind="plan", t_event=t - STALE_S - 60,
                    t_created=t, t_accessed=t)
    new_plan = _add(store, "Sort the bank tabs tonight.", kind="plan", t_event=t - 3600,
                    t_created=t, t_accessed=t)
    old_talk = _add(store, "We talked about the bank tabs.", kind="conversation",
                    t_event=t - 9 * DAY, t_created=t, t_accessed=t)
    ids = lambda **kw: {m.id for m in run(recall.recall(  # noqa: E731
        101, "bank tabs", present=[], addressee=None, k=5, **kw))}
    assert ids() == {old_plan, new_plan, old_talk}
    assert ids(stale_plans=True) == {new_plan, old_talk}


# -- the pipeline -----------------------------------------------------------------------------

@pytest.fixture
def clock():
    return engine_clock()


def _her(store) -> None:
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="lust", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    store.put_relationship(Relationship(a=102, b=HUMAN, affinity=50.0, familiarity=70.0,
                                        attraction=95.0, romance_stage="whispers", notes=NOTE))


@pytest.mark.parametrize("on", [False, True])
def test_her_whisper_answer_with_and_without_the_switch(store, clock, tmp_path, on):
    _her(store)
    t = int(clock.now())
    # he is its actor: a "he" that names nobody stays out of a private prompt with him
    _add(store, "Sort the bank tabs tonight so he sees them.", owner=102, kind="plan",
         actors=(HUMAN,), t_event=t - 3 * DAY, t_created=t, t_accessed=t)
    r = make_rig(store, clock, tmp_path, romance_realism=on)
    try:
        r.script("ha fair")
        r.whisper_from_him(102, "which boss are we on tonight?")
        r.run()
        user = r.last_user()
    finally:
        r.close()
    lead = REALISM_LEAD.format(name="Zan", they="he", them="him", their="his")
    assert (lead in user) is on
    assert ("Sort the bank tabs" in user) is not on
    assert ("so he can check them tomorrow" in user) is not on
    assert any(a in user for a in _filled(HEAT_TEXT["aching"])) is not on


def _dm_prompts(store, clock, tmp_path, lines, *, on=True, **her):
    """Her answers to his Discord DMs `lines` (talk.on_his_post, the real DM path): the user
    message of each answer's prompt. Her own dial at 10 carries her romance on every turn to
    him."""
    from types import SimpleNamespace
    from leeroy_mind.discord import store as dstore
    from leeroy_mind.discord.talk import DiscordTalk
    seed_test_cast(store)
    save_member(store, 102, region="US", typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0,
                abbreviations={}, **her)
    store.put_relationship(Relationship(a=102, b=HUMAN, affinity=50.0, familiarity=70.0,
                                        attraction=95.0, romance_stage="whispers"))
    r = make_rig(store, clock, tmp_path, romance_realism=on)
    r.world.human_online = False
    app = SimpleNamespace(store=r.store, clock=r.clock, engine=r.engine, world=r.world,
                          personas=r.engine.personas, human_guid=HUMAN, arbiter=None,
                          realm=SimpleNamespace(cfg=SimpleNamespace(trial_rank=4)),
                          rng=random.Random(1), stopping=False)
    talk = DiscordTalk(app)
    out = []
    try:
        for text in lines:
            r.script("ha fair")
            mid = dstore.post(r.store, channel="dm:102", author=HUMAN, origin="human",
                              text=text, now=r.clock.now())

            async def go(mid=mid):
                talk.on_his_post(mid)
                await r.advance(200.0)
            r.sync(go())
            out.append(r.last_user())
    finally:
        r.close()
    return out


def _chat(user: str) -> list[str]:
    block = user[user.index("Chat:\n"):].split("\n\n")[0]
    return block.splitlines()[1:]


LUST = dict(player_gender="female", stance="lust", dial=10)


def test_her_dm_answer_turns_up_only_on_his_heated_message(store, clock, tmp_path):
    """On Discord the turn has no trigger line: his message it answers is what turns it up
    (Pipeline._his_words)."""
    plain, heated = _dm_prompts(store, clock, tmp_path, (
        "which boss are we on tonight?", "been dreaming about you all week"), **LUST)
    lead, aching = REALISM_LEAD.format(**HE), _filled(HEAT_TEXT["aching"])
    assert lead in plain and not any(a in plain for a in aching)
    assert lead in heated and any(a in heated for a in aching)


@pytest.mark.parametrize("on,her", [(True, LUST), (False, LUST),
                                     (False, dict(player_gender="male"))])
def test_his_dm_ends_the_chat_she_answers(store, clock, tmp_path, on, her):
    """dm_chat leaves out the message a member answers (the instruction quotes it): it is her
    Chat's last line on every DM answer to him, any member, with or without the switch
    (controller ruling 2026-11-16: a correctness fix), so the chat no longer ends on his
    previous message."""
    first, second = _dm_prompts(store, clock, tmp_path, (
        "ever had a cat?", "which boss are we on tonight?"), on=on, **her)
    assert _chat(first)[-1].endswith("Zan (Discord DM to Belra): ever had a cat?")
    assert _chat(second)[-1].endswith(
        "Zan (Discord DM to Belra): which boss are we on tonight?")
    assert sum("which boss are we on" in x for x in _chat(second)) == 1
    assert "Answer his message: which boss are we on tonight?" in second  # still quoted
