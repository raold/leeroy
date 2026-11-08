import { $, arr, hhmm, on } from "./core.mjs";
import { S, hereRoom, name, roomName } from "./state.mjs";
import { selfView } from "./comms/tree.mjs";

// -- the strip ----------------------------------------------------------------------------------
// Always on, above the tabs. A "status", "snapshot" or "voice" event redraws it.

var MIND_WORDS = { running: "mind running", starting: "mind starting", stopping: "mind stopping",
                   stopped: "mind stopped", crashed: "mind crashed", unknown: "mind: unclear" };

export function setPill(id, cls, text, title) {
  var e = $(id);
  e.className = e.className.replace(/\b(ok|warn|bad)\b/g, "").trim() + (cls ? " " + cls : "");
  var words = e.querySelector(".t");
  if (words.textContent !== text) { words.textContent = text; }
  e.title = title || "";
}
export function mindClass(state) {
  return state === "running" ? "ok" : state === "crashed" ? "bad"
       : (state === "starting" || state === "stopping" || state === "unknown") ? "warn" : "";
}
export function gameWords() {
  var g = S.status.game;
  if (g.state === "in_game") { return "you: in game" + (g.since ? " since " + hhmm(g.since) : ""); }
  if (g.state === "not_in_game") { return "you: not in game"; }
  return "you: unknown";
}
export function gamePill() {
  var st = S.status.game.state;
  return st === "in_game" ? "In game" : st === "not_in_game" ? "Not in game" : "Game: unknown";
}
export function renderStrip() {
  var m = S.status.mind;
  setPill("s-mind", mindClass(m.state), MIND_WORDS[m.state] || ("mind " + m.state),
          m.detail || (m.pid ? "pid " + m.pid : ""));
  var bridge = S.status.bridge;
  setPill("s-bridge", m.state !== "running" ? "" : bridge === true ? "ok" : "bad",
          "game bridge" + (m.state !== "running" ? " -" : bridge === true ? "" : " down"),
          m.state !== "running" ? "the mind is not running" : "");
  var v = S.status.voice, vtext, vcls;
  if (!v.enabled) { vtext = "voice off"; vcls = ""; }
  else if (!v.service) { vtext = "voice service down"; vcls = "bad"; }
  else if (v.link) { vtext = "voice linked"; vcls = "ok"; }
  else { vtext = "voice: mind not linked"; vcls = "warn"; }
  setPill("s-vlink", vcls, vtext, "");
  setPill("s-game", S.status.game.state === "in_game" ? "ok" : S.status.game.state === "not_in_game" ? "warn" : "",
          gamePill(), S.status.game.reason || "");
  var here = hereRoom(), roomText;
  if (!S.voice) { roomText = "voice not connected"; }
  else if (!here) { roomText = "not in a room"; }
  else {
    var talking = arr(S.voice.speaking).map(function (g) { return name(g); });
    roomText = roomName(here) + (talking.length ? " · " + talking.join(", ") + " speaking" : "");
  }
  setPill("s-room", "", roomText, roomText);
  $("s-ptt").classList.toggle("on", !!(S.voice && S.voice.ptt));
  var me = selfView(S.voice);
  $("s-self").textContent = me.deafen ? "deafened" : me.mute ? "muted" : "";
  $("s-self").hidden = !me.mute;
}

on("status", renderStrip);
on("snapshot", renderStrip);
on("voice", renderStrip);
