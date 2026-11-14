"""The native responder's feed ([raidplay] respond): only what stock playerbots leaves reaches native, each
covered instance's table fits the bridge, the switch is off by default and invalid means off, and the feed sends
every table at hello and an instance's again when his group enters it."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from leeroy_mind import raidplay
from leeroy_mind.guild.protocol import GuildGroup
from leeroy_mind.protocol import MAX_LINE_BYTES, Hello, encode_command
from leeroy_mind.raidplay import PlaybookError, load_files
from leeroy_mind.raidplay import respond
from leeroy_mind.raidplay.respond import MechanicFeed, RaidMechanics, actionable, settings, tables

ON = "[raidplay]\nrespond = true\n[training]\nenabled = true\nplaybooks = true\n"


def _ids(map_id, difficulty):
    return [r.id for r in actionable(raidplay.load(), map_id, difficulty)]


def test_only_the_mechanics_stock_leaves_reach_native():
    pb = raidplay.load()
    for (map_id, difficulty), rules in tables(pb).items():
        key = raidplay.DIFFICULTIES[difficulty]
        for rule in rules:
            m = pb.mechanic(rule.id)
            assert m.handled_by != "playerbots"                     # never fight stock
            assert m.kind in raidplay.model.RESPOND_KINDS and rule.kind == m.kind
            assert m.area not in ("", "none") and rule.area == m.area
            assert set(rule.roles) <= set(m.roles) and rule.roles
            assert set(rule.spells) <= set(m.spell_ids(key)) and rule.spells
            if m.handled_by == "partial":
                assert rule.roles == m.respond_roles               # only the roles its note says stock leaves
    # A partial mechanic's tank, covered by stock, is never moved; nor are its ranged when stock places them.
    shadow_trap = next(r for r in actionable(pb, 631, 3) if r.id == "icc.lichking.shadow_trap")
    assert "tank" not in shadow_trap.roles
    vortex = next(r for r in actionable(pb, 631, 3) if r.id == "icc.council.shock_vortex")
    assert vortex.spells == (72037,)                               # stock dodges the empowered one itself
    coldflame = next(r for r in actionable(pb, 631, 1) if r.id == "icc.marrowgar.coldflame")
    assert coldflame.spells == (69140,) and coldflame.area == "line" and coldflame.range_ == 70
    assert "icc.rotface.slime_spray" not in _ids(631, 1)           # respond_roles = []
    assert "naxx.loatheb.spores" not in _ids(533, 1)
    assert "icc.putricide.choking_gas_bomb" not in _ids(631, 1)    # area none: nowhere known at cast start
    assert "icc.gunship.rocket_artillery" not in _ids(631, 1)
    assert "toc.beasts.slime_pool" not in _ids(649, 0)
    assert "icc.sindragosa.frost_bomb" not in _ids(631, 3)         # stock's los
    assert "naxx.patchwerk.hateful_strike" not in _ids(533, 0)     # soak is not responded yet
    assert "icc.lichking.shadow_trap" not in _ids(631, 0)          # heroic only


def test_each_kind_carries_what_native_needs():
    pb = raidplay.load()
    by_id = {r.id: r for rules in tables(pb).values() for r in rules}
    assert by_id["naxx.anubrekhan.impale"].angle == 24 and by_id["naxx.anubrekhan.impale"].range_ == 45
    assert by_id["naxx.horsemen.holy_wrath"].duration == 15        # a spread holds until the next one
    assert by_id["toc.jaraxxus.fel_lightning"].duration == 12
    assert by_id["toc.beasts.arctic_breath"].duration == 5
    assert by_id["toc.champions.bladestorm"].area == "caster" and by_id["toc.champions.bladestorm"].roles == ("melee",)
    assert by_id["naxx.kelthuzad.detonate_mana"].area == "target"
    assert by_id["naxx.horsemen.meteor"].kind == "stack" and by_id["naxx.horsemen.meteor"].radius == 8
    for rule in by_id.values():
        assert 0 <= rule.radius <= respond.MAX_YARDS and 0 <= rule.range_ <= respond.MAX_YARDS
        assert 0 <= rule.duration <= respond.MAX_DURATION and len(rule.spells) <= respond.MAX_SPELLS
        if rule.area == "cone":
            assert rule.angle > 0


def test_every_table_is_bounded_and_fits_one_bridge_line():
    respond.register_command("raid_mechanics", RaidMechanics)
    found = tables(raidplay.load())
    assert set(found) == {(533, 0), (533, 1), (631, 0), (631, 1), (631, 2), (631, 3),
                          (649, 0), (649, 1), (649, 2), (649, 3)}
    for (map_id, difficulty), rules in found.items():
        assert 0 < len(rules) <= respond.MAX_RULES
        line = encode_command(RaidMechanics("m1", map_id, difficulty, rules), 1)
        assert len(line) < MAX_LINE_BYTES // 4
        wire = json.loads(line)
        assert wire["type"] == "raid_mechanics" and wire["map"] == map_id and wire["difficulty"] == difficulty
        assert set(wire["mechanics"][0]) == {"id", "kind", "area", "roles", "spells", "radius", "range", "angle",
                                             "duration"}


def test_actionable_answers_nothing_for_an_uncovered_place():
    pb = raidplay.load()
    assert actionable(pb, 603, 0) == ()        # Ulduar
    assert actionable(pb, 533, 2) == ()        # Naxxramas has no heroic
    assert actionable(pb, 631, "1") == ()


@pytest.mark.parametrize("text, want", [
    (None, (False, "off")),
    ("", (False, "off")),
    ("[raidplay]\nrespond = false\n", (False, "off")),
    ("[raidplay]\nrespond = true\n", (False, "needs_playbooks")),
    ("[raidplay]\nrespond = true\n[training]\nplaybooks = false\n", (False, "needs_playbooks")),
    ("[raidplay]\nrespond = true\n[training]\nplaybooks = 3\n", (False, "needs_playbooks")),
    ("[raidplay]\nrespond = 1\n", (False, "invalid")),
    ("[raidplay]\nrespond = true\nextra = true\n", (False, "invalid")),
    ("raidplay = true\n", (False, "invalid")),
    ("[raidplay\n", (False, "invalid")),
    (ON, (True, "")),
])
def test_the_switch_is_off_by_default_and_invalid_means_off(tmp_path, text, want):
    if text is not None:
        (tmp_path / "mind.toml").write_text(text, encoding="utf-8")
    assert settings(tmp_path / "mind.toml") == want


class _App:
    def __init__(self, data_dir=None, ok=True, backlog=False):
        self.data_dir = data_dir
        self.sent = []
        self.ok = ok
        self.backlog = backlog
        self.handlers, self.hooks, self.background = {}, [], []

        async def send(cmd, preserve_id=False):
            self.sent.append(cmd)
            return SimpleNamespace(ok=self.ok, error="" if self.ok else "bad field: map")
        self.bridge = SimpleNamespace(send=send)

    def is_backlog(self, ev):
        return self.backlog

    def register_event_handler(self, kind, handler):
        self.handlers.setdefault(kind, []).append(handler)

    def register_connection_hook(self, hook):
        self.hooks.append(hook)

    def register_background(self, factory):
        self.background.append(factory)


def _hello(features=("item_links", "playstyle", "mechanics")):
    return Hello(ts=1, server="mod-leeroy", version="t", guild_id=1, human_guid=1, features=tuple(features))


def _entered(map_id=631, instance=7, difficulty=1, reason="entered"):
    return GuildGroup(ts=2, reason=reason, size=10, leader_guid=1, raid=True, map=map_id, instance_id=instance,
                      difficulty=difficulty, added=(), members=(101, 102))


async def _drain(feed):
    while feed.pending:
        await feed.send(feed.pending.pop(0))


def test_hello_sends_every_table_to_a_worldserver_that_takes_them():
    async def go():
        app = _App()
        feed = MechanicFeed(app, raidplay.load())
        await feed.connection(True)
        await feed.event(_hello())
        await _drain(feed)
        assert sorted((c.map, c.difficulty) for c in app.sent) == sorted(feed.tables)
        assert all(isinstance(c, RaidMechanics) and c.mechanics for c in app.sent)

        # An older worldserver without the feature gets nothing.
        old = _App()
        feed = MechanicFeed(old, raidplay.load())
        await feed.connection(True)
        await feed.event(_hello(("item_links", "playstyle")))
        await _drain(feed)
        assert old.sent == [] and not feed.supported
    asyncio.run(go())


def test_his_group_entering_an_instance_sends_its_table_once():
    async def go():
        app = _App()
        feed = MechanicFeed(app, raidplay.load())
        await feed.connection(True)
        await feed.event(_hello())
        await _drain(feed)
        app.sent.clear()
        await feed.event(_entered())
        await feed.event(_entered())                       # the same copy again: nothing new
        await feed.event(_entered(reason="built"))         # forming the group outside: nothing
        await feed.event(_entered(map_id=36, instance=9))  # a 5-man is not covered
        await _drain(feed)
        assert [(c.map, c.difficulty) for c in app.sent] == [(631, 1)]
        await feed.event(_entered(instance=8))             # another copy
        await _drain(feed)
        assert [(c.map, c.difficulty) for c in app.sent] == [(631, 1), (631, 1)]

        replay = _App(backlog=True)
        feed = MechanicFeed(replay, raidplay.load())
        await feed.connection(True)
        await feed.event(_hello())
        await _drain(feed)
        replay.sent.clear()
        await feed.event(_entered())                       # a replayed entry is history
        await _drain(feed)
        assert replay.sent == []
    asyncio.run(go())


def test_a_dropped_bridge_sends_nothing_until_the_next_hello():
    async def go():
        app = _App()
        feed = MechanicFeed(app, raidplay.load())
        await feed.connection(True)
        await feed.event(_hello())
        await feed.connection(False)
        await _drain(feed)
        assert app.sent == [] and not feed.pending
        await feed.event(_entered())
        assert not feed.pending
    asyncio.run(go())


def test_a_refused_table_is_logged_by_code(caplog):
    async def go():
        app = _App(ok=False)
        feed = MechanicFeed(app, raidplay.load())
        await feed.connection(True)
        await feed.event(_hello())
        assert not await feed.send(feed.pending.pop(0))
    with caplog.at_level("INFO"):
        asyncio.run(go())
    assert "raidplay_respond_refused" in caplog.text and "bad field: map" not in caplog.text


def test_attach_adds_nothing_when_off(tmp_path):
    app = _App(tmp_path)
    assert respond.attach(app) is None
    (tmp_path / "mind.toml").write_text("[raidplay]\nrespond = true\n", encoding="utf-8")
    assert respond.attach(app) is None                     # without [training] playbooks
    assert app.handlers == {} and app.hooks == [] and app.background == []
    (tmp_path / "mind.toml").write_text(ON, encoding="utf-8")
    feed = respond.attach(app)
    assert isinstance(feed, MechanicFeed)
    assert set(app.handlers) == {"hello", "guild_group"} and len(app.hooks) == 1 and len(app.background) == 1


RAID = '''
map = 649
key = "toc"
difficulties = ["10", "25", "10h", "25h"]
script_dir = "x"
[[boss]]
index = 4
name = "Anub'arak"
slug = "anubarak"
script = "y.cpp"
phase = [{ id = "main", name = "Main" }]
[[boss.mechanic]]
id = "toc.anubarak.thing"
name = "Thing"
kind = "KIND"
roles = ["tank", "ranged"]
spells = [1, 2]
handled_by = "HANDLED"
explain = "Do the thing."
radius = 3
EXTRA
'''
PARTIAL = 'playerbots = ["avoid aoe"]\nhandled_note = "only tanks"\n'


def _doc(kind="avoid_ground", handled="none", extra=""):
    return {"toc.toml": RAID.replace("KIND", kind).replace("HANDLED", handled).replace("EXTRA", extra).encode()}


def test_every_partial_mechanic_of_a_responded_kind_says_what_stock_leaves():
    for m in raidplay.load().mechanics():
        if m.handled_by == "partial" and m.kind in raidplay.model.RESPOND_KINDS:
            assert m.respond_roles is not None, m.id
        if m.handled_by != "partial":
            assert m.respond_roles is None and m.respond_spells is None, m.id


def test_the_model_reads_the_responder_fields():
    m = load_files(_doc(handled="partial", extra=PARTIAL)).mechanic("toc.anubarak.thing")
    assert m.respond_roles is None and m.responder_roles() == ()   # nothing said: the responder leaves it
    m = load_files(_doc()).mechanic("toc.anubarak.thing")
    assert m.area == "dest" and m.responder_roles() == ("tank", "ranged") and m.responder_spells("10") == (1, 2)
    m = load_files(_doc(handled="partial", extra=PARTIAL + 'respond_roles = ["ranged"]\nrespond_spells = [2]')
                   ).mechanic("toc.anubarak.thing")
    assert m.responder_roles() == ("ranged",) and m.responder_spells("25h") == (2,)
    m = load_files(_doc(handled="partial", extra=PARTIAL + "respond_roles = []")).mechanic("toc.anubarak.thing")
    assert m.responder_roles() == ()
    m = load_files(_doc(handled="playerbots", extra='playerbots = ["avoid aoe"]')).mechanic("toc.anubarak.thing")
    assert m.responder_roles() == ()
    m = load_files(_doc(extra='area = "none"')).mechanic("toc.anubarak.thing")
    assert m.responder_roles() == ()
    m = load_files(_doc(kind="move_out_cone", extra="angle = 24")).mechanic("toc.anubarak.thing")
    assert m.area == "cone" and m.number("angle", "10") == 24
    m = load_files(_doc(kind="interrupt")).mechanic("toc.anubarak.thing")
    assert m.area == "" and m.responder_roles() == ()


@pytest.mark.parametrize("doc, why", [
    (_doc(extra='respond_roles = ["tank"]'), "only for a partial"),
    (_doc(handled="partial", extra=PARTIAL + 'respond_roles = ["healer"]'), "respond_roles lists"),
    (_doc(handled="partial", extra=PARTIAL + 'respond_roles = ["tank", "tank"]'), "respond_roles lists"),
    (_doc(handled="partial", extra=PARTIAL + 'respond_roles = []\nrespond_spells = [9]'), "among its spells"),
    (_doc(extra='area = "cone"'), "area must be"),
    (_doc(kind="spread", extra='area = "dest"'), "area must be"),
    (_doc(kind="interrupt", extra='area = "dest"'), "area must be"),
    (_doc(extra='area = "line"'), "a line needs"),
    (_doc(extra="angle = 24"), "for a cone"),
    (_doc(kind="move_out_cone", extra="angle = 0"), "more than 0"),
])
def test_the_model_refuses_a_bad_responder_field(doc, why):
    with pytest.raises(PlaybookError, match=why):
        load_files(doc)


def test_the_switch_is_on_the_ops_board(tmp_path):
    import time
    from leeroy_mind.console import opsview as ops
    deadline = time.monotonic() + 10
    (tmp_path / "mind.toml").write_text("[raidplay]\nrespond = true\n", encoding="utf-8")
    rows = ops.switches_view(tmp_path, deadline, mind_started=None)["rows"]
    row = next(r for r in rows if r["table"] == "raidplay")
    assert row["state"] == "on" and row["unmet"] == ["training.playbooks"]
    (tmp_path / "mind.toml").write_text("[raidplay]\nrespond = 1\n", encoding="utf-8")
    rows = ops.switches_view(tmp_path, deadline, mind_started=None)["rows"]
    assert next(r for r in rows if r["table"] == "raidplay")["state"] == "invalid"
