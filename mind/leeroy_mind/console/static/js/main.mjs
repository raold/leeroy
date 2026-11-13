import { $, api, arr, emit, hashKey, on, own, refused, remember, setKey } from "./core.mjs";
import { serverBuild } from "./update.mjs";
import { S, adoptVoice, applyVoice } from "./state.mjs";
import { forgetFaces, photosHidden, redrawFaces } from "./faces.mjs";
import { renderStrip } from "./strip.mjs";
import { renderFeed } from "./comms/feedview.mjs";
import { renderLeft } from "./comms/leftview.mjs";
import { refreshFaces, renderRight } from "./comms/rightview.mjs";
import { renderComposer } from "./comms/composer.mjs";
import { initPanelToggle } from "./comms/panel.mjs";
import "./comms/discordview.mjs";
import "./raid.mjs";
import "./guild.mjs";
import "./social.mjs";
import "./board.mjs";
import "./calendar.mjs";
import "./tonight.mjs";
import "./realm.mjs";
import "./bank.mjs";
import "./training.mjs";
import "./dojo.mjs";
import "./library.mjs";
import "./lab.mjs";
import "./ops.mjs";
import "./personas.mjs";
import "./mind.mjs";
import { showTab } from "./tabs.mjs";
import { ReconnectingStream } from "./eventstream.mjs";
import "./freshness.mjs";
import "./gpu.mjs";

// The page's start: the key, the first drawing, the open tab and the console's event stream.
// Every tab module above registers its tab and its bus listeners as it loads.

var connection = null, keyMissing = false;

// -- the connection -------------------------------------------------------------------------------

function lost(text) {
  var e = $("s-lost");
  e.textContent = text;
  e.hidden = !text;
}
function keyLost() {
  keyMissing = true;
  if (connection) { connection.stop(); }
  lost("This console has restarted, or this page was reloaded or opened without its key: open the URL the console printed when it started.");
}
on("key-lost", keyLost);
// A member's photo picked or undone (faces.mjs let the old one go): the room's faces and the
// card's photo in Comms are drawn again with the new one
on("selfie", function (d) { if (d.action !== "more") { refreshFaces(); } });
var KINDS = { snapshot: 1, status: 1, voice: 1, chat: 1, chat_gone: 1, control: 1, raid: 1, tuning: 1,
              selfie: 1, board: 1, discord: 1, feedback: 1 };
// One server-sent event block: "event: <kind>" and "data: <json>" lines; ": ping" is a comment
function dispatch(block) {
  var kind = "", data = "";
  block.split("\n").forEach(function (line) {
    if (line.indexOf("event: ") === 0) { kind = line.slice(7); }
    else if (line.indexOf("data: ") === 0) { data += line.slice(6); }
  });
  if (!kind || !own(KINDS, kind)) { return; }
  var d;
  try { d = JSON.parse(data); } catch (e) { return; }
  if (!d || typeof d !== "object" || Array.isArray(d)) { return; }
  if (kind === "snapshot" && (!d.status || !Array.isArray(d.rooms))) { return; }
  var status = kind === "snapshot" ? d.status : kind === "status" ? d : null;
  if (status && (!status.mind || !status.game || !status.voice)) { return; }
  if (kind === "snapshot") {
    serverBuild(d.build);
    S.status = d.status || S.status;
    S.rooms = arr(d.rooms);
    S.mood = d.mood && typeof d.mood === "object" ? d.mood : null;
    adoptVoice(d.voice);
  }
  if (kind === "status") { S.status = d; }
  if (kind === "voice") { applyVoice(d); }
  emit(kind, d);
}
// The event stream is read with fetch, since an EventSource cannot send the key's header
function connect() {
  if (!connection) {
    connection = new ReconnectingStream({
      open: function (signal) { return api("/api/events", { headers: { "Accept": "text/event-stream" }, signal: signal }); },
      refused: refused, dispatch: dispatch, connected: function () { lost(""); },
      retrying: function () { lost("The console is not reachable: retrying."); }
    });
  }
  if (!document.hidden && !keyMissing) { connection.start(); }
}

// The key from the URL's fragment (see setKey), then out of the address bar
var first = hashKey(location.hash);
setKey(first || "");
if (first !== null) { history.replaceState(null, "", location.pathname + "#comms"); }

// A new key pasted into this open tab (the console restarted on the same port) changes only the
// fragment, so the page does not reload: take it and connect again with it. A malformed one
// leaves the key as it was.
function takeKey(key) {
  setKey(key);
  forgetFaces();  // the photos the old key was refused are asked for again
  keyMissing = false;
  if (connection) { connection.stop(); }
  lost("");
  connect();
  refreshFaces();  // the room's faces and the card's photo, with the new key
  showTab(S.tab);  // the open tab asks again at once
}

renderStrip();
renderLeft();
renderFeed(false);
renderRight();
renderComposer();
showTab((location.hash || "#comms").slice(1));
window.addEventListener("hashchange", function () {
  var key = hashKey(location.hash);
  if (key !== null) {
    history.replaceState(null, "", location.pathname + "#" + S.tab);
    if (key) { takeKey(key); }
    return;
  }
  var want = location.hash.slice(1);
  if (want !== S.tab) { showTab(want); }
});
connect();
document.addEventListener("visibilitychange", function () {
  if (document.hidden) { if (connection) { connection.stop(); } }
  else { connect(); }
});
window.addEventListener("online", function () { if (connection) { connection.stop(); } connect(); });

// The light/dark switch. theme.js applied the stored choice before paint; this flips it and
// remembers it under the one key (JSON, so a stored value reads "light" or "dark" with quotes)
var THEME_KEY = "guild-console-theme";
function currentTheme() { return document.documentElement.dataset.theme === "light" ? "light" : "dark"; }
$("theme").addEventListener("click", function () {
  var next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  remember(THEME_KEY, next);
});

// Hide photos: every member photo on the page becomes initials (faces) or a blank (the gallery,
// the selfie picker), remembered like the theme; theme.js applied it before paint
var PHOTOS_KEY = "guild-console-photos";
function showPhotosButton() {
  var hidden = photosHidden();
  $("photos").textContent = hidden ? "Show photos" : "Hide photos";
  $("photos").setAttribute("aria-pressed", String(hidden));
}
$("photos").addEventListener("click", function () {
  var hide = !photosHidden();
  if (hide) { document.documentElement.dataset.photos = "hidden"; }
  else { delete document.documentElement.dataset.photos; }
  remember(PHOTOS_KEY, hide ? "hidden" : "shown");
  showPhotosButton();
  redrawFaces();
  refreshFaces();
});
showPhotosButton();
initPanelToggle();
