"""Naxx route proof: prepared prerequisites do not count as earned kills."""

APPROACH_SPAWNS = [
    127638, 127646, 127647, 127648, 127650, 127651, 127652, 127653, 127654,
    127666, 127667, 127668, 127669, 127672, 127673, 127674, 127675, 127693,
    127694, 127695, 127696, 127697, 127698, 127699, 127700, 127703, 127704,
    127705, 127750, 127751, 127752, 127755, 127756, 127758, 127759, 127764,
    127765, 127766, 127767, 127768, 127769, 127790, 127791, 127792, 127793,
]


def reached_naxx(observation, *, control):
    gate = observation.get("naxx_pass", {})
    native = observation.get("native", {})
    event = native.get("event", {})
    if (gate.get("state") != "ready" or gate.get("control") is not control
            or gate.get("strategies_restored") is not True
            or gate.get("setup_bosses") != [3, 4, 5, 6, 7, 8, 10]
            or gate.get("setup_boss_deaths") != [15956, 15953, 15952, 15954, 15936, 16011, 16061]
            or gate.get("setup_trash_deaths") != APPROACH_SPAWNS
            or type(gate.get("setup_add_deaths")) is not int or gate["setup_add_deaths"] != 2
            or type(gate.get("setup_completed_mask")) is not int or gate["setup_completed_mask"] != 127
            or any(type(gate.get(k)) is not int or gate[k] < 0
                   for k in ("measured_ms", "skip_ms", "engage_ms", "passes"))
            or not 0 < gate["measured_ms"] <= 600000
            or type(event.get("wipes")) is not int or event["wipes"] != 0
            or type(event.get("kills")) is not int or event["kills"] != 0
            or observation.get("scripted_wipe") is not False):
        return False
    if control:
        return (gate.get("gothik_skipped") is False and gate.get("patchwerk_engaged") is False
                and gate["passes"] == 16 and gate["skip_ms"] == gate["engage_ms"] == 0
                and native.get("active") is False and native.get("cleanup") is True
                and native.get("map") == 533 and event.get("state") == "done"
                and event.get("reason") == "dc_unreachable")
    if (native.get("stage") != "running" or native.get("active") is not True
            or gate.get("gothik_skipped") is not True or gate.get("patchwerk_engaged") is not True
            or gate["passes"] != 1 or not 0 < gate["skip_ms"] < gate["engage_ms"] <= 600000):
        return False
    leader = next((b for b in observation.get("bodies", []) if b.get("slot") == 1), {})
    states = leader.get("bosses", [])
    return (leader.get("map") == 533 and leader.get("instance") == event.get("instance_id")
            and leader.get("alive") is True and leader.get("owned") is True and leader.get("pinned") is True
            and len(states) == 15 and all(type(s) is int and s in range(6) for s in states)
            and states[0] == 1 and states[11] == 0 and all(states[i] == 3 for i in gate["setup_bosses"]))
