"""His away clock (offline_min_away_min) starts at his logout, not at a later hello.

Every hello (a mind start, a worldserver start, a bridge reconnect) re-reports his presence. One that
finds him offline when he already was must leave the clock where his logout put it: on 2026-11-19 a
setup failure at 13:00 was due to be retried at 13:15, but hellos at 13:01, 13:02, 13:35, 13:38, 13:43
and 14:22 each restarted the hour, and no offline raid ran before he logged in at 14:29.
"""
import random

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.offline import LOGOUT_KEY, OfflineRaids
from leeroy_mind.guild.loot import _get
from leeroy_mind.guild.protocol import StartOfflineRaid
from leeroy_mind.protocol import Login, Logout
from .test_offline import setup  # noqa: F401  (fixture)
from .test_raid_night import _run_async, advance, setup as raid_setup  # noqa: F401  (fixture)


def starts(ports):
    return [cmd for cmd in ports.sent if isinstance(cmd, StartOfflineRaid)]


async def play_then_leave(ports, controller):
    """He logs in and out: the away clock starts at the logout."""
    ports.online = True
    await controller.on_login(Login(0, 1, True))
    await advance(ports, 10)
    ports.online = False
    await controller.on_logout(Logout(0, 1, True))
    return ports.clock.now()


@_run_async
async def test_his_logout_starts_the_hour_he_must_be_away(setup):
    ports, _, controller = setup
    left = await play_then_leave(ports, controller)
    assert _get(ports.conn, LOGOUT_KEY) == left
    await advance(ports, 59 * 60)
    await controller.tick()
    assert not starts(ports)
    await advance(ports, 61)
    await controller.tick()
    assert len(starts(ports)) == 1


@_run_async
async def test_a_hello_that_finds_him_still_offline_keeps_his_away_clock(setup):
    ports, _, controller = setup
    left = await play_then_leave(ports, controller)
    await advance(ports, 50 * 60)
    # A worldserver start or bridge reconnect: the hello says he is offline, as he already was.
    await controller.on_logout(Logout(0, 1, True))
    assert _get(ports.conn, LOGOUT_KEY) == left
    await advance(ports, 11 * 60)
    await controller.tick()
    assert len(starts(ports)) == 1


@_run_async
async def test_a_mind_restart_keeps_the_away_clock_of_a_logout_it_recorded(setup):
    ports, armory, controller = setup
    left = await play_then_leave(ports, controller)
    for _ in range(5):  # Releases: five restarts within the hour, each greeted with him offline.
        await advance(ports, 10 * 60)
        controller = OfflineRaids(ports, GuildConfig(loot_receipts=False), armory, random.Random(7))
        await controller.on_logout(Logout(0, 1, True))
    assert _get(ports.conn, LOGOUT_KEY) == left
    await advance(ports, 11 * 60)
    await controller.tick()
    assert len(starts(ports)) == 1


@_run_async
async def test_a_hello_after_he_was_last_seen_online_is_his_logout(setup):
    """He was in game when the mind stopped and is offline at its next hello: he left in between,
    so the hello is the first proof of his logout and the hour starts there."""
    ports, armory, controller = setup
    ports.online = True
    await controller.on_login(Login(0, 1, True))
    await advance(ports, 30 * 60)
    ports.online = False
    restarted = OfflineRaids(ports, GuildConfig(loot_receipts=False), armory, random.Random(7))
    await restarted.on_logout(Logout(0, 1, True))
    greeted = ports.clock.now()
    assert _get(ports.conn, LOGOUT_KEY) == greeted
    await advance(ports, 59 * 60)
    await restarted.tick()
    assert not starts(ports)
    await advance(ports, 61)
    await restarted.tick()
    assert len(starts(ports)) == 1


@_run_async
async def test_a_first_hello_with_nothing_recorded_starts_the_clock(setup):
    """No logout was ever recorded (a fresh install): the hello is all there is to go by."""
    ports, _, controller = setup
    await controller.on_logout(Logout(0, 1, True))
    assert _get(ports.conn, LOGOUT_KEY) == ports.clock.now()
