import asyncio
import logging

from leeroy_mind.talk.facts import FACTS_TOKENS, LibraryFacts
from leeroy_mind.talk.questions import GameQuestion
from leeroy_mind.text import estimate_tokens


class Library:
    async def search(self, *args, **kwargs):
        return []

    def titled(self, *args):
        return []


def test_provider_runs_once_in_shared_lookup_with_expanded_question():
    async def scenario():
        facts = LibraryFacts(Library(), None)
        calls = []
        def provider(question):
            calls.append(question)
            return ["Dummy result is provisional; field performance unmeasured."]
        facts.add_provider(provider)
        facts.add_provider(provider)
        q = GameQuestion("class", ("class",), "Icecrown Citadel mage rotation")
        facts.start(7, q)
        facts.start(7, q)
        first, second = await asyncio.gather(facts.for_line(7), facts.for_line(7))
        assert first == second == ["Dummy result is provisional; field performance unmeasured."]
        assert calls == [q.query]
    asyncio.run(scenario())


def test_all_providers_and_library_share_one_budget(monkeypatch):
    import leeroy_mind.talk.facts as module
    monkeypatch.setattr(module, "fact_lines", lambda *a, **kw: ["Library evidence. " * 25])
    async def scenario():
        facts = LibraryFacts(Library(), None)
        facts.add_provider(lambda q: ["Extra evidence. " * 50, "A complete short fact."])
        facts.add_provider(lambda q: ["Another complete fact."] * 100)
        facts.start(1, GameQuestion("class", (), "mage rotation"))
        lines = await facts.for_line(1)
        assert lines[0].startswith("Library evidence.")
        assert "A complete short fact." in lines
        assert len(lines) == len(set(lines))
        assert estimate_tokens("\n".join(lines)) <= FACTS_TOKENS
        assert not any(line.startswith("Extra") for line in lines)
    asyncio.run(scenario())


def test_provider_failure_logs_no_sensitive_text_and_preserves_other_facts(caplog):
    async def scenario():
        facts = LibraryFacts(Library(), None)
        def bad(question):
            raise RuntimeError("secret human question: " + question)
        facts.add_provider(bad)
        facts.add_provider(lambda q: ["A safe local fact."])
        facts.start(1, GameQuestion("class", (), "private phrase"))
        return await facts.for_line(1)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(scenario()) == ["A safe local fact."]
    assert "facts_provider_failed" in caplog.text
    assert "secret" not in caplog.text and "private phrase" not in caplog.text


def test_provider_output_shape_and_oversize_lines_are_bounded():
    async def scenario():
        facts = LibraryFacts(Library(), None)
        facts.add_provider(lambda q: "a string is not a fact list")
        facts.add_provider(lambda q: [None, "x" * 100000, "Retain this qualifier: provisional."])
        facts.start(1, GameQuestion("class", (), "mage rotation"))
        return await facts.for_line(1)
    assert asyncio.run(scenario()) == ["Retain this qualifier: provisional."]
