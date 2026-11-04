"""Writes dojo_protocol.jsonl, the golden protocol and lifecycle cases that the C++ library and leeroy_dojo both run.

The expected outputs below are written out by hand from the contract (dojo/README.md); this script does not import
either implementation, so a bug in one cannot leak into the fixture. Run it after changing a case:

    python modules/mod-dojo/tests/fixtures/make_dojo_protocol.py

Line kinds: "defaults" (the worker info later session cases use), "info" (a worker description and whether it is
usable), "frame" (a framed line and whether a transport skips it as blank), "decode" (one request line and the
expected decision) and "session" (a script of transport, request and worker events with the exact responses, effects
and state each must produce; an omitted "send" or "effects" means none). Error messages are free text and are not
compared, but every one a runner sees must fit MAX_MESSAGE_BYTES.

Every begin_reset and begin_step effect carries a token, counted from 1 per worker and never reused; a completion
names the generation and the token of the operation it finishes. The tokens below are counted by hand.
"""

import json
import os
from pathlib import Path

OUT = Path(os.environ.get("DOJO_FIXTURE_OUT") or Path(__file__).with_name("dojo_protocol.jsonl"))

MAX_MESSAGE_BYTES = 256
WORKER = {
    "worker": {"kind": "fixture", "module": "mod-dojo 0.1.0", "core": None, "playerbots": None},
    "scenario": "fixture_duel_v1",
    "schemas": {"observation": "dojo.fixture.obs/1", "action": "dojo.fixture.act/1"},
    "actors": [{"slot": 0, "team": 0}, {"slot": 1, "team": 1}],
    "actions": ["noop", "stop_cast", "bolt"],
    "clock": {"mode": "virtual", "substep_ms": 10, "decision_ms": 200, "paced": False},
    "rng": {"scenario_seed": True, "combat_replay": False},
}
BOUNDS = {"max_line_bytes": 262144, "max_actors": 2, "max_events": 64, "result_cache": 8, "recent_episodes": 64,
          "in_flight": 1, "max_copies": 16}
OPEN = ([1, 0, 1], [1, 0, 1])          # noop and bolt open, stop_cast closed (nobody is casting)

lines = [{"kind": "defaults", "max_message_bytes": MAX_MESSAGE_BYTES, "worker": WORKER}]


# --- builders ---

def wire_bounds(**over):
    return {**BOUNDS, **over}


def err(code, rid, reason=None):
    e = {"v": 1, "type": "error", "id": rid, "ok": False, "code": code}
    if reason is not None:
        e["reason"] = reason
    return e


def hello(rid="h1", **extra):
    return {"v": 1, "type": "hello", "id": rid, "protocol": {"min": 1, "max": 1}, **extra}


def hello_result(rid="h1", session="s1", state="ready", bounds=None):
    return {"v": 1, "type": "hello_result", "id": rid, "ok": True, "session": session, "protocol": 1,
            "state": state, "bounds": bounds or BOUNDS, **WORKER}


def status(rid="q1"):
    return {"v": 1, "type": "status", "id": rid}


def status_result(rid, state, session="s1", episode=None, decision=None, generation=0, in_flight=None,
                  draining=False, abort_reason=None):
    return {"v": 1, "type": "status_result", "id": rid, "ok": True, "state": state, "session": session,
            "episode": episode, "decision": decision, "generation": generation, "in_flight": in_flight,
            "draining": draining, "abort_reason": abort_reason}


def reset(rid, episode, seed=7, session="s1", decision=0, **extra):
    return {"v": 1, "type": "reset", "id": rid, "session": session, "episode": episode, "decision": decision,
            "scenario_seed": seed, **extra}


def begin_reset(generation, token, episode, seed=7):
    return {"effect": "begin_reset", "generation": generation, "token": token, "episode": episode,
            "scenario": "fixture_duel_v1", "scenario_seed": seed}


def observations(masks=OPEN, values=((1.0, 0.5), (1.0, 0.5)), actors=(0, 1)):
    return [{"actor": actors[i], "obs": list(values[i]), "mask": list(masks[i])} for i in range(2)]


def reset_done(masks=OPEN, sim_ms=0):
    return {"ok": True, "observations": observations(masks), "sim_ms": sim_ms}


def reset_result(rid, episode, generation, seed=7, session="s1", masks=OPEN, sim_ms=0):
    return {"v": 1, "type": "reset_result", "id": rid, "ok": True, "session": session, "episode": episode,
            "decision": 0, "generation": generation, "scenario": "fixture_duel_v1", "scenario_seed": seed,
            "sim_ms": sim_ms, "observations": observations(masks)}


def step(rid, episode, decision, actions=((0, 0), (1, 0)), session="s1"):
    return {"v": 1, "type": "step", "id": rid, "session": session, "episode": episode, "decision": decision,
            "actions": [{"actor": a, "action": b} for a, b in actions]}


def requested(decision, actions):
    return [{"actor": a, "action": b, "action_id": decision * 2 + a} for a, b in sorted(actions)]


def begin_step(generation, token, episode, decision, actions=((0, 0), (1, 0))):
    return {"effect": "begin_step", "generation": generation, "token": token, "episode": episode,
            "decision": decision, "actions": requested(decision, actions)}


def step_done(sim_ms, events=(), terminated=False, truncated=False, outcome=None, rewards=((0, 0.0), (0, 0.0)),
              masks=OPEN, elapsed_ms=200, reward_actors=(0, 1)):
    return {"elapsed_ms": elapsed_ms, "substeps": elapsed_ms // 10, "sim_ms": sim_ms,
            "observations": observations(masks), "events": list(events),
            "rewards": [{"actor": reward_actors[i], "terminal": t, "shaping": s} for i, (t, s) in enumerate(rewards)],
            "terminated": terminated, "truncated": truncated, "outcome": outcome}


def step_result(rid, episode, decision, generation, actions, done, session="s1"):
    return {"v": 1, "type": "step_result", "id": rid, "ok": True, "session": session, "episode": episode,
            "decision": decision, "next_decision": decision + 1, "generation": generation,
            "requested": requested(decision, actions), "elapsed_ms": done["elapsed_ms"],
            "substeps": done["substeps"], "sim_ms": done["sim_ms"], "observations": done["observations"],
            "events": done["events"], "rewards": done["rewards"], "terminated": done["terminated"],
            "truncated": done["truncated"], "outcome": done["outcome"]}


def close(rid="c1", session="s1", **extra):
    return {"v": 1, "type": "close", "id": rid, "session": session, **extra}


def close_result(rid="c1", session="s1"):
    return {"v": 1, "type": "close_result", "id": rid, "ok": True, "session": session}


def abort(generation, reason):
    return {"effect": "abort", "generation": generation, "reason": reason}


def release(reason):
    return {"effect": "release", "reason": reason}


CLOSE_CONNECTION = {"effect": "close_connection"}


# --- script ops ---

def connect(session="s1", **expect):
    return {"op": "connect", "session": session, **expect}


def recv(message, send=(), effects=(), **expect):
    op = {"op": "recv", "json": message} if isinstance(message, dict) else {"op": "recv", "line": message}
    return {**op, "send": list(send), "effects": list(effects), **expect}


def complete_reset(generation, token, payload, send=(), effects=(), **expect):
    return {"op": "complete_reset", "generation": generation, "token": token, "payload": payload,
            "send": list(send), "effects": list(effects), **expect}


def complete_step(generation, token, payload, send=(), effects=(), **expect):
    return {"op": "complete_step", "generation": generation, "token": token, "payload": payload,
            "send": list(send), "effects": list(effects), **expect}


def op(name, send=(), effects=(), **expect):
    return {"op": name, "send": list(send), "effects": list(effects), **expect}


def callback(generation, accepted):
    return {"op": "callback", "generation": generation, "accepted": accepted}


def session(name, script, bounds=None, booted=True):
    case = {"kind": "session", "name": name, "script": script}
    if bounds:
        case["bounds"] = bounds
    if not booted:
        case["booted"] = False
    lines.append(case)


def started(episode="e1", masks=OPEN, bounds=None):
    """connect s1, hello and reset `episode` at generation 1 with token 1, ready for decision 0."""
    return [
        connect(),
        recv(hello(), [hello_result(bounds=bounds)]),
        recv(reset("r1", episode), effects=[begin_reset(1, 1, episode)], state="resetting"),
        complete_reset(1, 1, reset_done(masks), [reset_result("r1", episode, 1, masks=masks)],
                       state="awaiting_action"),
    ]


def decode(name, message, code=None, rid=None, rtype=None, bounds=None):
    case = {"kind": "decode", "name": name}
    if isinstance(message, dict):
        case["json"] = message
    else:
        case["line"] = message
    if bounds:
        case["bounds"] = bounds
    case["expect"] = {"code": code, "id": rid} if code else {"code": None, "id": rid, "type": rtype}
    lines.append(case)


def info(name, worker, valid):
    lines.append({"kind": "info", "name": name, "worker": worker, "valid": valid})


def frame(name, line, blank):
    lines.append({"kind": "frame", "name": name, "line": line, "blank": blank})


def actors(*pairs):
    return {**WORKER, "actors": [dict(p) for p in pairs]}


# --- worker descriptions: types are exact, as everywhere else ---

info("info_fixture_worker", WORKER, True)
info("info_slot_bool", actors({"slot": False, "team": 0}, {"slot": True, "team": 1}), False)
info("info_slot_float", actors({"slot": 0, "team": 0}, {"slot": 1.0, "team": 1}), False)
info("info_team_bool", actors({"slot": 0, "team": 0}, {"slot": 1, "team": True}), False)
info("info_actor_extra_key", actors({"slot": 0, "team": 0, "name": "x"}, {"slot": 1, "team": 1}), False)
info("info_slot_out_of_order", actors({"slot": 1, "team": 0}, {"slot": 0, "team": 1}), False)
info("info_noop_not_first", {**WORKER, "actions": ["bolt", "noop"]}, False)

# --- framing: only spaces, tabs and carriage returns make a blank line ---

frame("frame_empty", "", True)
frame("frame_spaces", "   ", True)
frame("frame_tab", "\t", True)
frame("frame_carriage_return", "\r", True)
frame("frame_mixed_blank", " \t\r \t", True)
frame("frame_vertical_tab", "\x0b", False)
frame("frame_form_feed", "\x0c", False)
frame("frame_vertical_tab_in_spaces", " \x0b ", False)
frame("frame_nul", "\x00", False)
frame("frame_no_break_space", " ", False)
frame("frame_next_line", "\u0085", False)
frame("frame_request", '{"v":1,"type":"status","id":"q"}', False)

# --- decode cases ---

decode("hello_ok", hello("h1", client="leeroy_dojo 0.1.0", expect={"scenario": "x"}), rid="h1", rtype="hello")
decode("reset_ok", reset("r1", "e1", seed=2**53 - 1, scenario="fixture_duel_v1"), rid="r1", rtype="reset")
decode("step_ok", step("r2", "e1", 4, ((0, 2), (1, 0))), rid="r2", rtype="step")
decode("step_empty_actions_decodes", step("r2", "e1", 0, ()), rid="r2", rtype="step")
decode("status_ok", status("q"), rid="q", rtype="status")
decode("status_with_session", {**status("q"), "session": "s1"}, rid="q", rtype="status")
decode("close_ok", close(reason="done"), rid="c1", rtype="close")
decode("padded_whitespace", '  {"v":1,"type":"status","id":"q"}  ', rid="q", rtype="status")
decode("multibyte_text_ok", hello("h1", client="héllo"), rid="h1", rtype="hello")
decode("not_json", "not json", "malformed")
decode("empty_line", "", "malformed")
decode("array", "[1,2]", "malformed")
decode("nan_is_not_json", '{"v":1,"type":"status","id":"q","session":NaN}', "malformed")
decode("lone_surrogate", '{"v":1,"type":"status","id":"q","session":"\\ud800"}', "malformed")
decode("missing_v", {"type": "status", "id": "q"}, "malformed", "q")
decode("v_float", '{"v":1.0,"type":"status","id":"q"}', "malformed", "q")
decode("v_string", {"v": "1", "type": "status", "id": "q"}, "malformed", "q")
decode("v_huge", '{"v":99999999999999999999999,"type":"status","id":"q"}', "malformed", "q")
decode("v2_unsupported", {"v": 2, "type": "status", "id": "q"}, "unsupported_version", "q")
decode("v2_without_id", {"v": 2, "type": "status"}, "unsupported_version")
decode("missing_id", {"v": 1, "type": "status"}, "malformed")
decode("id_with_space", {"v": 1, "type": "status", "id": "a b"}, "malformed")
decode("id_too_long", {"v": 1, "type": "status", "id": "x" * 65}, "malformed")
decode("id_64_ok", {"v": 1, "type": "status", "id": "x" * 64}, rid="x" * 64, rtype="status")
decode("id_number", {"v": 1, "type": "status", "id": 7}, "malformed")
decode("unknown_type", {"v": 1, "type": "dance", "id": "q"}, "malformed", "q")
decode("missing_type", {"v": 1, "id": "q"}, "malformed", "q")
decode("unknown_field", {**status("q"), "extra": True}, "malformed", "q")
decode("unknown_field_name_is_clipped", {**status("q"), "k" * 3000: 1}, "malformed", "q")
decode("status_bad_session", {**status("q"), "session": "a b"}, "malformed", "q")
decode("reset_missing_seed", {k: v for k, v in reset("r1", "e1").items() if k != "scenario_seed"}, "malformed", "r1")
decode("reset_negative_seed", reset("r1", "e1", seed=-1), "malformed", "r1")
decode("reset_seed_too_big", reset("r1", "e1", seed=2**53), "malformed", "r1")
decode("reset_decision_too_big", reset("r1", "e1", decision=1000001), "malformed", "r1")
decode("reset_bad_episode", reset("r1", "e 1"), "malformed", "r1")
decode("reset_bad_scenario", reset("r1", "e1", scenario=""), "malformed", "r1")
decode("step_negative_decision", step("r2", "e1", -1), "malformed", "r2")
decode("step_actions_not_list", {**step("r2", "e1", 0), "actions": {"actor": 0}}, "malformed", "r2")
decode("step_too_many_actions", step("r2", "e1", 0, ((0, 0), (1, 0), (0, 0))), "malformed", "r2")
decode("step_action_extra_key", {**step("r2", "e1", 0), "actions": [{"actor": 0, "action": 0, "x": 1}]},
       "malformed", "r2")
decode("step_actor_bool", {**step("r2", "e1", 0), "actions": [{"actor": True, "action": 0}]}, "malformed", "r2")
decode("step_action_float", '{"v":1,"type":"step","id":"r2","session":"s1","episode":"e1","decision":0,'
       '"actions":[{"actor":0,"action":1.5}]}', "malformed", "r2")
decode("step_missing_session", {k: v for k, v in step("r2", "e1", 0).items() if k != "session"}, "malformed", "r2")
decode("hello_protocol_reversed", {**hello("h1"), "protocol": {"min": 2, "max": 1}}, "malformed", "h1")
decode("hello_protocol_extra", {**hello("h1"), "protocol": {"min": 1, "max": 1, "x": 1}}, "malformed", "h1")
decode("hello_missing_protocol", {"v": 1, "type": "hello", "id": "h1"}, "malformed", "h1")
decode("hello_expect_unknown", hello("h1", expect={"color": "blue"}), "malformed", "h1")
decode("hello_client_too_long", hello("h1", client="é" * 129), "malformed", "h1")
decode("close_missing_session", {"v": 1, "type": "close", "id": "c1"}, "malformed", "c1")
decode("nested_64_reaches_the_schema", {**status("q"), "session": json.loads("[" * 63 + "]" * 63)}, "malformed", "q")
decode("nested_65_is_too_deep", {**status("q"), "session": json.loads("[" * 64 + "]" * 64)}, "malformed")
decode("brackets_in_strings_do_not_nest", hello("h1", client="[" * 200), rid="h1", rtype="hello")
decode("line_too_long", status("q" * 40), "line_too_long", bounds={"max_line_bytes": 64})
decode("line_at_bound", status("q" * 33), rid="q" * 33, rtype="status", bounds={"max_line_bytes": 64})
# A byte order mark is not JSON whitespace: refused at the start of a line as anywhere else.
decode("bom_is_refused", '﻿{"v":1,"type":"status","id":"q"}', "malformed")
decode("bom_after_space_is_refused", ' ﻿{"v":1,"type":"status","id":"q"}', "malformed")
# A line that is not blank by the framing rule is decoded, and a lone vertical tab or form feed is not JSON.
decode("vertical_tab_line", "\x0b", "malformed")
decode("form_feed_line", "\x0c", "malformed")
# An integer that no double can hold makes the whole line not JSON, so its id cannot be trusted; one that a double
# holds is merely out of range for its field.
RESET_HEAD = '{"v":1,"type":"reset","id":"r1","session":"s1","episode":"e1","scenario_seed":7,"decision":'
decode("integer_beyond_double", RESET_HEAD + "9" * 400 + "}", "malformed")
decode("negative_integer_beyond_double", RESET_HEAD + "-" + "9" * 400 + "}", "malformed")
decode("integer_just_beyond_double", RESET_HEAD + "2" + "0" * 308 + "}", "malformed")
decode("integer_within_double", RESET_HEAD + "1" + "0" * 308 + "}", "malformed", "r1")
decode("integer_beyond_uint64", RESET_HEAD + "18446744073709551616}", "malformed", "r1")
decode("float_beyond_double", RESET_HEAD + "1e400}", "malformed")
decode("id_beyond_double", '{"v":1,"type":"status","id":' + "9" * 400 + "}", "malformed")

# --- session cases ---

session("happy_episode", [
    connect(),
    recv(status("q0"), [status_result("q0", "ready", session=None)]),
    recv(hello(), [hello_result()]),
    recv(reset("r1", "e1"), effects=[begin_reset(1, 1, "e1")], state="resetting"),
    recv(status("q1"), [status_result("q1", "resetting", episode="e1", generation=1, in_flight="r1")]),
    complete_reset(1, 1, reset_done(), [reset_result("r1", "e1", 1)], state="awaiting_action"),
    recv(step("r2", "e1", 0, ((0, 2), (1, 0))), effects=[begin_step(1, 2, "e1", 0, ((0, 2), (1, 0)))],
         state="stepping"),
    complete_step(1, 2, d := step_done(200, [{"kind": "cast_start", "actor": 0, "action_id": 0, "t_ms": 150}],
                                       rewards=((0, 0.0), (0, 0.0)), masks=([1, 1, 0], [1, 0, 1])),
                  [step_result("r2", "e1", 0, 1, ((0, 2), (1, 0)), d)], state="awaiting_action"),
    recv(status("q2"), [status_result("q2", "awaiting_action", episode="e1", decision=1, generation=1)]),
    recv(step("r3", "e1", 1), effects=[begin_step(1, 3, "e1", 1)], state="stepping"),
    complete_step(1, 3, d := step_done(400, [{"kind": "damage", "actor": 1, "action_id": 0, "t_ms": 360}],
                                       terminated=True, outcome={"reason": "death", "winner": 0},
                                       rewards=((1, 0.025), (-1, -0.025))),
                  [step_result("r3", "e1", 1, 1, ((0, 0), (1, 0)), d)], state="terminal"),
    recv(step("r4", "e1", 2), [err("not_ready", "r4")], state="terminal"),
    recv(close(), [close_result()], [release("closed"), CLOSE_CONNECTION], state="ready"),
    op("disconnect", state="ready"),
])

session("duplicate_step_applies_once", [
    *started(),
    recv(step("r2", "e1", 0, ((0, 2), (1, 2))), effects=[begin_step(1, 2, "e1", 0, ((0, 2), (1, 2)))]),
    recv(step("r2", "e1", 0, ((0, 2), (1, 2))), state="stepping"),
    recv(step("r2", "e1", 0, ((0, 0), (1, 2))), [err("conflicting_duplicate", "r2")]),
    recv(status("q1"), [status_result("q1", "stepping", episode="e1", decision=0, generation=1, in_flight="r2")]),
    complete_step(1, 2, d := step_done(200), [r := step_result("r2", "e1", 0, 1, ((0, 2), (1, 2)), d), r],
                  state="awaiting_action"),
    recv(step("r2", "e1", 0, ((0, 2), (1, 2))), [r], state="awaiting_action"),
    recv(step("r9", "e1", 0, ((0, 2), (1, 2))), [err("wrong_step", "r9")]),
    recv(step("r2", "e1", 0, ((0, 0), (1, 0))), [err("conflicting_duplicate", "r2")]),
    recv(status("r2"), [err("conflicting_duplicate", "r2")]),
    recv(status("q2"), [status_result("q2", "awaiting_action", episode="e1", decision=1, generation=1)]),
])

session("duplicate_hello_and_reset", [
    connect(),
    recv(hello(), [hello_result()]),
    recv(hello(), [hello_result()]),
    recv(hello("h2"), [err("not_ready", "h2")]),
    recv(reset("r1", "e1"), effects=[begin_reset(1, 1, "e1")]),
    recv(reset("r1", "e1"), state="resetting"),
    complete_reset(1, 1, reset_done(), [r := reset_result("r1", "e1", 1), r], state="awaiting_action"),
    recv(reset("r1", "e1"), [r], state="awaiting_action"),
    recv(reset("r5", "e1"), [err("stale_episode", "r5")]),
    recv(reset("r6", "e2", decision=3), [err("wrong_step", "r6")]),
    recv(reset("r1", "e1", seed=8), [err("conflicting_duplicate", "r1")]),
    recv(status("q1"), [status_result("q1", "awaiting_action", episode="e1", decision=0, generation=1)]),
])

# Copies of the request in flight are owed only up to bounds.max_copies, the original included: the inbox's worth.
# Beyond that a copy is answered busy at once, so no controller can make one completion fan out without bound.
session("duplicate_copies_are_capped", [
    *started(bounds=wire_bounds(max_copies=3)),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    recv(step("r2", "e1", 0)),
    recv(step("r2", "e1", 0), state="stepping"),
    recv(step("r2", "e1", 0), [err("busy", "r2")]),
    recv(step("r2", "e1", 0), [err("busy", "r2")]),
    recv(step("r2", "e1", 0, ((0, 2), (1, 0))), [err("conflicting_duplicate", "r2")]),
    recv(status("q1"), [status_result("q1", "stepping", episode="e1", decision=0, generation=1, in_flight="r2")]),
    complete_step(1, 2, d := step_done(200), [r := step_result("r2", "e1", 0, 1, ((0, 0), (1, 0)), d), r, r],
                  state="awaiting_action"),
    recv(step("r2", "e1", 0), [r]),
    recv(reset("r3", "e2"), effects=[begin_reset(2, 3, "e2")]),
    recv(reset("r3", "e2")),
    recv(reset("r3", "e2")),
    recv(reset("r3", "e2"), [err("busy", "r3")], state="resetting"),
    complete_reset(2, 3, reset_done(), [r := reset_result("r3", "e2", 2), r, r], state="awaiting_action"),
    recv(step("r4", "e2", 0), effects=[begin_step(2, 4, "e2", 0)]),
    recv(step("r4", "e2", 0)),
    recv(step("r4", "e2", 0)),
    recv(step("r4", "e2", 0), [err("busy", "r4")]),
    recv(close(), [err("aborted", "r4", "closed")] * 3 + [close_result()],
         [abort(2, "closed"), release("closed"), CLOSE_CONNECTION], state="closing"),
], bounds={"max_copies": 3})

session("one_copy_means_no_duplicates_in_flight", [
    *started(bounds=wire_bounds(max_copies=1)),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    recv(step("r2", "e1", 0), [err("busy", "r2")], state="stepping"),
    complete_step(1, 2, d := step_done(200), [r := step_result("r2", "e1", 0, 1, ((0, 0), (1, 0)), d)],
                  state="awaiting_action"),
    recv(step("r2", "e1", 0), [r]),
], bounds={"max_copies": 1})

session("stale_episode_and_wrong_step", [
    *started(),
    recv(step("r2", "e0", 0), [err("stale_episode", "r2")]),
    recv(step("r3", "e1", 1), [err("wrong_step", "r3")]),
    recv(step("r4", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    complete_step(1, 2, d := step_done(200), [step_result("r4", "e1", 0, 1, ((0, 0), (1, 0)), d)]),
    recv(reset("r5", "e2"), effects=[begin_reset(2, 3, "e2")], state="resetting"),
    complete_reset(2, 3, reset_done(), [reset_result("r5", "e2", 2)]),
    recv(step("r6", "e1", 1), [err("stale_episode", "r6")]),
    recv(reset("r7", "e1"), [err("stale_episode", "r7")]),
    recv(step("r8", "e2", 0), effects=[begin_step(2, 4, "e2", 0)], state="stepping"),
])

session("invalid_actions_have_no_effect", [
    *started(masks=([1, 0, 1], [1, 1, 0])),
    recv(step("r2", "e1", 0, ((0, 0),)), [err("invalid_action", "r2")]),
    recv(step("r3", "e1", 0, ((0, 0), (0, 2))), [err("invalid_action", "r3")]),
    recv(step("r4", "e1", 0, ((0, 0), (2, 0))), [err("invalid_action", "r4")]),
    recv(step("r5", "e1", 0, ((0, 3), (1, 0))), [err("invalid_action", "r5")]),
    recv(step("r6", "e1", 0, ((0, 1), (1, 0))), [err("invalid_action", "r6")]),
    recv(step("r7", "e1", 0, ((0, 0), (1, 2))), [err("invalid_action", "r7")]),
    recv(step("r8", "e1", 0, ()), [err("invalid_action", "r8")], state="awaiting_action"),
    recv(step("r9", "e1", 0, ((1, 1), (0, 2))), effects=[begin_step(1, 2, "e1", 0, ((0, 2), (1, 1)))],
         state="stepping"),
    complete_step(1, 2, d := step_done(200), [step_result("r9", "e1", 0, 1, ((0, 2), (1, 1)), d)]),
])

session("noop_is_always_a_command", [
    *started(masks=([1, 0, 0], [1, 0, 0])),
    recv(step("r2", "e1", 0, ((0, 0), (1, 0))), effects=[begin_step(1, 2, "e1", 0)], state="stepping"),
    complete_step(1, 2, d := step_done(200, masks=([1, 0, 0], [1, 0, 0])),
                  [step_result("r2", "e1", 0, 1, ((0, 0), (1, 0)), d)], state="awaiting_action"),
    recv(step("r3", "e1", 1, ((0, 2), (1, 0))), [err("invalid_action", "r3")]),
])

session("not_ready_bad_session_and_busy", [
    connect(),
    recv(reset("r1", "e1"), [err("not_ready", "r1")]),
    recv(step("r2", "e1", 0), [err("not_ready", "r2")]),
    recv(close(), [err("not_ready", "c1")], state="ready"),
    recv(hello(), [hello_result()]),
    recv(step("r3", "e1", 0), [err("not_ready", "r3")]),
    recv(reset("r4", "e1", session="s9"), [err("bad_session", "r4")]),
    recv(close("c2", session="s9"), [err("bad_session", "c2")]),
    recv(reset("r5", "e1"), effects=[begin_reset(1, 1, "e1")]),
    recv(step("r6", "e1", 0), [err("busy", "r6")]),
    recv(reset("r7", "e2"), [err("busy", "r7")], state="resetting"),
    complete_reset(1, 1, reset_done(), [reset_result("r5", "e1", 1)]),
    recv(step("r8", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    recv(reset("r9", "e2"), [err("busy", "r9")], state="stepping"),
])

session("booting_refuses_reset", [
    connect(),
    recv(hello(), [hello_result(state="booting")], state="booting"),
    recv(reset("r1", "e1"), [err("not_ready", "r1")]),
    recv(status("q1"), [status_result("q1", "booting")]),
    op("boot_complete", state="ready"),
    recv(reset("r2", "e1"), effects=[begin_reset(1, 1, "e1")], state="resetting"),
], booted=False)

session("callbacks_are_fenced_by_generation", [
    connect(),
    recv(hello(), [hello_result()]),
    recv(reset("r1", "e1"), effects=[begin_reset(1, 1, "e1")]),
    callback(1, True),
    callback(0, False),
    complete_reset(1, 1, reset_done(), [reset_result("r1", "e1", 1)]),
    callback(1, True),
    recv(reset("r2", "e2"), effects=[begin_reset(2, 2, "e2")]),
    callback(1, False),
    complete_reset(1, 1, reset_done(), stale=True, state="resetting"),
    complete_reset(1, 2, reset_done(), stale=True, state="resetting"),     # the right token, an old generation
    complete_reset(2, 2, reset_done(), [reset_result("r2", "e2", 2)], state="awaiting_action"),
    callback(2, True),
    recv(step("r3", "e2", 0), effects=[begin_step(2, 3, "e2", 0)]),
    complete_step(1, 3, step_done(200), stale=True, state="stepping"),
    complete_reset(2, 3, reset_done(), stale=True, state="stepping"),     # generation and token right, kind wrong
    complete_step(2, 3, d := step_done(200, terminated=True, outcome={"reason": "death", "winner": 1},
                                       rewards=((-1, 0.0), (1, 0.0))),
                  [step_result("r3", "e2", 0, 2, ((0, 0), (1, 0)), d)], state="terminal"),
    callback(2, False),
])

# A completion finishes only the operation whose token it carries: a late or repeated completion of an earlier
# decision, in the same generation, can never answer the next one, and only the abandoned operation's own
# completion ends a drain.
session("completions_need_their_token", [
    *started(),
    complete_reset(1, 1, reset_done(), stale=True, state="awaiting_action"),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    complete_step(1, 1, step_done(200), stale=True, state="stepping"),
    complete_step(1, 3, step_done(200), stale=True, state="stepping"),
    complete_reset(1, 2, reset_done(), stale=True, state="stepping"),
    complete_step(1, 2, d := step_done(200), [step_result("r2", "e1", 0, 1, ((0, 0), (1, 0)), d)],
                  state="awaiting_action"),
    recv(step("r3", "e1", 1), effects=[begin_step(1, 3, "e1", 1)]),
    complete_step(1, 2, step_done(200), stale=True, state="stepping"),
    recv(status("q1"), [status_result("q1", "stepping", episode="e1", decision=1, generation=1, in_flight="r3")]),
    complete_step(1, 3, d := step_done(400), [step_result("r3", "e1", 1, 1, ((0, 0), (1, 0)), d)],
                  state="awaiting_action"),
    recv(step("r4", "e1", 2), effects=[begin_step(1, 4, "e1", 2)]),
    op("disconnect", effects=[abort(1, "disconnect"), release("disconnect")], state="closing"),
    complete_step(1, 3, step_done(600), stale=True, state="closing"),
    complete_step(2, 4, step_done(600), stale=True, state="closing"),
    complete_step(1, 4, step_done(600), stale=True, state="ready"),
])

session("fault_while_paused", [
    *started(),
    op("fault", effects=[abort(1, "fault")], state="aborted"),
    callback(1, False),
    recv(step("r2", "e1", 0), [err("aborted", "r2", "fault")]),
    recv(status("q1"), [status_result("q1", "aborted", episode="e1", generation=2, abort_reason="fault")]),
    recv(reset("r3", "e1"), [err("stale_episode", "r3")]),
    recv(reset("r4", "e2"), effects=[begin_reset(3, 2, "e2")], state="resetting"),
    complete_reset(3, 2, reset_done(), [reset_result("r4", "e2", 3)], state="awaiting_action"),
    recv(status("q2"), [status_result("q2", "awaiting_action", episode="e2", decision=0, generation=3)]),
])

session("fault_mid_step_drains", [
    *started(),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    op("fault", [err("aborted", "r2", "fault")], [abort(1, "fault")], state="aborted"),
    recv(step("r2", "e1", 0), [err("aborted", "r2", "fault")]),
    recv(reset("r3", "e2"), [err("not_ready", "r3")]),
    recv(status("q1"), [status_result("q1", "aborted", episode="e1", generation=2, draining=True,
                                      abort_reason="fault")]),
    complete_step(1, 2, step_done(200), stale=True, state="aborted"),
    recv(reset("r3", "e2"), effects=[begin_reset(3, 3, "e2")], state="resetting"),
])

session("reset_failure_aborts", [
    connect(),
    recv(hello(), [hello_result()]),
    recv(reset("r1", "e1"), effects=[begin_reset(1, 1, "e1")]),
    complete_reset(1, 1, {"ok": False, "reason": "fixture login timed out"}, [err("aborted", "r1", "reset_failed")],
                   [abort(1, "reset_failed")], state="aborted"),
    recv(reset("r1", "e1"), [err("aborted", "r1", "reset_failed")]),
    recv(step("r2", "e1", 0), [err("aborted", "r2", "reset_failed")]),
    recv(reset("r3", "e1"), [err("stale_episode", "r3")]),
    recv(reset("r4", "e2"), effects=[begin_reset(3, 2, "e2")], state="resetting"),
])

# A worker's reason or fault text is echoed in the error, clipped to the message bound the runners check.
session("worker_reasons_are_clipped", [
    connect(),
    recv(hello(), [hello_result()]),
    recv(reset("r1", "e1"), effects=[begin_reset(1, 1, "e1")]),
    complete_reset(1, 1, {"ok": False, "reason": "x" * 5000}, [err("aborted", "r1", "reset_failed")],
                   [abort(1, "reset_failed")], state="aborted"),
    recv(reset("r2", "e2"), effects=[begin_reset(3, 2, "e2")]),
    complete_reset(3, 2, reset_done(), [reset_result("r2", "e2", 3)]),
    recv(step("r3", "e2", 0), effects=[begin_step(3, 3, "e2", 0)]),
    complete_step(3, 3, {"fault": "é" * 3000}, [err("aborted", "r3", "fault")], [abort(3, "fault")],
                  state="aborted"),
    recv({**step("r4", "e3", 0), "x" * 3000: 1}, [err("malformed", "r4")]),
])

session("event_overflow_aborts", [
    *started(bounds=wire_bounds(max_events=2)),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    complete_step(1, 2, step_done(200, [{"kind": "tick"}] * 3), [err("result_overflow", "r2", "result_overflow")],
                  [abort(1, "result_overflow")], state="aborted"),
    recv(step("r3", "e1", 0), [err("aborted", "r3", "result_overflow")]),
    recv(reset("r4", "e2"), effects=[begin_reset(3, 3, "e2")]),
    complete_reset(3, 3, reset_done(), [reset_result("r4", "e2", 3)]),
    recv(step("r5", "e2", 0), effects=[begin_step(3, 4, "e2", 0)]),
    complete_step(3, 4, d := step_done(200, [{"kind": "tick"}] * 2),
                  [step_result("r5", "e2", 0, 3, ((0, 0), (1, 0)), d)], state="awaiting_action"),
], bounds={"max_events": 2})

session("line_overflow_aborts", [
    connect(),
    recv(hello(), [hello_result(bounds=wire_bounds(max_line_bytes=2048))]),
    recv(reset("r1", "e1"), effects=[begin_reset(1, 1, "e1")]),
    complete_reset(1, 1, {"ok": True, "observations": [{"actor": i, "obs": [0.25] * 300, "mask": [1, 0, 1]}
                                                       for i in range(2)], "sim_ms": 0},
                   [err("result_overflow", "r1", "result_overflow")], [abort(1, "result_overflow")], state="aborted"),
    recv(reset("r2", "e2"), effects=[begin_reset(3, 2, "e2")]),
    complete_reset(3, 2, reset_done(), [reset_result("r2", "e2", 3)], state="awaiting_action"),
], bounds={"max_line_bytes": 2048})

session("invalid_results_abort", [
    connect(),
    recv(hello(), [hello_result()]),
    recv(reset("r1", "e1"), effects=[begin_reset(1, 1, "e1")]),
    complete_reset(1, 1, reset_done(masks=([0, 0, 1], [1, 0, 1])), [err("aborted", "r1", "invalid_result")],
                   [abort(1, "invalid_result")], state="aborted"),
    recv(reset("r2", "e2"), effects=[begin_reset(3, 2, "e2")]),
    complete_reset(3, 2, {"ok": True, "observations": observations()[:1], "sim_ms": 0},
                   [err("aborted", "r2", "invalid_result")], [abort(3, "invalid_result")], state="aborted"),
    recv(reset("r3", "e3"), effects=[begin_reset(5, 3, "e3")]),
    complete_reset(5, 3, reset_done(), [reset_result("r3", "e3", 5)]),
    recv(step("r4", "e3", 0), effects=[begin_step(5, 4, "e3", 0)]),
    complete_step(5, 4, step_done(200, terminated=True, truncated=True,
                                  outcome={"reason": "external", "winner": None}),
                  [err("aborted", "r4", "invalid_result")], [abort(5, "invalid_result")], state="aborted"),
    recv(reset("r5", "e4"), effects=[begin_reset(7, 5, "e4")]),
    complete_reset(7, 5, reset_done(), [reset_result("r5", "e4", 7)]),
    recv(step("r6", "e4", 0), effects=[begin_step(7, 6, "e4", 0)]),
    complete_step(7, 6, step_done(200, terminated=True, outcome={"reason": "death", "winner": None}),
                  [err("aborted", "r6", "invalid_result")], [abort(7, "invalid_result")], state="aborted"),
    recv(reset("r7", "e5"), effects=[begin_reset(9, 7, "e5")]),
    complete_reset(9, 7, reset_done(), [reset_result("r7", "e5", 9)]),
    recv(step("r8", "e5", 0), effects=[begin_step(9, 8, "e5", 0)]),
    complete_step(9, 8, {"fault": "fixture map crashed"}, [err("aborted", "r8", "fault")], [abort(9, "fault")],
                  state="aborted"),
])

# Worker payload types are as exact as request types: a bool or a float is never an actor index, a mask bit or a
# millisecond count, in either language.
session("worker_payload_types_are_exact", [
    connect(),
    recv(hello(), [hello_result()]),
    recv(reset("r1", "e1"), effects=[begin_reset(1, 1, "e1")]),
    complete_reset(1, 1, {"ok": True, "observations": observations(actors=(False, True)), "sim_ms": 0},
                   [err("aborted", "r1", "invalid_result")], [abort(1, "invalid_result")], state="aborted"),
    recv(reset("r2", "e2"), effects=[begin_reset(3, 2, "e2")]),
    complete_reset(3, 2, {"ok": True, "observations": observations(actors=(0.0, 1.0)), "sim_ms": 0},
                   [err("aborted", "r2", "invalid_result")], [abort(3, "invalid_result")], state="aborted"),
    recv(reset("r3", "e3"), effects=[begin_reset(5, 3, "e3")]),
    complete_reset(5, 3, reset_done(), [reset_result("r3", "e3", 5)]),
    recv(step("r4", "e3", 0), effects=[begin_step(5, 4, "e3", 0)]),
    complete_step(5, 4, step_done(200, reward_actors=(False, 1)), [err("aborted", "r4", "invalid_result")],
                  [abort(5, "invalid_result")], state="aborted"),
    recv(reset("r5", "e4"), effects=[begin_reset(7, 5, "e4")]),
    complete_reset(7, 5, reset_done(), [reset_result("r5", "e4", 7)]),
    recv(step("r6", "e4", 0), effects=[begin_step(7, 6, "e4", 0)]),
    complete_step(7, 6, step_done(200, reward_actors=(0, 1.0)), [err("aborted", "r6", "invalid_result")],
                  [abort(7, "invalid_result")], state="aborted"),
    recv(reset("r7", "e5"), effects=[begin_reset(9, 7, "e5")]),
    complete_reset(9, 7, {"ok": True, "observations": observations(masks=([True, False, True], [1, 0, 1])),
                          "sim_ms": 0},
                   [err("aborted", "r7", "invalid_result")], [abort(9, "invalid_result")], state="aborted"),
    recv(reset("r8", "e6"), effects=[begin_reset(11, 8, "e6")]),
    complete_reset(11, 8, {"ok": True, "observations": observations(values=((True, 0.5), (1.0, 0.5))), "sim_ms": 0},
                   [err("aborted", "r8", "invalid_result")], [abort(11, "invalid_result")], state="aborted"),
    recv(reset("r9", "e7"), effects=[begin_reset(13, 9, "e7")]),
    complete_reset(13, 9, {"ok": True, "observations": observations(), "sim_ms": 1.0},
                   [err("aborted", "r9", "invalid_result")], [abort(13, "invalid_result")], state="aborted"),
    recv(reset("r10", "e8"), effects=[begin_reset(15, 10, "e8")]),
    complete_reset(15, 10, reset_done(), [reset_result("r10", "e8", 15)], state="awaiting_action"),
])

session("disconnect_mid_step", [
    *started(),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    op("disconnect", effects=[abort(1, "disconnect"), release("disconnect")], state="closing"),
    callback(1, False),
    connect("s2"),
    recv(hello(), [hello_result(session="s2", state="closing")]),
    recv(reset("r1", "e1", session="s2"), [err("not_ready", "r1")]),
    recv(status("q1"), [status_result("q1", "closing", session="s2", generation=2, draining=True)]),
    complete_step(1, 2, step_done(200), stale=True, state="ready"),
    recv(reset("r1", "e1", session="s2"), effects=[begin_reset(3, 3, "e1")], state="resetting"),
    complete_reset(3, 3, reset_done(), [reset_result("r1", "e1", 3, session="s2")], state="awaiting_action"),
])

session("disconnect_while_paused", [
    *started(),
    op("disconnect", effects=[abort(1, "disconnect"), release("disconnect")], state="ready"),
    connect("s2"),
    recv(hello(), [hello_result(session="s2")]),
    recv(step("r2", "e1", 0, session="s2"), [err("not_ready", "r2")]),
    recv(step("r3", "e1", 0), [err("bad_session", "r3")]),
    recv(reset("r4", "e1", session="s2"), effects=[begin_reset(3, 2, "e1")]),
])

session("disconnect_while_resetting", [
    connect(),
    recv(hello(), [hello_result()]),
    recv(reset("r1", "e1"), effects=[begin_reset(1, 1, "e1")]),
    op("disconnect", effects=[abort(1, "disconnect"), release("disconnect")], state="closing"),
    op("drained", state="ready"),
    connect("s2"),
    recv(hello(), [hello_result(session="s2")]),
    complete_reset(1, 1, reset_done(), stale=True, state="ready"),
    recv(reset("r1", "e1", session="s2"), effects=[begin_reset(3, 2, "e1")], state="resetting"),
])

session("disconnect_after_terminal_and_before_hello", [
    *started(),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    complete_step(1, 2, d := step_done(120, terminated=True, outcome={"reason": "simultaneous_death", "winner": None},
                                       elapsed_ms=120),
                  [step_result("r2", "e1", 0, 1, ((0, 0), (1, 0)), d)], state="terminal"),
    op("disconnect", effects=[release("disconnect")], state="ready"),
    connect("s2"),
    op("disconnect", effects=[release("disconnect")], state="ready"),
    op("disconnect", state="ready"),
])

session("close_while_stepping", [
    *started(),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    recv(step("r2", "e1", 0)),
    recv(close(reason="controller done"), [err("aborted", "r2", "closed"), err("aborted", "r2", "closed"),
                                           close_result()],
         [abort(1, "closed"), release("closed"), CLOSE_CONNECTION], state="closing"),
    complete_step(1, 2, step_done(200), stale=True, state="ready"),
])

session("close_while_paused", [
    *started(),
    recv(close(), [close_result()], [abort(1, "closed"), release("closed"), CLOSE_CONNECTION], state="ready"),
    connect("s2"),
    recv(status("q1"), [status_result("q1", "ready", session=None, generation=2)]),
])

session("terminal_outcomes", [
    *started(),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    complete_step(1, 2, d := step_done(150, terminated=True, outcome={"reason": "simultaneous_death", "winner": None},
                                       elapsed_ms=150),
                  [step_result("r2", "e1", 0, 1, ((0, 0), (1, 0)), d)], state="terminal"),
    recv(reset("r3", "e2"), effects=[begin_reset(2, 3, "e2")]),
    complete_reset(2, 3, reset_done(), [reset_result("r3", "e2", 2)]),
    recv(step("r4", "e2", 0), effects=[begin_step(2, 4, "e2", 0)]),
    complete_step(2, 4, d := step_done(200, truncated=True, outcome={"reason": "external", "winner": None}),
                  [step_result("r4", "e2", 0, 2, ((0, 0), (1, 0)), d)], state="terminal"),
    recv(reset("r5", "e3"), effects=[begin_reset(3, 5, "e3")]),
    complete_reset(3, 5, reset_done(sim_ms=179800), [reset_result("r5", "e3", 3, sim_ms=179800)]),
    recv(step("r6", "e3", 0), effects=[begin_step(3, 6, "e3", 0)]),
    complete_step(3, 6, d := step_done(180000, terminated=True, outcome={"reason": "time_limit", "winner": None}),
                  [step_result("r6", "e3", 0, 3, ((0, 0), (1, 0)), d)], state="terminal"),
    recv(step("r7", "e3", 1), [err("not_ready", "r7")]),
])

session("malformed_requests_change_nothing", [
    *started(),
    recv("{not json", [err("malformed", None)]),
    recv({**step("r2", "e1", 0), "extra": 1}, [err("malformed", "r2")]),
    recv({**step("r2", "e1", 0), "v": 2}, [err("unsupported_version", "r2")]),
    recv({**step("r2", "e1", 0), "decision": "0"}, [err("malformed", "r2")]),
    recv('﻿{"v":1,"type":"status","id":"q9"}', [err("malformed", None)]),
    recv('{"v":1,"type":"step","id":"r2","session":"s1","episode":"e1","actions":[],"decision":' + "9" * 400 + "}",
         [err("malformed", None)]),
    recv(status("q1"), [status_result("q1", "awaiting_action", episode="e1", decision=0, generation=1)]),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)], state="stepping"),
])

session("hello_version_and_scenario_checks", [
    connect(),
    recv({**hello("h1"), "protocol": {"min": 2, "max": 3}}, [err("unsupported_version", "h1")]),
    recv(hello("h2", expect={"observation_schema": "dojo.other.obs/1"}), [err("unsupported_version", "h2")]),
    recv(hello("h3", expect={"action_schema": "dojo.other.act/1"}), [err("unsupported_version", "h3")]),
    recv(reset("r0", "e1"), [err("not_ready", "r0")]),
    recv(hello("h4", expect={"observation_schema": "dojo.fixture.obs/1", "action_schema": "dojo.fixture.act/1",
                             "scenario": "fixture_duel_v1"}),
         [hello_result("h4")]),
    recv(reset("r1", "e1", scenario="other_v1"), [err("unsupported_version", "r1")]),
    recv(reset("r2", "e1", scenario="fixture_duel_v1"), effects=[begin_reset(1, 1, "e1")]),
])

session("evicted_duplicates_still_cannot_apply", [
    *started(bounds=wire_bounds(result_cache=2)),
    recv(step("r2", "e1", 0), effects=[begin_step(1, 2, "e1", 0)]),
    complete_step(1, 2, d2 := step_done(200), [r2 := step_result("r2", "e1", 0, 1, ((0, 0), (1, 0)), d2)]),
    recv(step("r3", "e1", 1), effects=[begin_step(1, 3, "e1", 1)]),
    complete_step(1, 3, d3 := step_done(400), [r3 := step_result("r3", "e1", 1, 1, ((0, 0), (1, 0)), d3)]),
    recv(step("r2", "e1", 0), [r2]),
    recv(step("r3", "e1", 1), [r3]),
    recv(reset("r1", "e1"), [err("stale_episode", "r1")]),
    recv(hello(), [err("not_ready", "h1")]),
    recv(status("q1"), [status_result("q1", "awaiting_action", episode="e1", decision=2, generation=1)]),
], bounds={"result_cache": 2})

session("fault_without_a_session", [
    op("fault", state="aborted"),
    connect(),
    recv(hello(), [hello_result(state="aborted")]),
    recv(status("q1"), [status_result("q1", "aborted", abort_reason="fault")]),
    recv(reset("r1", "e1"), effects=[begin_reset(1, 1, "e1")], state="resetting"),
    recv(status("q2"), [status_result("q2", "resetting", episode="e1", generation=1, in_flight="r1")]),
])

OUT.write_text("".join(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n" for line in lines),
               encoding="utf-8", newline="\n")
print(f"wrote {len(lines)} lines to {OUT}")
