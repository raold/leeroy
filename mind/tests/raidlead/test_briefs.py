"""The boss briefs ([raidlead] briefs): a pre-pull plan built by code from the raid playbooks and
said word for word, its difficulty and role filtering, its lines, the fallback to the standing
call, his questions about the plan for the boss here or one he names, and nothing changed off."""

import logging

import pytest

from conftest import run
from leeroy_mind import raidplay
from leeroy_mind.guild.protocol import GuildGroup
from leeroy_mind.protocol import Boss, Chat
from leeroy_mind.raidlead import briefs as B
from leeroy_mind.raidlead import callouts as C
from leeroy_mind.raidlead.config import load
from leeroy_mind.raidlead.orders import AskPlan, parse
from leeroy_mind.talk.turns import TurnKind

from .rig import CAST, HUMAN, drain, make_app, ready

EVERYONE = ("healer", "melee", "ranged", "tank", "tank")
NAMES = {CAST[g][0].lower(): g for g in CAST}


def explains(brief, book=None):
    return [(book or raidplay.load()).mechanic(m).explain for m in brief.mechanics]


def assert_word_for_word(brief, header, book=None):
    """Every line is whole playbook lines after the header: nothing reworded, nothing cut."""
    assert brief.lines[0].startswith(header + " ")
    assert " ".join(brief.lines) == " ".join([header, *explains(brief, book)])
    assert 1 <= len(brief.lines) <= B.MAX_LINES
    assert all(len(line.encode("utf-8")) <= B.LINE_BYTES for line in brief.lines)
    assert 2 <= len(brief.mechanics) <= B.MOST


# -- the brief -----------------------------------------------------------------------------------

def test_jaraxxus_normal_and_heroic_differ_as_the_script_does():
    normal, heroic = B.build(649, 1, 0, EVERYONE), B.build(649, 1, 3, EVERYONE)
    assert_word_for_word(normal, "Lord Jaraxxus, 10 normal:")
    assert_word_for_word(heroic, "Lord Jaraxxus, 25 heroic:")
    assert normal.context == "649:1:0" and heroic.context == "649:1:3"
    # on normal the volcano and portal cannot be attacked: no line says to
    assert not {"toc.jaraxxus.infernal_volcano", "toc.jaraxxus.nether_portal"} & set(normal.mechanics)
    assert {"toc.jaraxxus.mistress_of_pain", "toc.jaraxxus.fel_fireball",
            "toc.jaraxxus.infernal_eruption"} <= set(normal.mechanics)
    # on heroic what only heroic has comes first
    assert heroic.mechanics[:2] == ("toc.jaraxxus.infernal_volcano", "toc.jaraxxus.nether_portal")


def test_only_mechanics_for_the_roles_in_his_group():
    damage = B.build(649, 1, 0, ("melee", "ranged"))
    assert all({"melee", "ranged"} & set(raidplay.mechanic(m).roles) for m in damage.mechanics)
    healers = B.build(649, 1, 0, ("healer",))
    assert all("healer" in raidplay.mechanic(m).roles for m in healers.mechanics)
    assert "toc.jaraxxus.fel_fireball" in damage.mechanics  # the kick is for melee and ranged
    assert "toc.jaraxxus.fel_fireball" not in healers.mechanics
    assert "toc.jaraxxus.incinerate_flesh" not in damage.mechanics  # the healers' alone


def test_a_tanks_job_only_when_tanks_are_named():
    """Gluth's Mortal Wound is the tanks' alone: said for a group with tanks, never without, and
    never when nobody's role is known."""
    tank_only = {m.id for m in raidplay.playbook_for(533, 2, 0).mechanics if m.roles == ("tank",)}
    assert tank_only
    assert tank_only & set(B.build(533, 2, 0, EVERYONE).mechanics)
    assert not tank_only & set(B.build(533, 2, 0, ("healer", "melee", "ranged")).mechanics)
    assert not tank_only & set(B.build(533, 2, 0, ()).mechanics)


def test_no_brief_without_a_playbook_a_difficulty_or_anyone_it_is_for():
    assert B.build(603, 0, 0, EVERYONE) is None  # Ulduar has no playbooks yet
    assert B.build(631, 0, None, EVERYONE) is None
    assert B.build(533, 0, 3, EVERYONE) is None  # Naxxramas has no heroic
    assert B.build(631, 0, 0, ("bard",)) is None
    assert C.boss_brief(C.BossContext(631, 0, "Lord Marrowgar")) is None  # size not known
    assert C.boss_brief(C.BossContext(603, 0, "Flame Leviathan", 25)) is None


def test_unreadable_playbooks_give_no_brief_and_say_so_once(monkeypatch, caplog):
    monkeypatch.setattr(C, "_UNAVAILABLE", [])
    monkeypatch.setattr(raidplay, "load", lambda: (_ for _ in ()).throw(raidplay.PlaybookError("bad")))
    context = C.BossContext(631, 0, "Lord Marrowgar", 25, False, EVERYONE)
    with caplog.at_level(logging.WARNING):
        assert C.boss_brief(context) is None and C.boss_brief(context) is None
    warned = [r.getMessage() for r in caplog.records if "raidlead_brief_unavailable" in r.getMessage()]
    assert len(warned) == 1 and "PlaybookError" in warned[0] and "bad" not in warned[0]
    assert C.boss_plan(context).startswith("marrowgar")


def _book(explain_lengths):
    """An invented Ulduar playbook: one boss whose interrupts have explanations of these lengths."""
    mechanics = "".join(
        f'[[boss.mechanic]]\nid = "uld.leviathan.m{i}"\nname = "M{i}"\nkind = "interrupt"\n'
        f'roles = ["melee", "ranged"]\nspells = [{i + 1}]\nhandled_by = "none"\n'
        f'explain = "{chr(65 + i)}{"x" * (n - 2)}."\ncheck = "none"\n\n'
        for i, n in enumerate(explain_lengths))
    text = ('map = 603\nkey = "uld"\ndifficulties = ["10", "25"]\nscript_dir = "x"\n\n'
            '[[boss]]\nindex = 0\nname = "Flame Leviathan"\nslug = "leviathan"\nscript = "x.cpp"\n\n'
            + mechanics)
    return raidplay.load_files({"uld.toml": text.encode()})


def test_lines_hold_whole_sentences_at_most_three_of_255_bytes():
    book = _book([190, 190, 190, 190, 190])
    brief = B.build_from(book, 603, 0, 1, ("melee",))
    assert len(brief.lines) == 3 and len(brief.mechanics) == 3  # a fourth does not fit whole
    assert_word_for_word(brief, "Flame Leviathan, 25 normal:", book)
    # one that does not fit is left out, never cut, and a shorter one after it may still fit
    book = _book([190, 190, 190, 200, 20])
    brief = B.build_from(book, 603, 0, 0, ("ranged",))
    assert brief.mechanics == ("uld.leviathan.m0", "uld.leviathan.m1", "uld.leviathan.m2",
                               "uld.leviathan.m4")
    assert_word_for_word(brief, "Flame Leviathan, 10 normal:", book)
    short = B.build_from(_book([40, 40, 40, 40, 40]), 603, 0, 0, ("melee",))
    assert len(short.lines) == 1 and len(short.mechanics) == B.MOST


def test_boss_aliases_name_each_boss_once():
    aliases = B.boss_aliases()
    assert aliases["heigan"] == (533, 4) == aliases["heigan the unclean"]
    assert aliases["lich king"] == (631, 12) == aliases["lichking"]
    assert aliases["jaraxxus"] == (649, 1) and aliases["kelthuzad"] == (533, 14)
    assert "general" not in aliases and "the" not in aliases  # two bosses' word, or no name


# -- his questions ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text, asked", [
    ("what do we do on heigan", (533, 4)), ("what's the plan for the lich king?", (631, 12)),
    ("how do we kill sindragosa?", (631, 11)), ("how does jaraxxus work?", (649, 1)),
    ("explain festergut", (631, 4)), ("anub'arak strat?", (649, 4)),
])
def test_a_question_naming_a_boss_is_a_plan_question_only_with_the_briefs(text, asked):
    assert parse(text, NAMES, B.boss_aliases()) == AskPlan(None, asked)
    assert parse(text, NAMES) is None  # off: talk, as before


def test_boss_questions_keep_the_other_readings():
    bosses = B.boss_aliases()
    assert parse("coros, how does sindragosa work?", NAMES, bosses) == AskPlan(103, (631, 11))
    assert parse("what's the plan?", NAMES, bosses) == AskPlan(None)
    assert parse("what do we do on monday", NAMES, bosses) is None
    assert parse("everyone stack on me", NAMES, bosses).action == "stack"


# -- the switch -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, enabled, briefs", [
    ("[raidlead]\nenabled = true\n", True, False),
    ("[raidlead]\nenabled = true\nbriefs = true\n", True, True),
    ("[raidlead]\nenabled = false\nbriefs = true\n", False, False),
    ('[raidlead]\nenabled = true\nbriefs = "yes"\n', False, False),
    ("[raidlead]\nbriefs = true\n", False, False),
])
def test_the_briefs_switch(tmp_path, text, enabled, briefs):
    (tmp_path / "mind.toml").write_text(text, encoding="utf-8")
    cfg = load(tmp_path / "mind.toml")
    assert (cfg.enabled, cfg.briefs) == (enabled, briefs)


def test_attach_turns_on_the_pre_pull_brief_only_with_the_switch(store, clock, tmp_path):
    from types import SimpleNamespace
    from leeroy_mind.raidlead.service import attach
    for text, on in (("[raidlead]\nenabled = true\n", False),
                     ("[raidlead]\nenabled = true\nbriefs = true\n", True)):
        app = make_app(store, clock, tmp_path)
        app.guild.calls = SimpleNamespace(briefs=False)
        app.register_event_handler = lambda *a, **k: None
        app.register_connection_hook = app.register_background = app.register_shutdown_drain = lambda h: None
        (tmp_path / "mind.toml").write_text(text, encoding="utf-8")
        service = attach(app)
        assert service.briefs is on and app.guild.calls.briefs is on


# -- the raid lead answers --------------------------------------------------------------------------

@pytest.fixture
def app(store, clock, tmp_path):
    return make_app(store, clock, tmp_path)


def line(text):
    return Chat(ts=0, channel="raid", from_guid=HUMAN, from_name="Rowan", to_guid=0, text=text,
                map=649, zone=4722)


async def ask(service, text, line_id=2000):
    ev = line(text)
    claimed = service.claims(ev, line_id)
    await service.event(ev)
    await drain(service)
    if service._briefing is not None:
        await service._briefing
    return claimed


async def in_toc(app, *, briefs=True, difficulty=3, entered=True):
    """He leads his group in Trial of the Crusader at that difficulty, and has engaged Jaraxxus."""
    service = await ready(app)
    service.briefs = briefs
    await drain(service)  # the default lead: 101, the officer with the lowest guid
    members = app.world.members[101]
    app.world.members[HUMAN] = members.__class__(
        guid=HUMAN, name="Rowan", race=1, class_=8, gender=0, level=80, rank=0, online=True,
        map=649, zone=4722, spec_tab=0)
    if entered:
        await service.event(GuildGroup(0, "entered", 25, HUMAN, True, 649, 77, difficulty, (),
                                       tuple(CAST)))
    await service.event(Boss(0, 649, 77, 1, "", "engaged", (HUMAN, 101)))
    await drain(service)
    return service


def test_his_plan_question_gets_the_brief_word_for_word_then_her_words(app):
    async def main():
        service = await in_toc(app)
        assert await ask(service, "what's the plan?") == "order"
        context = service._context(649, 1, "Lord Jaraxxus")
        assert (context.size, context.heroic) == (25, True)
        assert context.roles == ("healer", "melee", "ranged", "ranged", "tank", "tank")  # his own too
        brief = B.build(649, 1, 3, context.roles)
        said = app.guild.ports.said
        assert [s[2] for s in said] == list(brief.lines) == app.guild.ports.exact
        assert all(s[0] == 101 and s[1] == "raid" and s[3] is TurnKind.CALLOUT for s in said)
        [turn] = app.guild.source.turns
        assert turn.speaker == 101 and turn.kind is TurnKind.REPLY_HUMAN and turn.trigger_line == 2000
        assert "word for word" in turn.instruction and "name no mechanic" in turn.instruction
        assert not any(e in turn.instruction for e in explains(brief))  # she cannot reword it
    run(main())


def test_a_boss_he_names_gets_its_brief_and_another_raids_falls_back(app):
    async def main():
        service = await in_toc(app)
        assert await ask(service, "what do we do on jaraxxus?", 2001) == "order"
        assert app.guild.ports.said[0][2].startswith("Lord Jaraxxus, 25 heroic: ")
        app.guild.ports.said.clear()
        app.guild.source.turns.clear()
        # Heigan is in Naxxramas, not here: its difficulty is not known, so no brief
        assert await ask(service, "what do we do on heigan", 2002) == "order"
        assert app.guild.ports.said == []
        [turn] = app.guild.source.turns
        assert "asked about Heigan the Unclean; there is no plan on file for it" in turn.instruction
    run(main())


def test_without_a_known_difficulty_the_standing_call_answers(app):
    async def main():
        service = await in_toc(app, entered=False)
        assert await ask(service, "what's the plan?")
        assert app.guild.ports.said == [] and app.guild.ports.exact == []
        [turn] = app.guild.source.turns
        assert "The boss here is Lord Jaraxxus: jaraxxus: interrupt, dodge eruptions" in turn.instruction
    run(main())


def test_off_nothing_changes(app):
    async def main():
        service = await in_toc(app, briefs=False)
        assert service._modes == {}  # nothing is even recorded
        assert await ask(service, "what do we do on jaraxxus?", 2003) is False  # talk, as before
        assert await ask(service, "what's the plan?", 2004) == "order"
        assert app.guild.ports.said == [] and app.guild.ports.exact == []
        [turn] = app.guild.source.turns
        facts = ["Arkon calls the raid; Rowan keeps the group.",
                 "The group: 2 tanks, 1 healers, 2 damage dealers.",
                 "The boss here is Lord Jaraxxus: jaraxxus: interrupt, dodge eruptions, kill the mistress."]
        assert turn.instruction == C.plan("Rowan", facts)
    run(main())


def test_one_brief_at_a_time_and_not_again_at_once(app):
    async def main():
        service = await in_toc(app)
        await ask(service, "what's the plan?", 2005)
        count = len(app.guild.ports.said)
        await ask(service, "what's the plan?", 2006)
        assert len(app.guild.ports.said) == count and len(app.guild.source.turns) == 1
        await app.clock.advance(21)
        await ask(service, "what's the plan?", 2007)
        assert len(app.guild.ports.said) == 2 * count
    run(main())


def test_a_broken_group_stops_the_rest_of_the_brief(app):
    async def main():
        service = await in_toc(app)
        ports, real = app.guild.ports, app.guild.ports.say

        async def say(*args, **kw):
            said = await real(*args, **kw)
            service._break("group_left")  # his group breaks while her first line is typed
            return said
        ports.say = say
        await ask(service, "what's the plan?")
        assert len(ports.said) == 1 and app.guild.source.turns == []
    run(main())


def test_the_logs_carry_ids_and_counts_only(app, caplog):
    async def main():
        service = await in_toc(app)
        with caplog.at_level(logging.INFO):
            await ask(service, "what's the plan?")
        logged = [r.getMessage() for r in caplog.records if "raidlead_brief" in r.getMessage()]
        assert logged and all("context=649:1:3" in m and "Jaraxxus" not in m and "Kick" not in m
                              for m in logged)
    run(main())


def test_the_raid_he_drives_gives_its_own_difficulty(app):
    """His adopted raid's plan is the instance's size and heroic, before any guild_group."""
    from types import SimpleNamespace
    async def main():
        service = await in_toc(app, entered=False)
        app.guild.raid_night.run = SimpleNamespace(
            lead_is_human=True, plan=SimpleNamespace(raid_key="toc", size=10, heroic=True,
                                                     lead_guid=101, members=list(CAST)))
        context = service._context(649, 1, "Lord Jaraxxus")
        assert (context.size, context.heroic, context.context_id) == (10, True, "649:1:2")
        assert service._context(631, 0, "Lord Marrowgar").size == 0  # not where he is
        await ask(service, "what's the plan?")
        assert app.guild.ports.said[0][2].startswith("Lord Jaraxxus, 10 heroic: ")
    run(main())
