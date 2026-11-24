"""The WoW golden ([talk] wow_grounding, talk/grounding.py, and [talk] wow_topic, talk/wowtopic.py;
2026-11-20): the plain tuning scenario over invented raid rows (scenario.wow_rows: a Naxxramas 25
ended three hours before it, a wipe to Frost Blast, Belra's loot, everyone's DKP and attendance,
two days old guild and raid lines of each member, one slang term), recorded in golden_wow.json
(scenario.write_golden_wow) with both switches on, as mind.toml has them by default.

[talk] wow_topic adds the topic hold line to the instruction of each guild answer and follow-on
(32 of the 35 guild prompts; the three greetings are not held) and nothing else: the scenario's
cast has no voice sheet, nobody talks loot in game words and every draft fits her line.
Re-recorded for it with a diff helper (then deleted) that showed exactly that line added to those
32 prompts, no other prompt moving and the same lines sent.

Its public band (2026-11-24, talk/romance.py PUBLIC_BAND: on a game channel others read her
wording goes no further than flirty) moves the band's wording of the 14 guild turns to him that
carry her romance: 6 said the suggestive band's, 8 the explicit overlay's, and all 14 now say the
flirty band's. Re-recorded with a diff helper (then deleted) that showed exactly that sentence
changed in those 14 prompts, nothing else in any prompt and the same lines sent.

What it pins: each guild prompt (35 of the 41) gains the three grounding sections after the
instruction's preamble blocks, in this order: "Raid and guild (facts; ...)" with her class and
role, the last raid with her seat, its last boss with its playbook line for her role, the last
wipe's deaths and her DKP (and Belra's won item on hers); "How you talk in raid/guild (your own
past lines):" with her lines; and the "Raid words: ..." glossary with the slang. Nothing else in
any prompt moves: with the sections taken out every prompt is golden_prompts.json's, and the six
prompts of the scenario's whispers (his "hey you" to Belra and Dunna's evening whisper, each with
its retry) are byte for byte golden_prompts.json's. The lines sent are the same. The rows alone,
with the switch off, record golden_prompts.json exactly."""
import pytest

from engine_rig import OVERLAY
from leeroy_mind.talk import grounding as g
from leeroy_mind.talk import wowtopic
from leeroy_mind.talk.romance import BUILTIN_ROMANCE
from tuning.scenario import engine_clock, golden, golden_wow, record_wow


@pytest.fixture
def clock():
    return engine_clock()


def test_grounding_on_matches_the_recorded_wow_golden(store, clock, tmp_path):
    assert record_wow(store, clock, tmp_path) == golden_wow()


def test_the_rows_alone_change_no_prompt(store, clock, tmp_path):
    assert record_wow(store, clock, tmp_path, grounding=False) == {
        "prompts": golden()["prompts"], "sent": golden()["sent"]}


def _channels(sent, n):
    """Each prompt's channel: the scenario's script numbers its replies by prompt, and a prompt
    whose reply was not sent (a retry's first draft) is the turn of the next sent one."""
    by_n = {int(text.split()[3]): channel for _, channel, text in sent}
    return [by_n[min(k for k in by_n if k >= i + 1)] for i in range(n)]


HOLD = " " + wowtopic.hold_text("guild")  # always after a word of the instruction
FLIRTY = BUILTIN_ROMANCE["flirty"]
ABOVE = (BUILTIN_ROMANCE["suggestive"], OVERLAY)  # the bands the public band brings down


def public(plain):
    """`plain` (a prompt of golden_prompts.json) with her band's wording brought down to the
    public band's, as wow_topic does on a guild turn to him."""
    for band in ABOVE:
        plain = plain.replace(band, FLIRTY)
    return plain


def strip(prompt):
    """A prompt without its grounding sections and without the topic hold line."""
    return "\n\n".join(block for block in prompt.replace(HOLD, "").split("\n\n")
                       if not block.startswith((g.CARD_HEAD, g.EXEMPLAR_HEAD, "Raid words: ")))


def test_only_the_guild_prompts_gain_the_sections_and_nothing_else_moves():
    wow, plain = golden_wow(), golden()
    assert wow["sent"] == plain["sent"] and len(wow["prompts"]) == len(plain["prompts"])
    channels = _channels(plain["sent"], len(plain["prompts"]))
    assert channels.count("whisper") == 6 and set(channels) == {"guild", "whisper"}
    for channel, on, off in zip(channels, wow["prompts"], plain["prompts"]):
        if channel == "whisper":
            assert on == off  # a whisper's prompt is byte for byte what it was
            continue
        assert on != off and strip(on) == public(off)
        assert g.CARD_HEAD in on and g.GLOSSARY in on
        assert on.index(g.CARD_HEAD) < on.index(g.GLOSSARY)
        greeting = "just logged on. Greet" in on
        assert (HOLD in on) is not greeting  # every guild answer is held, no greeting is
        assert wowtopic.DKP_RULE not in on
    capped = [i for i, (c, off) in enumerate(zip(channels, plain["prompts"]))
              if c == "guild" and public(off) != off]
    assert len(capped) == 14
