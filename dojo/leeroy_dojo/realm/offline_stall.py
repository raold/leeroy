"""Require the production native timer to end a recorded ten-minute plateau."""


def reached_stall(observation):
    gate = observation.get("native_stall", {})
    native = observation.get("native", {})
    event = native.get("event", {})
    if any(type(gate.get(key)) is not int for key in ("held_ms", "samples", "max_gap_ms", "limit_ms")):
        return False
    # Setup may acquire the hold during the first two seconds of the real run.
    return (gate.get("state") == "held" and gate.get("released") is True
            and gate["limit_ms"] == 600000 and 598000 <= gate["held_ms"] <= 610000
            and gate["samples"] >= 1000 and 0 < gate["max_gap_ms"] <= 200
            and native.get("active") is False and native.get("cleanup") is True and native.get("map") == 649
            and event.get("state") == "done" and event.get("reason") == "stalled"
            and type(event.get("kills")) is int and event["kills"] == 0
            and type(event.get("wipes")) is int and event["wipes"] == 0)
