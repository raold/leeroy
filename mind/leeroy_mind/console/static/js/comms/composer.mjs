import { $, KEY_REFUSED, emit, on, post, str } from "../core.mjs";
import { S } from "../state.mjs";
import { currentView, viewTitle } from "./feedview.mjs";
import { MAX_BYTES, composerState, targetFor, utf8Bytes } from "./reply.mjs";

// The reply box under the Comms feed (#composer): his line, said in WoW as his on the open view's
// channel (# guild, # officer, # party / raid) or whispered to the open thread's member, through
// POST /api/say and the mind's say op. Only while he is in game: otherwise the box is closed with
// the reason in it (comms/reply.mjs says which, and never "not in game" for a mind or bridge that
// is down). One line at a time: while one is on its way the box keeps its text and Send waits, and
// Enter sends once per press (a held key's repeats send nothing). A sent line empties the box and
// is not drawn here: it appears in the feed when the game reports it. A refusal leaves the text and
// says why under the box. Each view keeps its own unsent text while the page lives.

var CLOSED = { control_off: 1, mind_down: 1, bridge_down: 1, game_unknown: 1, not_in_game: 1,
               connecting: 1, key_lost: 1, no_target: 1 };
var TEXT_BAD = { too_long: 1, command_prefix: 1 };
var WHERE = { guild: "#guild", officer: "#officer", group: "your party or raid" };
var NO_ANSWER = "The console did not answer. Check WoW before sending it again.";

var input = $("say-text"), count = $("say-count"), sendBtn = $("say-send"), why = $("say-why");
var heard = false;          // the console's status has come (until then S.status is the page's guess)
var keyLost = false;        // the console refused the key: nothing can be sent until a new one
var shownView = "";
var drafts = new Map();     // view -> its unsent text, for the views not open
var refusals = new Map();   // view -> why its last line was not sent, until he edits it
var sending = null;         // {view, text, where}: the line on its way

var MEASURE = document.createElement("canvas").getContext("2d");

function target(view) { return targetFor(view, viewTitle(view)); }
// Whether a closed box's reason fits in its line (narrow columns cut a placeholder off)
function fits(text) {
  var cs = getComputedStyle(input);
  MEASURE.font = cs.font;
  return MEASURE.measureText(text).width
         <= input.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
}
function whereOf(view, t) { return t.channel === "whisper" ? viewTitle(view) : WHERE[t.channel]; }

// The open view's text in the box, the one it leaves kept for later
function syncView() {
  var view = currentView();
  if (view === shownView) { return; }
  if (shownView) {
    if (input.value) { drafts.set(shownView, input.value); } else { drafts.delete(shownView); }
  }
  shownView = view;
  input.value = drafts.get(view) || "";
  drafts.delete(view);
}

function state(t) {
  var bytes = utf8Bytes(input.value);
  if (keyLost) { return { enabled: false, reason: KEY_REFUSED, bytes: bytes, code: "key_lost" }; }
  if (!heard) { return { enabled: false, reason: "Connecting to the console…", bytes: bytes, code: "connecting" }; }
  if (!t) { return { enabled: false, reason: "Nothing can be sent from this view.", bytes: bytes, code: "no_target" }; }
  return composerState({ status: S.status, sending: !!sending, text: input.value });
}

export function renderComposer() {
  syncView();
  var t = target(shownView), st = state(t), closed = !!CLOSED[st.code];
  $("say-to").textContent = t ? whereOf(shownView, t) : "-";
  input.disabled = closed;
  input.readOnly = !!sending;  // its text stays as sent, and the focus stays in it
  input.placeholder = closed ? st.reason : t.placeholder;
  input.title = closed ? st.reason : "";
  count.textContent = st.bytes + " / " + MAX_BYTES;
  count.classList.toggle("bad", st.bytes > MAX_BYTES);
  sendBtn.disabled = !st.enabled;
  var refused = refusals.get(shownView) || "", hint = "", bad = false;
  // Closed: the reason is the placeholder, and is said under the box too when text hides it or
  // the line is too narrow for it
  if (closed) { hint = input.value || !fits(st.reason) ? st.reason : ""; }
  else if (st.code === "sending") { hint = st.reason; }
  else if (refused) { hint = refused; bad = true; }
  else if (!st.enabled && input.value) { hint = st.reason; bad = !!TEXT_BAD[st.code]; }
  why.textContent = hint;
  why.classList.toggle("bad", bad);
}

function send() {
  syncView();
  var view = shownView, t = target(view), text = input.value;
  if (sending || !state(t).enabled) { renderComposer(); return; }
  var body = { channel: t.channel, text: text };
  if (t.to) { body.to = t.to; }
  sending = { view: view, text: text, where: whereOf(view, t) };
  refusals.delete(view);
  renderComposer();
  post("/api/say", body).then(function (j) {
    settle(j.ok === true ? "" : str(j.error) || "It was not sent.");
  }, function () { settle(NO_ANSWER); });
}
function settle(error) {
  var was = sending;
  sending = null;
  if (!error) {
    if (shownView === was.view) {
      if (input.value === was.text) { input.value = ""; }
    } else if (drafts.get(was.view) === was.text) { drafts.delete(was.view); }
  } else {
    refusals.set(was.view, error);
    if (shownView !== was.view) { emit("note", "Not sent to " + was.where + ": " + error); }
  }
  renderComposer();
}

input.addEventListener("input", function () { refusals.delete(shownView); renderComposer(); });
input.addEventListener("keydown", function (ev) {
  if (ev.key !== "Enter" || ev.isComposing) { return; }
  ev.preventDefault();
  if (!ev.repeat) { send(); }
});
sendBtn.addEventListener("click", send);
window.addEventListener("resize", renderComposer);

on("snapshot", function () { heard = true; keyLost = false; renderComposer(); });
on("status", function () { heard = true; renderComposer(); });
on("key-lost", function () { keyLost = true; renderComposer(); });
on("feed", renderComposer);  // the open view changed, or its title (a thread's name)
