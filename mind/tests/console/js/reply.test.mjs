import test from "node:test";
import assert from "node:assert/strict";
import { MAX_BYTES, composerState, targetFor, utf8Bytes } from "../../../leeroy_mind/console/static/js/comms/reply.mjs";

// The reply box as data: where a line from the open view goes and whether the box can send what
// it holds. The status is the console's strip (S.status); invented members only.

function status(o) {
  o = o || {};
  return {
    mind: { state: o.mind || "running", detail: "" },
    bridge: o.bridge === undefined ? true : o.bridge,
    game: { state: o.game || "in_game", reason: o.reason || "", since: 1000 },
    voice: { enabled: false, service: false, link: null },
    control: { enabled: o.control === undefined ? true : o.control, reason: "" }
  };
}
function state(st, text, sending) { return composerState({ status: st, sending: !!sending, text: text }); }

test("each view says where its line goes", () => {
  assert.deepEqual(targetFor("guild", ""), { channel: "guild", placeholder: "Message #guild" });
  assert.deepEqual(targetFor("officer", ""), { channel: "officer", placeholder: "Message #officer" });
  assert.deepEqual(targetFor("group", ""), { channel: "group", placeholder: "Message your party or raid" });
  assert.deepEqual(targetFor("w:1101", "Arkon"), { channel: "whisper", to: 1101, placeholder: "Whisper Arkon" });
  assert.deepEqual(targetFor("w:1102", ""), { channel: "whisper", to: 1102, placeholder: "Whisper #1102" });
  ["", "trade", "w:0", "w:01", "w:4294967296", "w:x", null].forEach(function (v) {
    assert.equal(targetFor(v, "Arkon"), null, String(v));
  });
});

test("control off gives the --no-control words", () => {
  var s = state(status({ control: false }), "hi");
  assert.equal(s.enabled, false);
  assert.equal(s.reason, "Replies are off: this console runs with --no-control.");
});

test("a mind that is not running is named, never as not in game", () => {
  var down = status({ mind: "stopped", bridge: null, game: "unknown", reason: "the mind is not running" });
  var s = state(down, "hi");
  assert.equal(s.enabled, false);
  assert.equal(s.reason, "The mind is stopped, so nothing can be sent.");
  assert.ok(!/not in game/i.test(s.reason));
  assert.equal(state(status({ mind: "crashed", bridge: null, game: "unknown" }), "hi").reason,
               "The mind is crashed, so nothing can be sent.");
  assert.equal(state(status({ mind: "starting", bridge: null, game: "unknown" }), "hi").reason,
               "The mind is starting, so nothing can be sent.");
  assert.equal(state(status({ mind: "unknown", bridge: null, game: "unknown" }), "hi").reason,
               "The mind's state is unclear, so nothing can be sent.");
  // Even a game state left over from before: the mind comes first
  assert.equal(state(status({ mind: "stopped", game: "not_in_game" }), "hi").reason,
               "The mind is stopped, so nothing can be sent.");
});

test("the mind running with its bridge down says the bridge", () => {
  var s = state(status({ bridge: false, game: "unknown", reason: "the mind's game bridge is down" }), "hi");
  assert.equal(s.enabled, false);
  assert.equal(s.reason, "The game bridge is down.");
});

test("nobody can tell: the can't-tell words with their reason", () => {
  var s = state(status({ bridge: null, game: "unknown", reason: "the mind's game bridge state is unknown" }), "hi");
  assert.equal(s.enabled, false);
  assert.equal(s.reason, "The console can't tell whether you're in game (the mind's game bridge state is unknown).");
  assert.equal(state(status({ game: "unknown", reason: "the mind's session could not be read (OperationalError)" }), "hi").reason,
               "The console can't tell whether you're in game (the mind's session could not be read (OperationalError)).");
});

test("not in game says so", () => {
  var s = state(status({ game: "not_in_game" }), "hi");
  assert.equal(s.enabled, false);
  assert.equal(s.reason, "You're not in game.");
});

test("sending disables", () => {
  var s = state(status(), "hi", true);
  assert.equal(s.enabled, false);
  assert.equal(s.code, "sending");
  assert.ok(s.reason);
  // The box keeps what it holds while it is on its way: its bytes are still counted
  assert.equal(s.bytes, 2);
  // and the environment still comes first
  assert.equal(state(status({ game: "not_in_game" }), "hi", true).reason, "You're not in game.");
});

test("an empty box, or one of only spaces, pipes and control characters, has nothing to send", () => {
  ["", "   ", "\n\t", "|||", " |\u0007 "].forEach(function (t) {
    var s = state(status(), t);
    assert.equal(s.enabled, false, JSON.stringify(t));
    assert.equal(s.reason, "There's nothing to send.");
  });
});

test("256 bytes are too long, counted as UTF-8 before any cleaning", () => {
  var s = state(status(), "é".repeat(128));
  assert.equal(s.bytes, 256);
  assert.equal(s.enabled, false);
  assert.equal(s.reason, "Too long: 256 of 255 bytes.");
  var edge = state(status(), "é".repeat(127) + "a");
  assert.equal(edge.bytes, MAX_BYTES);
  assert.equal(edge.enabled, true);
  assert.equal(state(status(), "a".repeat(256)).reason, "Too long: 256 of 255 bytes.");
  assert.equal(MAX_BYTES, 255);
});

test("the raw count is never below what the mind counts after cleaning", () => {
  // Cleaning only drops characters and turns runs of whitespace into one space
  assert.equal(utf8Bytes("gg 🎉"), 7);
  assert.equal(utf8Bytes(" hi   there \n now "), 18);  // the mind sends "hi there now", 12
  assert.equal(utf8Bytes("|cffff0000|Hitem:1|h[x]|h|r"), 27);  // the mind sends 22
  assert.equal(state(status(), "|" + "a".repeat(255)).enabled, false);  // the mind's 255: the box errs short
});

test("a command, after leading spaces, cannot be sent", () => {
  [" .gm", ".gm on", "!roll", "   .gm on", "\t!x", " | .gm"].forEach(function (t) {
    var s = state(status(), t);
    assert.equal(s.enabled, false, JSON.stringify(t));
    assert.equal(s.reason, "Commands can't be sent from the console.");
  });
  assert.equal(state(status(), "a .gm").enabled, true);
});

test("native's order for the text: empty, then too long, then a command", () => {
  assert.equal(state(status(), "   ").reason, "There's nothing to send.");
  assert.equal(state(status(), "." + "a".repeat(300)).reason, "Too long: 301 of 255 bytes.");
  assert.equal(state(status(), ".a").reason, "Commands can't be sent from the console.");
});

test("in game with a line, it can be sent", () => {
  var s = state(status(), "hi");
  assert.equal(s.enabled, true);
  assert.equal(s.reason, "");
  assert.equal(s.bytes, 2);
});

test("no status at all is an unclear mind, never not in game", () => {
  var s = composerState({ status: null, sending: false, text: "hi" });
  assert.equal(s.enabled, false);
  assert.equal(s.reason, "The mind's state is unclear, so nothing can be sent.");
});

test("a slash command is refused in the box: it would reach WoW as words", () => {
  ["/w Arkon hi", "  /dance", " | /w Arkon hi", "\t/2 hello", "/"].forEach(function (t) {
    var s = state(status(), t);
    assert.equal(s.enabled, false, JSON.stringify(t));
    assert.equal(s.code, "slash_command", JSON.stringify(t));
    assert.equal(s.reason, "Slash commands aren't sent from here.");
  });
  assert.equal(state(status(), "hi /wave").enabled, true);
  assert.equal(state(status(), "/" + "a".repeat(300)).reason, "Too long: 301 of 255 bytes.");  // native's order first
});
