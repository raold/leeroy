"""Naxx route proof: prepared prerequisites do not count as earned kills."""


def reached_naxx(observation, *, control):
    gate = observation.get("naxx_pass", {})
    native = observation.get("native", {})
    event = native.get("event", {})
    if (gate.get("state") != "ready" or gate.get("control") is not control
            or gate.get("strategies_restored") is not True
            or gate.get("setup_bosses") != [3, 4, 5, 6, 7, 8, 10]
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
