import { obj, str } from "../core.mjs";

// The reply box as data: where a line from the open view goes (targetFor) and whether the box can
// send what it holds now (composerState). No DOM here: node imports this file.
//
// The console's own checks come first, in the order the page can see them: control off, the mind
// not running, its bridge down, a game state nobody can read, not in game. A down mind or bridge
// is named as such, never as his absence. Then one line at a time (sending), then the text in
// native's order: empty, too long, a command; then a slash command, which WoW would post as words
// on the channel (the console refuses it too; the mind and native do not). The length is the raw
// text's UTF-8 bytes: the mind cleans first (drops "|" and control characters, collapses
// whitespace), which never makes a line longer, so the box never offers a line the mind refuses
// for its length. The mind and the game check again and have the last word (POST /api/say answers
// with their words).

export var MAX_BYTES = 255;
var ENCODER = new TextEncoder();
// What cleaning drops or turns into a space: before it a "." or "!" still starts a command
var BLANK = /^[\s|\u0000-\u001f\u007f-\u009f]*/;
var WHISPER = /^w:([1-9]\d{0,9})$/;
var PLACES = { guild: "Message #guild", officer: "Message #officer", group: "Message your party or raid" };

export var WORDS = {
  control_off: "Replies are off: this console runs with --no-control.",
  bridge_down: "The game bridge is down.",
  not_in_game: "You're not in game.",
  sending: "Sending your line…",
  empty_text: "There's nothing to send.",
  command_prefix: "Commands can't be sent from the console.",
  slash_command: "Slash commands aren't sent from here."
};

export function utf8Bytes(text) { return ENCODER.encode(str(text)).length; }

// {channel, placeholder}, plus to (the member's guid) for a whisper thread; null for a view no
// line can go to
export function targetFor(view, threadName) {
  view = str(view);
  if (PLACES[view]) { return { channel: view, placeholder: PLACES[view] }; }
  var m = WHISPER.exec(view), to = m ? Number(m[1]) : 0;
  if (!(to > 0 && to < 4294967296)) { return null; }
  return { channel: "whisper", to: to, placeholder: "Whisper " + (str(threadName) || "#" + to) };
}

function mindDown(state) {
  return !state || state === "unknown" ? "The mind's state is unclear, so nothing can be sent."
       : "The mind is " + state + ", so nothing can be sent.";
}

// {enabled, reason, bytes, code}: code names the first thing in the way ("" when it can be sent),
// so the box can tell what it shows (control_off, mind_down, bridge_down, game_unknown and
// not_in_game close it; sending holds it; empty_text, too_long, command_prefix and slash_command
// are the text's)
export function composerState(o) {
  o = obj(o);
  var st = obj(o.status), mind = obj(st.mind), game = obj(st.game), text = str(o.text);
  var bytes = utf8Bytes(text);
  function no(code, reason) { return { enabled: false, reason: reason || WORDS[code], bytes: bytes, code: code }; }
  if (obj(st.control).enabled === false) { return no("control_off"); }
  if (mind.state !== "running") { return no("mind_down", mindDown(str(mind.state))); }
  if (st.bridge === false) { return no("bridge_down"); }
  if (game.state === "not_in_game") { return no("not_in_game"); }
  if (game.state !== "in_game") {
    return no("game_unknown", "The console can't tell whether you're in game ("
                              + (str(game.reason) || "no reason given") + ").");
  }
  if (o.sending) { return no("sending"); }
  var rest = text.replace(BLANK, "");
  if (!rest) { return no("empty_text"); }
  if (bytes > MAX_BYTES) { return no("too_long", "Too long: " + bytes + " of " + MAX_BYTES + " bytes."); }
  if (rest.charAt(0) === "." || rest.charAt(0) === "!") { return no("command_prefix"); }
  if (rest.charAt(0) === "/") { return no("slash_command"); }
  return { enabled: true, reason: "", bytes: bytes, code: "" };
}
