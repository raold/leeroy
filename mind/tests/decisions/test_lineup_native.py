"""The add-from-guild picker's roster (native guild_add_roster): sent while a posted lineup's night
is at hand, empty when it ends, again after every hello; a refusal is logged by its code only."""
import asyncio
import json
import logging

from leeroy_mind.decisions import ledger
from leeroy_mind.decisions.lineup import NIGHT_S, ROSTER_AHEAD_S, LineupRules, subject_of
from leeroy_mind.guild.protocol import COMMANDS, GuildAddRoster, register
from leeroy_mind.protocol import encode_command

from .test_lineup import HOUR, Ports, app_for, cast, event, move  # noqa: F401  (the cast fixture)


def posted(store, now, at, members=(201, 211, 221), bench=(229,)):
    event(store, at)
    ident = ledger.propose(store, kind='lineup', subject=subject_of(7),
                           value={'members': list(members), 'bench': list(bench)}, positions=[], now=now)
    ledger.settle(store, ident, settled_by=201, now=now)
    return ident


def test_the_roster_is_on_the_wire_as_the_native_parser_reads_it():
    register()
    assert COMMANDS['guild_add_roster'] is GuildAddRoster
    line = json.loads(encode_command(GuildAddRoster('c1', (201, 211), (229,), 1_700_000_000_000), 5))
    assert line == {'v': 1, 'type': 'guild_add_roster', 'ts': 5, 'id': 'c1', 'members': [201, 211],
                    'bench': [229], 'until_ms': 1_700_000_000_000}


def test_sent_while_the_night_is_at_hand_cleared_after_and_resent_on_hello(store, clock, cast, caplog):
    members, personas = cast
    now = clock.now()
    at = now + 10 * HOUR
    posted(store, now, at)
    ports = Ports()
    rules, app = LineupRules(), app_for(store, clock, personas, ports)
    run = lambda: asyncio.run(rules.tick(app, clock.now()))  # noqa: E731
    run()
    assert ports.sent == []  # no hello yet: nothing is known of the worldserver
    rules.on_hello(None)
    run()
    [first] = ports.sent
    assert (first.members, first.bench, first.until_ms) == ((), (), 0)  # nothing at hand: native holds none
    move(clock, at - ROSTER_AHEAD_S)
    run()
    roster = ports.sent[-1]
    assert (roster.members, roster.bench) == ((201, 211, 221), (229,))
    assert roster.until_ms == int((at + NIGHT_S) * 1000) and roster.until_ms - clock.now() * 1000 <= 12 * HOUR * 1000
    run()
    assert len(ports.sent) == 2  # sent once per change
    rules.on_hello(None)  # a worldserver start cleared it
    run()
    assert len(ports.sent) == 3 and ports.sent[-1].members == (201, 211, 221)
    rules.on_connection(False)
    run()
    assert len(ports.sent) == 3  # nothing goes out while the bridge is down
    rules.on_hello(None)
    move(clock, at + NIGHT_S)
    run()
    assert (ports.sent[-1].members, ports.sent[-1].bench, ports.sent[-1].until_ms) == ((), (), 0)
    with caplog.at_level(logging.INFO):
        run()
    assert len(ports.sent) == 4


def test_his_overrule_before_the_raid_clears_the_roster(store, clock, cast):
    members, personas = cast
    now = clock.now()
    ident = posted(store, now, now + 2 * HOUR)
    ports = Ports()
    rules, app = LineupRules(), app_for(store, clock, personas, ports)
    rules.on_hello(None)
    asyncio.run(rules.tick(app, clock.now()))
    assert ports.sent[-1].members == (201, 211, 221)
    ledger.overrule(store, ident, now=now)
    asyncio.run(rules.tick(app, clock.now()))
    assert ports.sent[-1].members == () and ports.sent[-1].bench == ()


def test_a_refusal_is_logged_by_its_code(store, clock, cast, caplog):
    members, personas = cast
    now = clock.now()
    posted(store, now, now + 2 * HOUR)
    ports = Ports(ok=False)
    rules, app = LineupRules(), app_for(store, clock, personas, ports)
    rules.on_hello(None)
    with caplog.at_level(logging.INFO):
        asyncio.run(rules.tick(app, clock.now()))
        asyncio.run(rules.tick(app, clock.now()))
    refused = [r.getMessage() for r in caplog.records if 'guild_add_roster_refused' in r.getMessage()]
    assert refused == ['guild_add_roster_refused error=badfield:until_ms']  # once: not resent until it changes
    assert len(ports.sent) == 1
