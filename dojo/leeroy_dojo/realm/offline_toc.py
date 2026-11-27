"""The real offline raid must descend, surface Anub'arak and engage him."""


def reached_intro(observation):
    gate = observation.get("toc_intro", {})
    native = observation.get("native", {})
    event = native.get("event", {})
    counts = ("samples", "max_gap_ms", "trigger_samples", "measured_ms", "emerge_ms", "engage_ms")
    if any(type(gate.get(key)) is not int or gate[key] < 0 for key in counts):
        return False
    return (gate.get("state") == "ready" and gate.get("native_spawn") is True
            and gate.get("initially_blocked") is True and gate.get("emerged") is True
            and gate.get("engaged") is True and gate.get("strategies_restored") is True
            and gate.get("setup_bosses") == [0, 1, 2, 3]
            and gate["samples"] >= 20 and gate["max_gap_ms"] <= 200 and gate["trigger_samples"] >= 1
            and 0 < gate["emerge_ms"] <= gate["engage_ms"] <= gate["measured_ms"] <= 120000
            and native.get("stage") == "running" and native.get("map") == 649
            and native.get("boss_states") == [3, 3, 3, 3, 1]
            and type(event.get("kills")) is int and event["kills"] == 0
            and type(event.get("wipes")) is int and event["wipes"] == 0)


def reached_anub(observation):
    """One real kill after the prepared arena, with native frost and spike effects."""
    gate = observation.get("toc_intro", {})
    native = observation.get("native", {})
    event = native.get("event", {})
    counts = ("samples", "max_gap_ms", "trigger_samples", "measured_ms", "emerge_ms", "engage_ms",
              "grounded_spheres", "stopped_spikes", "boss_start_health", "boss_least_health")
    if any(type(gate.get(key)) is not int or gate[key] < 0 for key in counts):
        return False
    return (gate.get("state") == "ready" and gate.get("complete_encounter") is True
            and gate.get("native_spawn") is True and gate.get("initially_blocked") is True
            and gate.get("emerged") is True and gate.get("engaged") is True
            and gate.get("strategies_restored") is True and gate.get("setup_bosses") == [0, 1, 2, 3]
            and gate["samples"] >= 20 and 0 < gate["max_gap_ms"] <= 200 and gate["trigger_samples"] >= 1
            and 0 < gate["emerge_ms"] <= gate["engage_ms"] <= 120000
            and gate["engage_ms"] <= gate["measured_ms"] <= 900000
            and gate["grounded_spheres"] > 0 and gate["stopped_spikes"] > 0
            and 0 <= gate["boss_least_health"] < gate["boss_start_health"]
            and native.get("map") == 649 and native.get("boss_states") == [3, 3, 3, 3, 3]
            and native.get("active") is False and native.get("cleanup") is True
            and event.get("state") == "done" and event.get("reason") == "cleared"
            and type(event.get("kills")) is int and event["kills"] == 1
            and type(event.get("wipes")) is int and event["wipes"] == 0
            and observation.get("scripted_wipe") is False)
