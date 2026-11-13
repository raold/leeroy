import logging
from dataclasses import replace

from leeroy_mind.llm.presets import LIVE
from leeroy_mind.talk.prompt import LIVE_BUDGET, section_tokens, trim_to_budget
from test_prompt import parts, tokens
from leeroy_mind.talk.prompt import render_messages


def heavy_whisper():
    # Counts from the audit: fixed instruction can reach ~480, card 165,
    # samples 139 and rules 219, before the optional memory/chat floors.
    return parts(card="C" * 660, samples=["S" * 100] * 5, rules="R" * 876,
                 instruction="I" * 1912, memories=["M" * 160] * 3,
                 own_recent=["O" * 160] * 3, chat=["Q" * 80] * 5)


def test_fixed_whisper_instruction_fits_with_memory_and_own_line_floors():
    p = heavy_whisper()
    old = trim_to_budget(p, budget=1300)
    assert len(old.memories) < 3 or len(old.own_recent) < 3
    actual = trim_to_budget(p)
    assert actual == p
    assert tokens(render_messages(actual)) <= LIVE_BUDGET
    assert LIVE.options["num_ctx"] - LIVE_BUDGET - LIVE.options["num_predict"] == 2216


def test_under_old_budget_is_byte_identical():
    p = parts()
    assert trim_to_budget(p, budget=1300) == trim_to_budget(p) == p


def test_warning_reports_every_rendered_section_as_counts_without_text(caplog):
    p = replace(heavy_whisper(), instruction="PRIVATE_SENTINEL " * 600)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        trim_to_budget(p)
    record = next(r for r in caplog.records if "last-resort trim" in r.getMessage())
    assert record.args[-1] == section_tokens(p)
    assert len(record.args[-1]) == 13  # learned style has its own bounded section
    assert all(isinstance(v, int) for v in record.args[-1].values())
    assert "PRIVATE_SENTINEL" not in caplog.text


def test_style_is_trimmed_before_elsewhere_chat_and_mandatory_evidence():
    base=parts(elsewhere=['Another medium remembered.'],facts=['Mandatory fact.'],pinned=0)
    budget=tokens(render_messages(base))
    p=replace(base,style=['A learned optional note.']*8)
    trimmed=trim_to_budget(p,budget=budget)
    assert trimmed==base
    assert trimmed.facts==p.facts and trimmed.instruction==p.instruction
