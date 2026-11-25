"""His own group's raid in a real MindApp over the FakeBridge, with only the game database invented.

Raid 27 (2026-11-25): Emalon and Koralon died in Vault of Archavon 25 and the raid kept kills 0.
VoA's instance script keeps no boss states, so no `boss` event ever came; mod-leeroy now sends
one from the encounter table (boss_id the DungeonEncounter.dbc bit, the DBC's name, killed only).

Raid 26 (2026-11-25): his group's Ulduar raid stayed bound to the copy he left (618) while the
group killed four bosses in a fresh one (635); the raid now follows his group into the new copy.
"""

from conftest import HUMAN, run
from leeroy_mind.guild.raid_night import State
from leeroy_mind.guild.upgrades import ItemInfo

from test_app import CAST, hello, running

GROUP = (101, 102, 103, 104, 105)
ROLES = {101: "tank", 102: "healer", 104: "healer"}
BLADE = ItemInfo(50001, "Testblade", 4, 264, 17, 2, 8, -1, frozenset({4}))


def game_database(app):
    """The guild armory's game reads, invented: no MySQL is opened."""
    armory = app.guild.armory
    armory.available = True
    armory.roster = lambda guild: {}
    armory.friends = lambda human: set()
    armory.equipped = lambda guids: {g: {15: 200} for g in guids if g == 101}
    armory.items = lambda ids: {i: BLADE for i in ids if i == BLADE.item_id}


def group_event(reason, *, map, instance_id, difficulty):
    return {"type": "guild_group", "reason": reason, "size": 25, "leader_guid": HUMAN, "raid": True,
            "map": map, "instance_id": instance_id, "difficulty": difficulty,
            "added": [{"guid": g, "role": ROLES.get(g, "damage")} for g in GROUP] if reason == "built" else [],
            "members": list(GROUP)}


def job_done(cmd, instance_id):
    return {"type": "job", "id": cmd["id"], "kind": "summon", "state": "done", "reason": "", "stage": "",
            "guids": cmd["guids"], "skipped": [], "kills": 0, "wipes": 0, "instance_id": instance_id,
            "guid": 0, "level": 0, "avg_ilvl": 0}


def boss_killed(map, instance_id, boss_id, name=""):
    """mod-leeroy's EncodeBoss: every player in the map is a member."""
    return {"type": "boss", "map": map, "instance_id": instance_id, "boss_id": boss_id, "boss_name": name,
            "state": "killed", "members": [HUMAN, *GROUP]}


def raids(h, raid_key):
    return [tuple(r) for r in h.rows("SELECT raid_id,kills,ended,outcome FROM guild_raid WHERE raid_key=? "
                                     "ORDER BY raid_id", (raid_key,))]


def ledger(h, raid_id, reason):
    return sorted(g for (g,) in h.rows("SELECT guid FROM dkp_ledger WHERE raid_id=? AND reason=?",
                                       (raid_id, reason)))


async def adopt(h, *, map, zone, instance_id, difficulty=1, built=True):
    """He leads his add group into a raid instance; the server proves the members inside it."""
    await h.bridge.push({"type": "zone", "guid": HUMAN, "zone_id": zone, "zone_name": ""})
    if built:
        await h.bridge.push(group_event("built", map=571, instance_id=0, difficulty=0))
    before = sum(c["type"] == "summon" for c in h.bridge.commands)
    await h.bridge.push(group_event("entered", map=map, instance_id=instance_id, difficulty=difficulty))
    await h.until(lambda: sum(c["type"] == "summon" for c in h.bridge.commands) > before, timeout=20)
    summon = [c for c in h.bridge.commands if c["type"] == "summon"][-1]
    assert summon["where"] == "inside" and summon["map_id"] == map
    await h.bridge.push(job_done(summon, instance_id))
    night = h.app.guild.raid_night
    await h.until(lambda: night.session is not None and night.session.ctx.instance_id == instance_id
                  and night.state is State.RUNNING, timeout=20)
    return night.session


def test_a_vault_of_archavon_kill_pays_dkp_and_its_drop_is_handed_out(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST], prepare=game_database) as h:
            await h.wait(lambda: h.app.world.online and h.app.guild._ready())
            session = await adopt(h, map=624, zone=4603, instance_id=35)
            [(raid_id, kills, ended, _)] = raids(h, "voa")
            assert (kills, ended) == (0, None)
            await h.bridge.push(boss_killed(624, 35, 1, "Emalon the Storm Watcher"))
            await h.bridge.push({"type": "loot_drop", "map": 624, "instance_id": 35, "source_entry": 33993,
                                 "items": [{"item_id": BLADE.item_id, "name": BLADE.name, "quality": 4,
                                            "index": 0}]})
            await h.until(lambda: raids(h, "voa")[0][1] == 1, timeout=20)
            assert ledger(h, raid_id, "kill") == sorted((HUMAN, *GROUP))
            # The DBC's name reaches the DKP officer's line (her typing may lower its case).
            await h.until(lambda: any(s["channel"] == "raid" and s["text"].lower().endswith(" for emalon the storm watcher")
                                      for s in h.says()), timeout=20)
            # The same kill again (a resend, a reconnect's replay) is never a second one.
            await h.bridge.push(boss_killed(624, 35, 1, "Emalon the Storm Watcher"))
            await h.advance(5)
            assert raids(h, "voa")[0][1] == 1 and session.kills == 1
            # The drop goes to the member it is an upgrade for, through the game.
            await h.until(lambda: any(c["type"] == "give_loot" for c in h.bridge.commands), timeout=30)
            [give] = [c for c in h.bridge.commands if c["type"] == "give_loot"]
            assert (give["to_guid"], give["item_id"], give["instance_id"]) == (101, BLADE.item_id, 35)
            await h.until(lambda: h.rows("SELECT 1 FROM dkp_ledger WHERE raid_id=? AND reason='item'",
                                         (raid_id,)), timeout=20)
    run(main())


def test_his_groups_raid_follows_it_into_a_fresh_copy(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST], prepare=game_database) as h:
            await h.wait(lambda: h.app.world.online and h.app.guild._ready())
            first = await adopt(h, map=603, zone=4273, instance_id=618)
            # He leaves, unbinds, and leads the same group into a fresh copy of Ulduar.
            await h.bridge.push({"type": "zone", "guid": HUMAN, "zone_id": 67, "zone_name": "The Storm Peaks"})
            await h.advance(300)
            second = await adopt(h, map=603, zone=4273, instance_id=635, built=False)
            assert second.raid_id != first.raid_id
            await h.bridge.push(boss_killed(603, 635, 1))
            await h.until(lambda: raids(h, "ulduar")[-1][1] == 1, timeout=20)
            [(old, old_kills, ended, outcome), (new, new_kills, new_ended, _)] = raids(h, "ulduar")
            assert (old_kills, new_kills, new_ended) == (0, 1, None)
            assert ended is not None and outcome == "new_instance"
            # The copy he left saw no fight: its attendance went, and the new copy's raid paid it once.
            assert ledger(h, old, "attendance") == [] and ledger(h, new, "attendance") == sorted((HUMAN, *GROUP))
            assert ledger(h, new, "kill") == sorted((HUMAN, *GROUP))
    run(main())
