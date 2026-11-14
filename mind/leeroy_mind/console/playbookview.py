"""The Training tab's playbook panel: the repo's raid playbooks, read-only. No mind.db, no data
dir: the same answer for every request, so it is built once per console process."""
import functools

from leeroy_mind import raidplay


@functools.cache
def playbook_view() -> dict:
    playbooks = raidplay.load()
    raids = []
    for raid in playbooks.raids.values():
        bosses = []
        for boss in raid.bosses:
            bosses.append({
                "index": boss.index, "name": boss.name, "slug": boss.slug, "call": boss.call,
                "enrage": sorted(set(boss.enrage.values())),
                "phases": [p.name for p in boss.phases],
                "mechanics": [{"id": m.id, "name": m.name, "kind": m.kind, "roles": list(m.roles),
                               "phase": m.phase, "only": list(m.only) if m.only != raid.difficulties else [],
                               "handled_by": m.handled_by, "check": m.check, "explain": m.explain}
                              for m in boss.mechanics]})
        raids.append({"map": raid.map_id, "key": raid.key, "name": raid.name,
                      "difficulties": list(raid.difficulties), "bosses": bosses})
    mechanics = playbooks.mechanics()
    return {"raids": raids,
            "totals": {"raids": len(raids), "bosses": len(playbooks.bosses()), "mechanics": len(mechanics),
                       "playerbots": sum(m.handled_by == "playerbots" for m in mechanics),
                       "checked": sum(m.check != "none" for m in mechanics)}}
