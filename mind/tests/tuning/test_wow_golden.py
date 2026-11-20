"""The WoW golden ([talk] wow_grounding, talk/grounding.py; 2026-11-20): the plain tuning scenario
over invented raid rows (scenario.wow_rows: a Naxxramas 25 ended three hours before it, a wipe to
Frost Blast, Belra's loot, everyone's DKP and attendance, two days old guild and raid lines of
each member, one slang term), recorded in golden_wow.json (scenario.write_golden_wow).

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

from leeroy_mind.talk import grounding as g
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


def strip(prompt):
    """A prompt without its grounding sections."""
    return "\n\n".join(block for block in prompt.split("\n\n")
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
        assert on != off and strip(on) == off
        assert g.CARD_HEAD in on and g.GLOSSARY in on
        assert on.index(g.CARD_HEAD) < on.index(g.GLOSSARY)
