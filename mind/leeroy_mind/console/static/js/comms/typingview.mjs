import { $, clear, el, emit, on } from "../core.mjs";
import { S, name } from "../state.mjs";
import { FEED, currentView } from "./feedview.mjs";
import { staleIn, typingByView, typingLabel, whisperTypers } from "./typing.mjs";

// Who is typing in Comms: "<Name> is typing" and three dots under the open view's feed, where
// the line will appear, and the members writing a whisper to him, which the whisper list marks
// (leftview.mjs, on a "typing" event). From the strip's `typing` (the mind's status, on the
// strip's own poll); nothing while the mind or its bridge is down. No poll of its own: a timer
// only takes a stale entry away when no new strip comes. Staleness counts from when this page
// received the strip, on performance.now() (monotonic), never this device's date and time.

var BY_VIEW = new Map(), shown = null, listed = "", timer = null;
var held = null, receivedAt = 0;  // the strip's typing list in hand, and when it came

function live() { return S.status.mind && S.status.mind.state === "running" && S.status.bridge === true; }

// The three dots; with a label they stand for it (the whisper list), else they are decoration
export function typingDots(label) {
  var d = el("span", "dots");
  if (label) {
    d.setAttribute("role", "img");
    d.setAttribute("aria-label", label);
    d.title = label;
  } else {
    d.setAttribute("aria-hidden", "true");
  }
  for (var i = 0; i < 3; i++) { d.appendChild(el("i")); }
  return d;
}

// The members writing a whisper to him, oldest first: [{guid, name}]
export function typingWhispers() { return whisperTypers(BY_VIEW); }

function render() {
  var who = BY_VIEW.get(currentView()) || [];
  var label = typingLabel(who.map(function (t) { return t.name; }));
  if (label === shown) { return; }
  shown = label;
  var box = $("typing");
  clear(box);
  if (!label) { return; }
  box.appendChild(el("span", "label", label));
  box.appendChild(typingDots());
}

function update() {
  clearTimeout(timer);
  timer = null;
  var now = performance.now();
  if (S.status.typing !== held) { held = S.status.typing; receivedAt = now; }  // a new strip
  BY_VIEW = live() ? typingByView(held, {
    receivedAt: receivedAt, now: now, human: S.voice ? S.voice.human : 0, name: name,
    lines: function (view) { return FEED.lines(view); } }) : new Map();
  render();
  var key = JSON.stringify(whisperTypers(BY_VIEW));
  if (key !== listed) { listed = key; emit("typing"); }
  var left = staleIn(BY_VIEW);
  if (left < Infinity) { timer = setTimeout(update, Math.max(1000, left * 1000 + 50)); }
}

on("snapshot", update);
on("status", update);
on("feed", update);  // a new line (its writer's dots go), or another view opened
